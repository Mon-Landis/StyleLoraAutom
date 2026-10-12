"""Read-only evidence probe. No generation retry or gallery scan exists here."""
import asyncio
from stylelora.backends.types import BackendError, digest
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db import receipts
from stylelora.project_db.recovery import inspect_sent as inspect_local, reconcile_item, pause_interrupted
from stylelora.project_db.repositories import sources
from stylelora.project_db.types import RecoveryEvidence
from stylelora.project_db.validation import relative_path
from .profile import from_snapshot, build_request
from .artifacts import read_attempt, request_digest, originals_for
from .metadata import inspect_png


async def probe(project, item, backend=None):
    with read_snapshot(project) as db:
        source = sources.get_source(db, item.source_id)
    request = build_request(from_snapshot(source.config_json), source, item, project.root)
    try:
        receipt = read_attempt(project.root, item.item_id, max(1, item.attempts))
        if (receipt.source_id != item.source_id or receipt.config_digest != digest(source.config_json)
                or receipt.request_digest != request_digest(request, project.root)):
            raise BackendError('artifact:ownership_conflict', 'recovery')
        local = inspect_local(project, item)
        if local.verdict == 'complete':
            record, artifact, _ = receipts.read_receipt(project.root, item.item_id, item.attempts)
            candidates = [relative_path(project.root, artifact.relative_staging_path)]
            for key in ('filename', 'previous_path'):
                if record.get(key):
                    candidates.append(relative_path(project.root / 'images' if key == 'filename' else project.root, record[key]))
            for path in candidates:
                if receipts.file_matches(path, artifact):
                    inspection = inspect_png(path, style_prompt=source.style_prompt, sent_prompt=request.positive,
                        metadata_stripped=receipt.metadata_stripped, expected_size=(request.width, request.height))
                    if inspection.sha256 == receipt.sha256 and receipt.complete:
                        return local
        # Completed identity receipt survives the window before F01 receipt preparation.
        if receipt.complete and receipt.sha256:
            path = relative_path(project.root, receipt.relative_staging_path)
            inspection = inspect_png(path, style_prompt=source.style_prompt, sent_prompt=request.positive,
                metadata_stripped=receipt.metadata_stripped, expected_size=(request.width, request.height))
            if (inspection.sha256, inspection.file_size) == (receipt.sha256, receipt.file_size):
                from stylelora.project_db.types import ImageArtifact
                from .artifacts import inspection_summary
                from stylelora.backends.types import plain
                artifact = ImageArtifact(item.item_id, max(1, item.attempts), receipt.relative_staging_path, '.png',
                    inspection.width, inspection.height, None, inspection.sha256, inspection.file_size,
                    plain(inspection.png_meta), '', inspection_summary(inspection))
                # Probe is read-only; construct the exact F01 receipt digest without publishing it.
                from dataclasses import asdict, replace
                from stylelora.project_db.validation import digest as dal_digest
                payload = asdict(artifact)
                payload.pop('receipt_digest')
                originals = originals_for(item.prompt, inspection)
                fingerprint = dal_digest({'schema_version': 1, 'complete': True, 'artifact': payload,
                                          'originals': [asdict(v) for v in originals]})
                artifact = replace(artifact, receipt_digest=fingerprint)
                return RecoveryEvidence(item.item_id, max(1, item.attempts), 'complete', artifact)
    except Exception:
        receipt = None
    if backend is not None:
        try:
            evidence = await backend.reconcile(item, receipt)
            if (evidence.item_id, evidence.attempt) == (item.item_id, item.attempts):
                return evidence
        except BackendError:
            pass
    return RecoveryEvidence(item.item_id, item.attempts, 'unknown', reason='recovery:unknown')


def prepare_reconciliation(project, item, evidence):
    """Local file publication, separate from read-only probing and DB transactions."""
    if evidence.verdict != 'complete' or receipts.receipt_path(project.root, item.item_id, evidence.attempt).exists():
        return
    with read_snapshot(project) as db:
        source = sources.get_source(db, item.source_id)
    request = build_request(from_snapshot(source.config_json), source, item, project.root)
    receipt = read_attempt(project.root, item.item_id, evidence.attempt)
    inspection = inspect_png(relative_path(project.root, receipt.relative_staging_path),
        style_prompt=source.style_prompt, sent_prompt=request.positive,
        metadata_stripped=receipt.metadata_stripped, expected_size=(request.width, request.height))
    artifact = receipts.prepare_receipt(project.root, evidence.artifact, originals_for(item.prompt, inspection))
    if artifact != evidence.artifact:
        raise BackendError('artifact:ownership_conflict', 'recovery')


async def recover_source(project, source_id, backend=None):
    from stylelora.sources.cost import all_items
    await asyncio.wrap_future(submit_write(project, pause_interrupted))
    unresolved = []
    for item in all_items(project, source_id, ('sent', 'done')):
        if item.status == 'done' and not receipts.receipt_path(project.root, item.item_id, item.attempts).exists():
            continue
        evidence = await probe(project, item, backend)
        if evidence.verdict == 'unknown':
            unresolved.append(item.item_id)
        else:
            prepare_reconciliation(project, item, evidence)
            await asyncio.wrap_future(submit_write(project, lambda db, e=evidence: reconcile_item(db, e)))
    return tuple(unresolved)
