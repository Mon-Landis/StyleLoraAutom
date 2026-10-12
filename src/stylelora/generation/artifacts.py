"""F05 identity journal wraps, and never replaces, the F01 publication receipt."""
import asyncio
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from stylelora.backends.types import Immutable, BackendError, digest, plain
from stylelora.project_db import submit_write
from stylelora.project_db import commands, receipts
from stylelora.project_db.migrations import atomic_json
from stylelora.project_db.types import ImageArtifact, OriginalInput
from stylelora.project_db.validation import relative_path
from stylelora.tagging.parser import parse_original_prompt
from .metadata import inspect_png


@dataclass(frozen=True)
class AttemptReceipt(Immutable):
    source_id: int
    item_id: int
    attempt: int
    request_digest: str
    config_digest: str
    relative_staging_path: str
    phase: str = 'intent'
    resource_id: str | None = None
    sha256: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    complete: bool = False
    candidate_image_id: int | None = None
    target_filename: str | None = None
    metadata_stripped: bool | None = None
    validation_codes: tuple[str, ...] = ()
    schema_version: int = 1
    cycle_id: str | None = None
    run_token: str | None = None
    backend_instance: str | None = None
    saved_path: str | None = None


def request_digest(request, root):
    value = plain(request)
    value['save_path'] = request.save_path.relative_to(root).as_posix()
    return digest(value)


def attempt_path(root, item_id, attempt):
    return relative_path(root, f'images/.pending/{item_id}/{attempt}/attempt.json')


def write_receipt(root, receipt):
    expected = f'images/.pending/{receipt.item_id}/{receipt.attempt}/out.png'
    if receipt.relative_staging_path != expected or receipt.schema_version != 1:
        raise BackendError('artifact:ownership_conflict', 'receipt')
    relative_path(root, expected)
    value = plain(receipt)
    value['digest'] = digest(value)
    path = attempt_path(root, receipt.item_id, receipt.attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = read_attempt(root, receipt.item_id, receipt.attempt)
        identity = ('source_id', 'item_id', 'attempt', 'request_digest', 'config_digest', 'relative_staging_path')
        if old.phase != 'intent':
            identity += ('cycle_id', 'run_token', 'backend_instance', 'resource_id')
        if any(getattr(old, k) != getattr(receipt, k) for k in identity) or (old.complete and old != receipt):
            raise BackendError('artifact:ownership_conflict', 'receipt')
    atomic_json(path, value)
    return value['digest']


def read_attempt(root, item_id, attempt):
    try:
        value = json.loads(attempt_path(root, item_id, attempt).read_text(encoding='utf-8'))
        fingerprint = value.pop('digest')
        if digest(value) != fingerprint:
            raise ValueError()
        receipt = AttemptReceipt(**value)
        if (receipt.item_id, receipt.attempt, receipt.schema_version) != (item_id, attempt, 1):
            raise ValueError()
        if receipt.relative_staging_path != f'images/.pending/{item_id}/{attempt}/out.png':
            raise ValueError()
        if receipt.phase not in ('intent', 'submitted', 'complete') or receipt.complete != (receipt.phase == 'complete'):
            raise ValueError()
        if receipt.saved_path not in (None, receipt.relative_staging_path):
            raise ValueError()
        relative_path(root, receipt.relative_staging_path)
        return receipt
    except (OSError, ValueError, TypeError, KeyError):
        raise BackendError('artifact:ownership_conflict', 'receipt') from None


def inspection_summary(inspection):
    return {'schema_version': 1, 'validation': inspection.validation, 'reason_codes': list(inspection.reason_codes)}


def save_validation(root, receipt, inspection):
    path = relative_path(root, f'images/.pending/{receipt.item_id}/{receipt.attempt}/validation.json')
    atomic_json(path, {**inspection_summary(inspection), 'sha256': inspection.sha256,
        'source_id': receipt.source_id, 'item_id': receipt.item_id, 'attempt': receipt.attempt,
        'resource_id': receipt.resource_id, 'saved_path': receipt.relative_staging_path,
        'cycle_id': receipt.cycle_id, 'run_token': receipt.run_token,
        'backend_instance': receipt.backend_instance})


def originals_for(prompt, inspection, catalog=None):
    params = {'schema_version': 1, 'generation_validation': inspection_summary(inspection)}
    if catalog is None or not catalog.available:
        return (OriginalInput('nl', prompt, params=params),) if prompt.strip() else ()
    parsed = parse_original_prompt(prompt, catalog=catalog)
    values = []
    if parsed.tags:
        values.append(OriginalInput('tags', parsed.tags, params=params))
    if parsed.natural_language:
        values.append(OriginalInput('nl', parsed.natural_language, params=params))
    return tuple(values)


def stage_result(root, request, result):
    expected = relative_path(root, f'images/.pending/{request.item_id}/{request.attempt}/out.png')
    if request.save_path.resolve() != expected.resolve():
        raise BackendError('artifact:ownership_conflict', 'artifact')
    if result.saved_path is not None and result.saved_path.resolve() != expected.resolve():
        raise BackendError('artifact:ownership_conflict', 'artifact')
    if result.saved_path is None and result.image_bytes is not None:
        expected.parent.mkdir(parents=True, exist_ok=True)
        try:
            with expected.open('xb') as stream:
                stream.write(result.image_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise BackendError('artifact:save_path_exists', 'artifact') from None
    if not expected.is_file():
        raise BackendError('artifact:io_error', 'artifact')
    return expected


def record_submission(root, request, result):
    """Persist remote identity before export, decoding, or publication can fail."""
    receipt = read_attempt(root, request.item_id, request.attempt)
    if result.saved_path is not None and result.saved_path.resolve() != request.save_path.resolve():
        raise BackendError('artifact:ownership_conflict', 'artifact', 'unknown')
    if receipt.resource_id is not None and receipt.resource_id != result.resource_id:
        raise BackendError('artifact:ownership_conflict', 'artifact', 'unknown')
    if receipt.phase == 'intent':
        receipt = replace(receipt, phase='submitted', resource_id=result.resource_id,
            saved_path=receipt.relative_staging_path if result.saved_path is not None else None,
            metadata_stripped=result.metadata_stripped)
        write_receipt(root, receipt)
    return receipt


async def register_artifact(project, inspection, receipt, originals):
    if not receipt.complete or (receipt.sha256, receipt.width, receipt.height, receipt.file_size) != (inspection.sha256, inspection.width, inspection.height, inspection.file_size):
        raise BackendError('artifact:ownership_conflict', 'artifact')
    summary = inspection_summary(inspection)
    # Durable independent check record is also used when there are no originals.
    save_validation(project.root, receipt, inspection)
    artifact = ImageArtifact(receipt.item_id, receipt.attempt, receipt.relative_staging_path,
        '.png', inspection.width, inspection.height, inspection.phash, inspection.sha256,
        inspection.file_size, plain(inspection.png_meta), '', summary)
    artifact = receipts.prepare_receipt(project.root, artifact, tuple(originals))
    result = await asyncio.wrap_future(submit_write(project, lambda db: commands.complete_item(db, artifact, tuple(originals))))
    try:
        attempt_path(project.root, receipt.item_id, receipt.attempt).unlink(missing_ok=True)
    except OSError:
        pass  # confirmed DB result stays successful; subsequent recovery can clean up
    return result.value


async def accept_result(project, source, item, request, result, *, catalog=None):
    receipt = record_submission(project.root, request, result)
    path = stage_result(project.root, request, result)
    inspection = inspect_png(path, style_prompt=source.style_prompt, sent_prompt=request.positive,
        metadata_stripped=result.metadata_stripped, expected_size=(request.width, request.height), catalog=catalog)
    receipt = replace(receipt, phase='complete', complete=True, resource_id=result.resource_id,
        sha256=inspection.sha256, width=inspection.width, height=inspection.height, file_size=inspection.file_size,
        metadata_stripped=result.metadata_stripped, validation_codes=inspection.reason_codes)
    write_receipt(project.root, receipt)
    return await register_artifact(project, inspection, receipt, originals_for(item.prompt, inspection, catalog))
