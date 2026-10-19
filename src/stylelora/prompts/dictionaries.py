"""Atomic JSON dictionaries with revision CAS and idempotent request keys."""
from __future__ import annotations
import json, os, tempfile, hashlib, threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from .types import *
from .validation import *

LOCK = threading.RLock()
def digest(value):
    return hashlib.sha256(json.dumps(plain(value),sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _root(root=None):
    return Path(root or os.environ.get('APPDATA', Path.home()) ) / 'StyleLoraAutom' / 'dictionaries'
def _path(dictionary_id, kind, root=None):
    identifier(dictionary_id)
    return _root(root) / kind / (dictionary_id + '.json')
def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            json.dump(plain(value), f, ensure_ascii=False, separators=(',', ':'))
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    except Exception:
        try: os.unlink(name)
        except OSError: pass
        raise
def _encode(value):
    if isinstance(value, FeatureDictionary):
        return {'schema_version':2,'kind':'features','dictionary_id':value.dictionary_id,'name':value.name,'revision':value.revision,
                'dimensions':{d:[{'tag':c.tag,'rating':c.rating} for c in cs] for d,cs in value.dimensions.items()}}
    return {'schema_version':2,'kind':'prompts','category':value.category,'dictionary_id':value.dictionary_id,'name':value.name,'revision':value.revision,
            'entries':[plain(e) for e in value.entries]}
def _decode(data):
    require(isinstance(data, dict))
    integer(data.get('schema_version'))
    require(data['schema_version'] in (1,2), 'newer_version' if data['schema_version'] > 2 else 'schema:invalid')
    kind=data.get('kind'); identifier(data.get('dictionary_id')); integer(data.get('revision'),1); text(data.get('name'))
    allowed = {'schema_version','kind','dictionary_id','name','revision','dimensions' if kind=='features' else 'entries','_operations'}
    if kind == 'prompts': allowed.add('category')
    require(set(data) <= allowed)
    if kind == 'features':
        require(isinstance(data.get('dimensions'),dict))
        dims={}
        for d, rows in data.get('dimensions', {}).items():
            require(isinstance(rows,list))
            for r in rows: require(isinstance(r,dict) and set(r) <= {'tag','rating'} and 'tag' in r)
            dims[d]=tuple(FeatureCandidate(d,r['tag'],r.get('rating')) for r in rows)
        return FeatureDictionary(data['dictionary_id'],data['name'],data['schema_version'],dims,data['revision'])
    if kind == 'prompts':
        require(isinstance(data.get('entries'),list))
        return PromptDictionary(data['dictionary_id'],data['name'],data['schema_version'],tuple(construct(PromptEntry,e) for e in data['entries']),data['revision'],data.get('category','full_content'))
    raise PromptError('schema:invalid')
def load_dictionary(dictionary_id, *, root=None):
    for kind in ('features','prompts'):
        path=_path(dictionary_id,kind,root)
        if path.is_file():
            raw = strict_loads(path.read_bytes())
            if kind == 'prompts' and raw.get('schema_version') in (3, 4, 5):
                require(not raw.get('_deleted'), 'dictionary:not_found')
                from .groups import migrate_group, legacy_dictionary
                return replace(legacy_dictionary(migrate_group(raw)), revision=raw['revision'])
            return _decode(raw)
    raise PromptError('dictionary:not_found')
def save_dictionary(value, *, expected_revision, request_key, root=None):
    with LOCK:
        require(isinstance(value,(FeatureDictionary,PromptDictionary))); text(request_key); integer(expected_revision)
        for entry in value.entries if isinstance(value,PromptDictionary) else (PromptEntry((c.tag,)) for cs in value.dimensions.values() for c in cs):
            validate_content(entry)
        path=_path(value.dictionary_id,'features' if isinstance(value,FeatureDictionary) else 'prompts',root)
        data=strict_loads(path.read_bytes()) if path.is_file() else None
        if data:
            require(type(data.get('schema_version')) is int and data['schema_version'] in PROMPT_GROUP_VERSIONS, 'newer_version')
            require(data['schema_version'] not in (3, 4, 5), 'dictionary:legacy_read_only')
        operations=data.get('_operations',{}) if data else {}
        signature=digest({'operation':'save','value':_encode(value),'expected_revision':expected_revision})
        if request_key in operations:
            legacy = _encode(value)
            legacy['schema_version'] = 1
            legacy.pop('category', None)
            legacy_signature = digest({'operation':'save','value':legacy,'expected_revision':expected_revision})
            require(operations[request_key]['digest'] in (signature, legacy_signature),'dictionary:request_key_conflict')
            return operations[request_key]['result']
        require((data['revision'] if data else 0)==expected_revision,'dictionary:revision_conflict')
        value=replace(value,revision=expected_revision+1)
        obj=_encode(value); operations[request_key]={'digest':signature,'result':value.revision}; obj['_operations']=operations
        try: _write(path,obj)
        except OSError: raise PromptError('storage:io_error') from None
        return value.revision
def append_entries(dictionary_id, entries, *, expected_revision, request_key, root=None):
    with LOCK:
        entries=tuple(entries); text(request_key)
        value=load_dictionary(dictionary_id,root=root)
        require(isinstance(value,PromptDictionary))
        path=_path(dictionary_id,'prompts',root); data=strict_loads(path.read_bytes()); operations=data.get('_operations',{})
        require(data.get('schema_version') not in (3, 4, 5), 'dictionary:legacy_read_only')
        legacy_signature=digest({'operation':'append','entries':entries,'expected_revision':expected_revision})
        signature=digest({'operation':'append','entries':entries,'expected_revision':expected_revision,'category':value.category})
        if request_key in operations:
            require(operations[request_key]['digest'] in (signature,legacy_signature),'dictionary:request_key_conflict')
            result=operations[request_key]['result']; result['conflicts']=tuple(construct(Issue,i) for i in result['conflicts'])
            return AppendResult(**result)
        require(value.revision==expected_revision,'dictionary:revision_conflict')
        current=list(value.entries); inserted=duplicates=0; conflicts=[]
        for entry in entries:
            require(isinstance(entry,PromptEntry)); validate_content(entry)
            old=next((e for e in current if entry_key(e)==entry_key(entry)),None)
            if old:
                if old.rating != entry.rating: conflicts.append(Issue('dictionary:rating_conflict','Choose the higher rating explicitly.'))
                else: duplicates += 1
                continue
            current.append(entry); inserted += 1
        if conflicts: return AppendResult(value.revision,0,duplicates,tuple(conflicts))
        result=AppendResult(value.revision+1,inserted,duplicates)
        data=_encode(replace(value,entries=tuple(current),revision=result.revision))
        operations[request_key]={'digest':signature,'result':plain(result)}; data['_operations']=operations
        try: _write(path,data)
        except OSError: raise PromptError('storage:io_error') from None
        return result


def list_dictionaries(*, category=None, root=None, kind=None):
    require(kind in (None,'features','prompts'))
    if category is not None:
        require(category == 'full_content','dictionary:category_unsupported')
        require(kind != 'features','dictionary:category_unsupported')
        kind = 'prompts'
    values = []
    for k in ((kind,) if kind else ('features','prompts')):
        for path in sorted((_root(root)/k).glob('*.json')):
            try:
                values.append(load_dictionary(path.stem, root=root))
            except PromptError as exc:
                if exc.code != 'dictionary:not_found':
                    raise
    return tuple(values)


def delete_dictionary(dictionary_id, *, expected_revision, root=None):
    with LOCK:
        value=load_dictionary(dictionary_id,root=root)
        raw = strict_loads(_path(dictionary_id,'features' if isinstance(value,FeatureDictionary) else 'prompts',root).read_bytes())
        require(raw.get('schema_version') not in (3, 4, 5), 'dictionary:legacy_read_only')
        require(value.revision==expected_revision,'dictionary:revision_conflict')
        try: _path(dictionary_id,'features' if isinstance(value,FeatureDictionary) else 'prompts',root).unlink()
        except OSError: raise PromptError('storage:io_error') from None
