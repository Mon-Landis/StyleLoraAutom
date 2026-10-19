"""Ordered NAI PNG batch previews; HTTP multipart parsing belongs to the caller."""
import hashlib
from dataclasses import dataclass, replace
from uuid import uuid4

from .dictionaries import digest
from .group_store import PreparedAsset, prepare_preview
from .groups import GroupRef
from .nai_metadata import extract_positive
from .types import Issue, PromptError, plain
from .validation import require

BATCH_BYTES = 100 * 1024**2
BATCH_FILES = 1000


class ImageImportError(PromptError):
    """Batch failure with the same per-file reports as a successful preview."""
    def __init__(self, code, files):
        self.files = plain(files)
        super().__init__(code, issues=tuple(Issue(**{k: v for k, v in issue.items()
            if k != 'file_index'}) for report in files for issue in report['issues']))


@dataclass(frozen=True)
class IndexedAsset(PreparedAsset):
    file_index: int


def preview_images(exchange, files, options=None, request_key=None):
    """Preview ordered ``(name: str, data: bytes)`` pairs using a GroupExchange.

    Limits are checked before extraction. ImageImportError exposes ``files`` on
    import:empty; import:limit is a batch-level error (HTTP 413).
    """
    exchange._sweep_previews()
    require(options is None or isinstance(options, dict))
    options = dict(options or {})
    require(set(options) <= {'format', 'level', 'ref', 'metadata', 'source_format'})
    require(options.get('format', 'nai_images') == 'nai_images')
    require(options.get('source_format', 'nai') == 'nai')
    batch, total = [], 0
    for item in files:
        require(isinstance(item, (tuple, list)) and len(item) == 2)
        name, data = item
        require(isinstance(name, str) and isinstance(data, bytes))
        total += len(data)
        require(len(batch) < BATCH_FILES and total <= BATCH_BYTES, 'import:limit')
        batch.append((name, data))
    signature = digest([[(name, hashlib.sha256(data).hexdigest()) for name, data in batch],
                        options, 'nai-images-v1'])
    if request_key is not None:
        require(isinstance(request_key, str) and 0 < len(request_key) <= 256)
        if request_key in exchange.preview_requests:
            previous, identity = exchange.preview_requests[request_key]
            frozen = exchange.previews.get(identity)
            require(frozen is not None and frozen[0] > exchange.clock(), 'preview:expired')
            require(previous == signature, 'dictionary:request_key_conflict')
            if not frozen[1]['blocked']:
                exchange.store.root(GroupRef(**frozen[1]['ref']))
            return plain(frozen[1])
    reports, rows, prepared, row_files = [], [], [], []
    for index, (name, data) in enumerate(batch, 1):
        extracted = extract_positive(data)
        notes = [dict(plain(replace(i, row=index)), file_index=index) for i in extracted.issues]
        report = dict(file_index=index, name=name, status='skipped', source=extracted.source, issues=notes)
        reports.append(report)
        try:
            if not extracted.text.strip():
                errors = [i for i in extracted.issues if i.severity == 'error']
                code = ('metadata:limit' if any(i.code == 'metadata:limit' for i in errors)
                        else 'image:invalid' if errors else 'metadata:empty')
                raise PromptError(code)
            asset = prepare_preview(data, str(uuid4()))
        except PromptError as exc:
            notes.extend(dict(plain(replace(i, row=index)), file_index=index)
                         for i in exc.issues or (Issue(exc.code),))
            continue
        rows.append({'nl': extracted.text})
        prepared.append(IndexedAsset(asset.full, asset.thumb, asset.width, asset.height, index))
        row_files.append(index)
    if not rows:
        raise ImageImportError('import:empty', reports)
    internal_options = {k: v for k, v in options.items() if k != 'format'} | {'format': 'json', 'source_format': 'nai'}
    payload = dict(group_metadata=dict(format_version=2, prompt_format='nai',
        normalization_version='nai-storage-v1'), prompts=rows)
    try:
        preview = exchange._preview_import(b'', internal_options, decoded=payload)
    except PromptError as exc:
        if exc.code != 'import:empty':
            raise
        for issue in exc.issues:
            index = row_files[issue.row - 1]
            reports[index - 1]['issues'].append(dict(plain(replace(issue, row=index)), file_index=index))
        raise ImageImportError('import:empty', reports) from None
    frozen = exchange.previews.pop(preview['preview_id'])
    failed = {i['row'] for i in preview['issues']}
    for issue in preview['issues'] + preview['warnings']:
        index = row_files[issue['row'] - 1]
        reports[index - 1]['issues'].append(dict(issue, row=index, file_index=index))
    assets = {}
    valid_assets = [asset for row, asset in enumerate(prepared, 1) if row not in failed]
    for entry, asset in zip(preview['entries'], valid_assets, strict=True):
        entry['file_index'] = asset.file_index
        assets[entry['entry_id']] = asset
        reports[asset.file_index - 1].update(status='valid', entry_id=entry['entry_id'])
    preview.update(files=reports, source_format='nai', format='nai_images',
        skipped_count=len(reports) - len(valid_assets),
        issues=[i for r in reports for i in r['issues'] if i['severity'] != 'warning'],
        warnings=[i for r in reports for i in r['issues'] if i['severity'] == 'warning'])
    preview.pop('digest')
    preview['digest'] = digest(preview)
    exchange._cache_preview(preview['preview_id'], (frozen[0], preview, frozen[2], assets, frozen[4]))
    if request_key is not None:
        exchange.preview_requests[request_key] = (signature, preview['preview_id'])
    return plain(preview)
