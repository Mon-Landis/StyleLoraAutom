"""Frozen import previews and bounded, declared-file-only group packages."""
import hashlib
import io
import json
import re
import stat
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from .groups import Group, GroupRef, content, validate_entry
from .group_store import (JSON_LIMIT, ZIP_LIMIT, EXPANDED_LIMIT, IMAGE_LIMIT,
                          ENTRY_LIMIT, prepare_preview)
from .dictionaries import LOCK, digest
from .types import PromptError, Issue, plain
from .validation import require, strict_loads, identifier


def parse_json(data):
    require(len(data) <= JSON_LIMIT, 'import:limit')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise PromptError('import:encoding') from None
    return strict_loads(text)


def safe_name(name):
    require(isinstance(name, str) and name and '\\' not in name and '\x00' not in name, 'import:unsafe_archive')
    parts = name.rstrip('/').split('/')
    for part in parts:
        require(part not in ('', '.', '..') and not part.endswith((' ', '.')) and
                not any(c in part for c in ':<>"|?*') and not any(ord(c) < 32 for c in part) and
                not re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part), 'import:unsafe_archive')
    return '/'.join(parts)


def read_package(source):
    files, names, total = {}, set(), 0
    def add(name, stream, declared=None, compressed=None):
        nonlocal total
        safe_name(name)
        require(name.casefold() not in names, 'import:unsafe_archive')
        names.add(name.casefold())
        require(len(names) <= ENTRY_LIMIT + 1, 'import:limit')
        limit = JSON_LIMIT if name == 'group.json' else IMAGE_LIMIT
        data = bytearray()
        while True:
            chunk = stream.read(64*1024)
            if not chunk:
                break
            total += len(chunk); data.extend(chunk)
            require(total <= EXPANDED_LIMIT and len(data) <= limit, 'import:limit')
            if compressed is not None:
                require(len(data) <= max(1, compressed)*100, 'import:limit')
        require(declared is None or len(data) == declared, 'import:unsafe_archive')
        files[name] = bytes(data)
    try:
        if isinstance(source, (bytes, bytearray)):
            require(len(source) <= ZIP_LIMIT, 'import:limit')
            with zipfile.ZipFile(io.BytesIO(source)) as archive:
                require(len(archive.infolist()) <= ENTRY_LIMIT+2, 'import:limit')
                directories = set()
                for info in archive.infolist():
                    name = safe_name(info.filename)
                    mode = info.external_attr >> 16
                    require(not stat.S_ISLNK(mode) and stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR) and not info.flag_bits & 1,
                            'import:unsafe_archive')
                    require(not (info.external_attr & 0x400), 'import:unsafe_archive')
                    if info.is_dir():
                        require(name == 'previews' and name.casefold() not in directories, 'import:unsafe_archive')
                        directories.add(name.casefold()); continue
                    require(info.file_size <= max(1, info.compress_size)*100, 'import:limit')
                    with archive.open(info) as stream:
                        add(name, stream, info.file_size, info.compress_size)
        else:
            root = Path(source)
            require(root.is_dir() and not root.is_symlink() and not getattr(root.lstat(), 'st_file_attributes', 0) & 0x400, 'import:unsafe_archive')
            for path in root.rglob('*'):
                attributes = path.lstat()
                require(not path.is_symlink() and not getattr(attributes, 'st_file_attributes', 0) & 0x400, 'import:unsafe_archive')
                require(path.resolve().is_relative_to(root.resolve()), 'import:unsafe_archive')
                name = safe_name(path.relative_to(root).as_posix())
                if path.is_dir():
                    require(name == 'previews', 'import:unsafe_archive'); continue
                require(path.is_file(), 'import:unsafe_archive')
                with path.open('rb') as stream:
                    add(name, stream, attributes.st_size)
    except PromptError:
        raise
    except (OSError, ValueError, EOFError, RuntimeError, zipfile.BadZipFile):
        raise PromptError('import:unsafe_archive') from None
    require('group.json' in files, 'schema:invalid')
    return files


def convert_entry(raw):
    try:
        from .nai_convert import convert_nai_to_anima
    except ImportError:
        raise PromptError('import:unsupported') from None
    result = dict(raw); warnings = []
    for field in ('tag', 'expand_nl', 'nl'):
        value = result.get(field)
        if not value:
            continue
        text = ', '.join(value) if field == 'tag' else value
        try:
            converted = convert_nai_to_anima(text)
        except Exception:
            raise PromptError('import:conversion_failed') from None
        warnings.extend(plain(converted.warnings))
        result[field] = [part.strip() for part in converted.text.split(',') if part.strip()] if field == 'tag' else converted.text
    return result, warnings


class GroupExchange:
    def __init__(self, store, clock=time.monotonic):
        self.store, self.clock, self.previews, self.artifacts = store, clock, {}, {}
        self.preview_requests = {}

    def preview_import(self, data, options=None, request_key=None):
        options = options or {}
        require(isinstance(options, dict) and set(options) <= {'format', 'level', 'ref', 'metadata', 'source_format', 'entry_ids'})
        format = options.get('format', 'json')
        level = options.get('level', 'group')
        require(format in ('json', 'folder', 'zip') and level in ('group', 'entries'))
        require(options.get('source_format', 'anima') in ('anima', 'nai'))
        if options.get('source_format') == 'nai':
            # Fail explicitly even when all rows are invalid or empty.
            try:
                from .nai_convert import convert_nai_to_anima
            except ImportError:
                raise PromptError('import:unsupported') from None
        files = read_package(data) if format != 'json' else {}
        source_digest = digest({k: hashlib.sha256(v).hexdigest() for k,v in files.items()}) if files else hashlib.sha256(data).hexdigest()
        request_digest = digest([source_digest, options])
        if request_key is not None:
            require(isinstance(request_key, str) and 0 < len(request_key) <= 256)
            if request_key in self.preview_requests:
                previous_digest, preview_id = self.preview_requests[request_key]
                frozen = self.previews.get(preview_id)
                require(frozen is not None and frozen[0] > self.clock(), 'preview:expired')
                target = GroupRef(**frozen[1]['ref'])
                if not frozen[1]['blocked']:
                    self.store.root(target)
                require(previous_digest == request_digest, 'dictionary:request_key_conflict')
                return plain(frozen[1])
        raw = parse_json(files['group.json'] if files else data)
        metadata, manifest = {}, {}
        if isinstance(raw, dict) and (level == 'group' or format != 'json'):
            allowed = {'group_metadata', 'prompts'} | ({'preview_manifest'} if format != 'json' else set())
            if 'prompts' not in raw:
                raise PromptError('schema:invalid', issues=(Issue('schema:invalid', message='请使用键名 prompts', field='prompts'),))
            require(not set(raw)-allowed, 'schema:invalid')
            metadata = raw.get('group_metadata', {})
            manifest = raw.get('preview_manifest', {})
            rows = raw['prompts']
        else:
            rows = [raw] if isinstance(raw, dict) and level == 'entries' else raw
        require(isinstance(metadata, dict) and set(metadata) <= {'format_version', 'group_id', 'name', 'nsfw', 'scope', 'category'})
        require(type(metadata.get('format_version', 1)) is int)
        require(metadata.get('format_version', 1) == 1, 'newer_version')
        if metadata:
            Group(metadata.get('group_id', str(uuid4())), metadata.get('name', '导入提示词组'),
                  metadata.get('scope', 'global'), metadata.get('nsfw', False), category=metadata.get('category', 'full_content'))
        require(isinstance(rows, list) and bool(rows), 'import:empty')
        require(len(rows) <= ENTRY_LIMIT, 'import:limit')
        require(isinstance(manifest, dict))
        if format != 'json':
            declared = {'group.json'}
            for identity, spec in manifest.items():
                identifier(identity)
                require(isinstance(spec, dict) and set(spec) == {'file', 'sha256'})
                require(spec['file'] == 'previews/'+identity+'.webp', 'import:unsafe_archive')
                require(spec['file'] in files, 'image:not_found')
                require(hashlib.sha256(files[spec['file']]).hexdigest() == spec['sha256'], 'image:invalid')
                prepare_preview(files[spec['file']], identity)
                declared.add(spec['file'])
            require(set(files) == declared, 'import:unsafe_archive')
            require(set(manifest) <= {r.get('entry_id') for r in rows if isinstance(r, dict)}, 'schema:invalid')
        override = options.get('metadata', {})
        require(isinstance(override, dict) and set(override) <= {'group_id', 'name', 'nsfw', 'scope', 'category'})
        metadata = metadata | override
        current = self.store.project()
        target = None
        if level == 'entries':
            require('ref' in options)
            target = GroupRef.parse(options['ref']); group = self.store.load(target)
            revision = group.revision
        else:
            scope = metadata.get('scope', 'project' if current else 'global')
            group = Group(metadata.get('group_id', str(uuid4())), metadata.get('name', '导入提示词组'), scope,
                          metadata.get('nsfw', False), category=metadata.get('category', 'full_content'))
            revision = 0
            target = GroupRef(scope, current[0] if scope == 'project' and current else '' if scope == 'project' else None, group.dictionary_id)
        issues, warnings, entries, assets = [], [], [], {}
        blocked = target.scope == 'project' and not current
        original_id = group.dictionary_id
        if not revision and not blocked and self.store.path(target).exists():
            group = replace(group, dictionary_id=str(uuid4()))
            target = replace(target, group_id=group.dictionary_id)
        used_ids = set()
        for row_number, row in enumerate(rows, 1):
            try:
                require(isinstance(row, dict))
                if format == 'json':
                    require(not set(row)-{'tag', 'expand_nl', 'nl', 'disable', 'aspect'})
                else:
                    identifier(row.get('entry_id'))
                original_entry_id = row.get('entry_id')
                if options.get('entry_ids') is not None and original_entry_id not in options['entry_ids']:
                    continue
                entry = validate_entry(row)
                if options.get('source_format') == 'nai':
                    converted, notes = convert_entry(row)
                    entry = validate_entry(converted); warnings.extend(notes)
                require(entry.entry_id not in used_ids, 'entry:id_conflict')
                used_ids.add(entry.entry_id)
                if revision:
                    entry = replace(entry, entry_id=str(uuid4()))
                if original_entry_id in manifest:
                    assets[entry.entry_id] = prepare_preview(files[manifest[original_entry_id]['file']], entry.entry_id)
                entries.append(entry)
            except PromptError as exc:
                # Broken declared images reject the whole package, not just a row.
                if exc.code.startswith('image:') or exc.code == 'import:unsupported':
                    raise
                issues.extend(plain(replace(i, row=row_number)) for i in exc.issues or (Issue(exc.code),))
        if not entries:
            raise PromptError('import:empty', issues=tuple(Issue(**i) for i in issues))
        preview = dict(preview_id=str(uuid4()), ref=plain(target), base_revision=revision,
                       entries=plain(entries), issues=issues, warnings=warnings, valid_count=len(entries),
                       skipped_count=len(issues and {i['row'] for i in issues} or ()),
                       metadata=dict(name=group.name, nsfw=group.nsfw, scope=group.scope),
                       id_mapping={original_id: group.dictionary_id}, blocked=blocked)
        preview['digest'] = digest(preview)
        self.previews[preview['preview_id']] = (self.clock()+900, preview, tuple(entries), assets, group)
        if request_key is not None:
            self.preview_requests[request_key] = (request_digest, preview['preview_id'])
        return plain(preview)

    def commit_import(self, preview_id, signature, expected_revision, request_key, ref=None):
        frozen = self.previews.get(preview_id)
        require(frozen is not None and frozen[0] > self.clock(), 'preview:expired')
        _, preview, entries, assets, template = frozen
        require(signature == preview['digest'], 'dictionary:request_key_conflict')
        target = GroupRef(**preview['ref'])
        require(ref is None or plain(ref) == plain(target), 'completion:stale')
        self.store.root(target)
        require(expected_revision == preview['base_revision'], 'dictionary:revision_conflict')
        def change(old):
            require(bool(old) == bool(expected_revision), 'group:id_conflict')
            current = list(old.entries) if old else []
            inserted = duplicates = 0
            conflicts = []
            for entry in entries:
                key = lambda e: (tuple(sorted(e.tag)), e.expand_nl, e.nl, e.aspect)
                match = next((e for e in current if key(e) == key(entry)), None)
                if match:
                    if match.disable != entry.disable:
                        conflicts.append(dict(code='entry:disable_conflict', entry_id=match.entry_id))
                    else:
                        duplicates += 1
                    continue
                if entry.entry_id in assets:
                    entry = replace(entry, preview=self.store.publish(assets[entry.entry_id], target, entry.entry_id))
                current.append(entry); inserted += 1
            group = replace(old or template, entries=tuple(current))
            return group, dict(ref=plain(target), inserted=inserted, duplicates=duplicates, conflicts=conflicts)
        return self.store.mutate(target, expected_revision, request_key, 'import', {'preview_id': preview_id, 'digest': signature}, change)

    def export_group(self, ref, format='json'):
        require(format in ('json', 'folder', 'zip'))
        with self.store.lease(ref) as group:
            metadata = dict(format_version=1, group_id=group.dictionary_id, name=group.name,
                            nsfw=group.nsfw, scope=group.scope, category=group.category)
            raw = dict(group_metadata=metadata, prompts=[content(e) for e in group.entries])
            if format == 'json':
                return json.dumps(raw, ensure_ascii=False).encode('utf-8')
            files, manifest = {}, {}
            for row, entry in zip(raw['prompts'], group.entries):
                row['entry_id'] = entry.entry_id
                if entry.preview:
                    data, sha = self.store.image(ref, entry)
                    name = 'previews/'+entry.entry_id+'.webp'
                    files[name] = data; manifest[entry.entry_id] = dict(file=name, sha256=sha)
            raw['preview_manifest'] = manifest
            files['group.json'] = json.dumps(raw, ensure_ascii=False).encode('utf-8')
            # Stored members avoid rejecting our own highly-compressible JSON on import.
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            if format == 'folder':
                handle = str(uuid4())
                self.artifacts[handle] = (self.clock()+900, stream.getvalue())
                return dict(handle=handle, download_url='/api/prompt-artifacts/'+handle,
                            format='folder', transport='zip', revision=group.revision)
            return stream.getvalue()
