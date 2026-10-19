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
from .group_legacy import legacy_texts, legacy_content_issues, variant_issues, nl_fallback
from .prompt_normalize import normalize_prompt
from .group_store import (JSON_LIMIT, ZIP_LIMIT, EXPANDED_LIMIT, IMAGE_LIMIT,
                          ENTRY_LIMIT, prepare_preview)
from .dictionaries import LOCK, digest
from .types import PromptError, Issue, plain
from .validation import require, strict_loads, identifier
from .group_txt import encode_group_txt, decode_group_txt
from .group_image_pack import read_image_pack, validate_image_pack, write_image_pack

PREVIEW_CACHE_COUNT = 32
PREVIEW_CACHE_BYTES = 128 * 1024**2


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
    return normalize_entry(raw, 'nai')


def normalize_entry(raw, source):
    result = dict(raw); warnings = []
    def convert(text, field):
        try:
            converted = normalize_prompt(text, source)
        except PromptError as exc:
            raise PromptError(exc.code, issues=tuple(replace(i, field=field) for i in
                              exc.issues or (Issue(exc.code),))) from None
        warnings.extend(dict(plain(i), field=field, severity='warning') for i in converted.warnings)
        return converted.text
    require(isinstance(result.get('nl'), str))
    result['nl'] = convert(result['nl'], 'nl')
    variants = result.get('variants', {})
    require(isinstance(variants, dict))
    require(all(isinstance(v, str) for v in variants.values()))
    result['variants'] = {k: convert(v, f'variants[{k!r}]') if v else v for k, v in variants.items()}
    return result, warnings


class GroupExchange:
    def __init__(self, store, clock=time.monotonic):
        self.store, self.clock, self.previews, self.artifacts = store, clock, {}, {}
        self.preview_requests = {}

    @property
    def preview_bytes(self):
        # Count both frozen text representations and full/thumbnail payloads.
        return sum(len(json.dumps(plain((p[1], p[2], p[4])), ensure_ascii=False).encode('utf-8'))
                   + sum(len(a.full) + len(a.thumb) for a in p[3].values())
                   for p in self.previews.values())

    def _sweep_previews(self):
        now = self.clock()
        for identity, frozen in list(self.previews.items()):
            if frozen[0] <= now:
                del self.previews[identity]
        self.preview_requests = {k: v for k, v in self.preview_requests.items()
                                 if v[1] in self.previews}

    def _cache_preview(self, identity, frozen):
        self._sweep_previews()
        self.previews[identity] = frozen
        # FIFO eviction bounds retained text and images across all import formats.
        # An individual oversized preview is rejected after evicting older items.
        while len(self.previews) > PREVIEW_CACHE_COUNT or self.preview_bytes > PREVIEW_CACHE_BYTES:
            del self.previews[next(iter(self.previews))]
        self._sweep_previews()
        require(identity in self.previews, 'import:limit')

    def preview_import(self, data, options=None, request_key=None):
        options = options or {}
        if isinstance(options, dict) and options.get('format') == 'nai_images':
            return self.preview_images(data, options, request_key)
        return self._preview_import(data, options, request_key)

    def _preview_import(self, data, options, request_key=None, *, decoded=None):
        self._sweep_previews()
        # Trusted image rows bypass the JSON upload byte cap; the image service
        # has already enforced its own batch cap. Row normalization stays shared.
        require(isinstance(options, dict) and set(options) <= {'format', 'level', 'ref', 'metadata', 'source_format', 'entry_ids'})
        format = options.get('format', 'json')
        level = options.get('level', 'group')
        require(format in ('json', 'txt', 'folder', 'zip', 'nai_image_zip') and level in ('group', 'entries'))
        require(options.get('source_format', 'anima') in ('anima', 'nai'))
        package = format in ('folder', 'zip', 'nai_image_zip')
        files = read_image_pack(data) if format == 'nai_image_zip' else read_package(data) if package else {}
        source_digest = digest({k: hashlib.sha256(v).hexdigest() for k,v in files.items()}) if files else hashlib.sha256(data).hexdigest()
        request_digest = digest([source_digest, options, 'nai-storage-v1'])
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
        diagnostics = decode_group_txt(data) if format == 'txt' else {}
        if decoded is not None:
            raw = decoded
        else:
            raw = ({k: diagnostics[k] for k in ('group_metadata', 'prompts')} if format == 'txt'
                   else parse_json(files['group.json'] if files else data))
        metadata, manifest = {}, {}
        if isinstance(raw, dict) and (level == 'group' or format != 'json' or 'prompts' in raw):
            allowed = {'group_metadata', 'prompts'} | ({'preview_manifest'} if package else set())
            if 'prompts' not in raw:
                raise PromptError('schema:invalid', issues=(Issue('schema:invalid', message='请使用键名 prompts', field='prompts'),))
            require(not set(raw)-allowed, 'schema:invalid')
            metadata = raw.get('group_metadata', {})
            manifest = raw.get('preview_manifest', {})
            rows = raw['prompts']
        else:
            rows = [raw] if isinstance(raw, dict) and level == 'entries' else raw
        require(isinstance(metadata, dict) and set(metadata) <= {'format_version', 'group_id', 'name', 'nsfw', 'scope', 'category', 'auto_complete', 'prompt_format', 'normalization_version'})
        require(type(metadata.get('format_version', 1)) is int)
        version = metadata.get('format_version', 1)
        require(version in (1, 2), 'newer_version')
        if version == 2:
            require(metadata.get('prompt_format') == 'nai')
            require(metadata.get('normalization_version') == 'nai-storage-v1')
            require(options.get('source_format', 'nai') == 'nai')
        source = 'nai' if version == 2 else options.get('source_format', 'anima')
        if metadata:
            Group(metadata.get('group_id', str(uuid4())), metadata.get('name', '导入提示词组'),
                  metadata.get('scope', 'global'), metadata.get('nsfw', False), category=metadata.get('category', 'full_content'))
        require(isinstance(rows, list), 'schema:invalid')
        require(len(rows) <= ENTRY_LIMIT, 'import:limit')
        require(isinstance(manifest, dict))
        if format == 'nai_image_zip':
            validate_image_pack(files, raw)
        elif package:
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
        issues, warnings, entries, assets = diagnostics.get('issues', []), diagnostics.get('warnings', []), [], {}
        blocked = target.scope == 'project' and not current
        original_id = group.dictionary_id
        if not revision and not blocked and self.store.path(target).exists():
            group = replace(group, dictionary_id=str(uuid4()))
            target = replace(target, group_id=group.dictionary_id)
        used_ids = set()
        for row_number, row in enumerate(rows, 1):
            if format == 'txt':
                row_number = diagnostics['row_numbers'][row_number-1]
            try:
                require(isinstance(row, dict))
                if package:
                    identifier(row.get('entry_id'))
                original_entry_id = row.get('entry_id')
                if options.get('entry_ids') is not None and original_entry_id not in options['entry_ids']:
                    continue
                if version == 1:
                    allowed = {'nl', 'tag', 'expand_nl', 'variants', 'aspect', 'disable'}
                    require(set(row) <= allowed | ({'entry_id'} if package else set()))
                    texts = legacy_texts(row)
                    notes = list(legacy_content_issues(row)) + list(variant_issues(row))
                    variants = dict(row.get('variants') or {})
                    for legacy in ('tag', 'expand_nl'):
                        # Conflicts keep the existing value; the 64-key limit drops the extra.
                        if legacy in variants or not texts[legacy].strip() or len(variants) >= 64:
                            continue
                        variants[legacy] = texts[legacy]
                    fallback = nl_fallback(texts)
                    if fallback is not None:
                        # v5 requires nl: same order as the group migration.
                        texts['nl'] = texts[fallback]
                        notes.append(Issue('migration:nl_fallback', field=row.get('entry_id'),
                            severity='warning', message=f'source={fallback}'))
                    warnings.extend(plain(replace(i, row=row_number)) for i in notes)
                    warnings.append(plain(Issue('import:legacy_format', row=row_number,
                        severity='warning', message=f'旧格式适配；来源 {source}。')))
                    row = {k: v for k, v in row.items() if k not in ('tag', 'expand_nl')}
                    row['nl'], row['variants'] = texts['nl'], variants
                converted, notes = normalize_entry(row, source)
                entry = validate_entry(converted, allow_id=package)
                warnings.extend(dict(i, row=row_number) for i in notes)
                require(entry.entry_id not in used_ids, 'entry:id_conflict')
                used_ids.add(entry.entry_id)
                if revision:
                    entry = replace(entry, entry_id=str(uuid4()))
                if original_entry_id in manifest and not manifest[original_entry_id].get('placeholder', False):
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
                        id_mapping={original_id: group.dictionary_id}, blocked=blocked,
                        normalization_version='nai-storage-v1')
        if format == 'nai_image_zip':
            preview['format'] = format
        preview['digest'] = digest(preview)
        self._cache_preview(preview['preview_id'], (self.clock()+900, preview, tuple(entries), assets, group))
        if request_key is not None:
            self.preview_requests[request_key] = (request_digest, preview['preview_id'])
        return plain(preview)

    def preview_images(self, files, options=None, request_key=None):
        # T10: pass ordered multipart (filename, bytes) pairs, never a name-keyed dict.
        from .group_image_import import preview_images
        return preview_images(self, files, options, request_key)

    def preview_image(self, preview_id, entry_id, size='thumb'):
        """Return frozen sanitized WebP bytes and SHA-256 before confirmation."""
        self._sweep_previews()
        require(size in ('full', 'thumb'))
        frozen = self.previews.get(preview_id)
        require(frozen is not None and frozen[0] > self.clock(), 'preview:expired')
        if not frozen[1]['blocked']:
            self.store.root(GroupRef(**frozen[1]['ref']))
        asset = frozen[3].get(entry_id)
        require(asset is not None, 'image:not_found')
        data = asset.thumb if size == 'thumb' else asset.full
        return data, hashlib.sha256(data).hexdigest()

    def commit_import(self, preview_id, signature, expected_revision, request_key, ref=None):
        self._sweep_previews()
        frozen = self.previews.get(preview_id)
        target = ref or (GroupRef(**frozen[1]['ref']) if frozen else None)
        payload = {'preview_id': preview_id, 'digest': signature}
        require(isinstance(request_key, str) and 0 < len(request_key) <= 256)
        require(type(expected_revision) is int and expected_revision >= 0)
        if target is not None:
            with LOCK:
                raw = self.store._raw(target, absent=True)
                receipt = (raw or {}).get('_operations', {}).get(request_key)
                if receipt is not None:
                    expected_digest = digest(dict(operation='import', payload=payload,
                                                  expected_revision=expected_revision))
                    require(receipt['digest'] == expected_digest, 'dictionary:request_key_conflict')
                    return plain(receipt['result'])
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
            def key(e):
                base = (e.nl, e.aspect, dict(e.variants))
                if preview.get('format') in ('nai_images', 'nai_image_zip'):
                    asset = assets.get(e.entry_id)
                    sha = (hashlib.sha256(asset.full).hexdigest() if asset else
                           e.preview.sha256 if e.preview else None)
                    return base + (e.disable, sha)
                return base
            for entry in entries:
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
        result = self.store.mutate(target, expected_revision, request_key, 'import', {'preview_id': preview_id, 'digest': signature}, change)
        assets.clear()
        return result

    def export_group(self, ref, format='json'):
        require(format in ('json', 'txt', 'folder', 'zip', 'nai_image_zip'))
        with self.store.lease(ref) as group:
            metadata = dict(format_version=2, prompt_format='nai', normalization_version='nai-storage-v1',
                            group_id=group.dictionary_id, name=group.name,
                            nsfw=group.nsfw, scope=group.scope, category=group.category)
            if format == 'nai_image_zip':
                return write_image_pack(self.store, ref, group, metadata)
            if format == 'txt':
                return encode_group_txt(metadata, group.entries)
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
