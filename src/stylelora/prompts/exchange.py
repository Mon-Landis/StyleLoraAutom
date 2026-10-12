"""Preview-first structured exchange. A preview is a content-addressed immutable DTO."""
import csv
import io
import json
import warnings
from dataclasses import replace
from uuid import uuid4
from stylelora.tagging.catalog import LocalCatalog
from stylelora.tagging.tokenizer import tokenize_prompt
from .types import *
from .validation import require, strict_loads, construct, validate_content, normalize_tag
from .dictionaries import _encode, _decode, load_dictionary, append_entries, digest, LOCK, _path, _write


def preview_digest(preview):
    return digest({k:v for k,v in plain(preview).items() if k not in ('digest','preview_id')})


def preview_import(data, *, format, kind, dimension=None, base_revision=1, catalog=None):
    require(isinstance(data,bytes) and len(data)<=10*1024*1024)
    require(format in ('json','csv','txt') and kind in ('features','prompts'))
    catalog=catalog or LocalCatalog(); entries=[]; features=[]; issues=[]; raw=[]
    def prompt(row,number):
        entry=construct(PromptEntry,row); validate_content(entry,catalog=catalog); entries.append(entry)
    def feature(row,number):
        candidate=construct(FeatureCandidate,row)
        validate_content(PromptEntry((candidate.tag,)),catalog=catalog)
        features.append(candidate)
    try:
        text=data.decode('utf-8-sig')
    except UnicodeError:
        raise PromptError('schema:invalid',issues=(Issue('import:encoding','Use UTF-8.'),)) from None
    if format=='json':
        obj=strict_loads(text); value=_decode(obj)
        require(obj['kind']==kind)
        rows=([plain(e) for e in value.entries] if kind=='prompts' else [plain(c) for cs in value.dimensions.values() for c in cs])
    elif format=='csv':
        reader=csv.DictReader(io.StringIO(text),strict=True)
        expected={'tags','natural_language','aspect','rating'} if kind=='prompts' else {'dimension','tag','rating'}
        require(reader.fieldnames is not None and len(reader.fieldnames)==len(set(reader.fieldnames)) and set(reader.fieldnames)==expected)
        try: rows=list(reader)
        except csv.Error: raise PromptError('schema:invalid') from None
    else:
        require(kind!='features' or bool(dimension),'import_mapping_required')
        rows=text.splitlines(); raw=list(rows)
    for number,row in enumerate(rows,2 if format=='csv' else 1):
        try:
            if format=='txt':
                if not row.strip(): continue
                if kind=='features':
                    parts=row.split('\t'); require(len(parts)<=2)
                    row={'dimension':dimension,'tag':parts[0],'rating':parts[1] or None if len(parts)==2 else None}
                else:
                    tags=[]; nl=[]
                    for token in tokenize_prompt(row,catalog=catalog):
                        require(token.syntax=='plain','import:syntax_requires_edit')
                        if token.lookup in ('known','alias'): tags.append(normalize_tag(token.raw,catalog))
                        elif token.raw.strip(): nl.append(token.raw.strip())
                    row={'tags':tags,'natural_language':', '.join(nl) or None}
            elif format=='csv':
                require(None not in row and all(v is not None for v in row.values()))
                row={k:(v or None) for k,v in row.items()}
                if kind=='prompts': row['tags']=strict_loads(row['tags'])
            (prompt if kind=='prompts' else feature)(row,number)
        except (PromptError,TypeError,KeyError) as exc:
            issues.append(Issue(getattr(exc,'code','schema:invalid'),'Edit this row and preview again.',number))
    merged={}
    for c in features:
        key=(c.dimension,normalize_tag(c.tag,catalog))
        if key in merged and merged[key].rating!=c.rating:
            rating=max((r for r in (c.rating,merged[key].rating) if r),key=RATINGS.index,default=None)
            issues.append(Issue('dictionary:rating_conflict','Feature duplicates use the higher rating.',severity='warning'))
            merged[key]=replace(c,tag=key[1],rating=rating)
        else: merged[key]=replace(c,tag=key[1])
    value=ImportPreview(str(uuid4()),'pending',tuple(entries),tuple(merged[k] for k in sorted(merged)),tuple(issues),base_revision,kind,tuple(raw))
    return replace(value,digest=preview_digest(value))


def valid_rows_preview(preview):
    """Explicit opt-in to commit only valid rows; creates a distinct preview identity."""
    value=replace(preview,preview_id=str(uuid4()),issues=tuple(i for i in preview.issues if i.severity!='error'))
    return replace(value,digest=preview_digest(value))


def commit_import(dictionary_id, preview, *, request_key, root=None):
    require(preview.digest==preview_digest(preview),'storage:corrupt_snapshot')
    require(not any(i.severity=='error' for i in preview.issues),'schema:invalid')
    if preview.kind=='prompts':
        return append_entries(dictionary_id,preview.entries,expected_revision=preview.base_revision,request_key=request_key,root=root)
    with LOCK:
        value=load_dictionary(dictionary_id,root=root); require(isinstance(value,FeatureDictionary))
        path=_path(dictionary_id,'features',root); data=strict_loads(path.read_bytes()); ops=data.get('_operations',{})
        signature=digest({'operation':'import','preview':preview.digest})
        if request_key in ops:
            require(ops[request_key]['digest']==signature,'dictionary:request_key_conflict')
            result=ops[request_key]['result']; result['conflicts']=tuple(construct(Issue,i) for i in result['conflicts'])
            return AppendResult(**result)
        require(value.revision==preview.base_revision,'dictionary:revision_conflict')
        dims={d:list(cs) for d,cs in value.dimensions.items()}; duplicates=0; inserted=0; conflicts=[]
        for c in preview.features:
            cs=dims.setdefault(c.dimension,[]); old=next((x for x in cs if x.tag==c.tag),None)
            if old:
                duplicates+=1
                if old.rating!=c.rating:
                    rating=max((r for r in (old.rating,c.rating) if r),key=RATINGS.index,default=None)
                    cs[cs.index(old)]=replace(old,rating=rating)
                    conflicts.append(Issue('dictionary:rating_conflict','Feature duplicates use the higher rating.',severity='warning'))
            else: cs.append(c); inserted+=1
        result=AppendResult(value.revision+1,inserted,duplicates,tuple(conflicts))
        data=_encode(replace(value,dimensions=dims,revision=result.revision)); ops[request_key]={'digest':signature,'result':plain(result)}; data['_operations']=ops
        try: _write(path,data)
        except OSError: raise PromptError('storage:io_error') from None
        return result


def export_value(value, format, *, dimension=None):
    require(format in ('json','csv','txt'))
    if format=='json': return json.dumps(_encode(value),ensure_ascii=False,separators=(',',':')).encode()
    out=io.StringIO(newline='')
    if isinstance(value,PromptDictionary):
        if format=='csv':
            w=csv.DictWriter(out,fieldnames=['tags','natural_language','aspect','rating']); w.writeheader()
            for e in value.entries:
                w.writerow({'tags':json.dumps(list(e.tags),ensure_ascii=False),'natural_language':e.natural_language or '', 'aspect':e.aspect or '', 'rating':e.rating or ''})
        else:
            warnings.warn('TXT loses aspect/rating and may reclassify comma fragments; use JSON or CSV for lossless exchange.',UserWarning,stacklevel=2)
            out.write('\n'.join(', '.join(e.tags)+(', '+e.natural_language if e.natural_language else '') for e in value.entries))
    else:
        if format=='csv':
            w=csv.DictWriter(out,fieldnames=['dimension','tag','rating']); w.writeheader()
            for d,cs in value.dimensions.items():
                for c in cs: w.writerow({'dimension':d,'tag':c.tag,'rating':c.rating or ''})
        else:
            require(dimension in value.dimensions,'import_mapping_required')
            warnings.warn('TXT exports one dimension and loses dictionary metadata; use JSON or CSV.',UserWarning,stacklevel=2)
            out.write('\n'.join(c.tag+'\t'+(c.rating or '') for c in value.dimensions[dimension]))
    return out.getvalue().encode('utf-8-sig' if format=='csv' else 'utf-8')


def export_dictionary(dictionary_id, format, *, root=None, dimension=None):
    return export_value(load_dictionary(dictionary_id,root=root),format,dimension=dimension)
