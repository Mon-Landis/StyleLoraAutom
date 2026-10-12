"""Read-only evidence probe. No generation retry or gallery scan exists here."""
import asyncio
import json
import weakref
from dataclasses import replace
from uuid import uuid4
from stylelora.backends.types import BackendError, digest
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db import receipts
from stylelora.project_db.recovery import inspect_sent as inspect_local, reconcile_item
from stylelora.project_db.repositories import sources
from stylelora.project_db.types import RecoveryEvidence
from stylelora.project_db.validation import relative_path
from .profile import from_snapshot, build_request
from .artifacts import read_attempt, request_digest, originals_for, save_validation, write_receipt
from .metadata import inspect_png

_gates = weakref.WeakKeyDictionary()


def source_gate(project, source_id):
    return _gates.setdefault(project.writer, {}).setdefault(source_id, asyncio.Lock())


async def probe(project, item, backend=None):
    with read_snapshot(project) as db:
        source = sources.get_source(db, item.source_id)
    request = build_request(from_snapshot(source.config_json), source, item, project.root)
    try:
        receipt = read_attempt(project.root, item.item_id, max(1, item.attempts))
        if (receipt.source_id != item.source_id or receipt.config_digest != digest(source.config_json)
                or receipt.request_digest != request_digest(request, project.root)):
            raise BackendError('artifact:ownership_conflict', 'recovery')
        if receipt.backend_instance is not None and backend is not None and receipt.backend_instance != getattr(backend, 'instance', backend.__class__.__name__):
            return RecoveryEvidence(item.item_id, item.attempts, 'unknown', reason='recovery:unknown')
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
        if (receipt.complete and receipt.sha256) or (receipt.phase == 'submitted' and receipt.resource_id):
            path = relative_path(project.root, receipt.relative_staging_path)
            inspection = inspect_png(path, style_prompt=source.style_prompt, sent_prompt=request.positive,
                metadata_stripped=receipt.metadata_stripped, expected_size=(request.width, request.height))
            if receipt.phase == 'submitted' or (inspection.sha256, inspection.file_size) == (receipt.sha256, receipt.file_size):
                from stylelora.project_db.types import ImageArtifact
                from .artifacts import inspection_summary
                from stylelora.backends.types import plain
                artifact = ImageArtifact(item.item_id, max(1, item.attempts), receipt.relative_staging_path, '.png',
                    inspection.width, inspection.height, inspection.phash, inspection.sha256, inspection.file_size,
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
        return RecoveryEvidence(item.item_id, item.attempts, 'unknown', reason='recovery:unknown')
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
    if receipt.phase == 'submitted':
        receipt = replace(receipt, phase='complete', complete=True, sha256=inspection.sha256,
            file_size=inspection.file_size, width=inspection.width, height=inspection.height,
            validation_codes=inspection.reason_codes)
        write_receipt(project.root, receipt)
    save_validation(project.root, receipt, inspection)
    artifact = receipts.prepare_receipt(project.root, evidence.artifact, originals_for(item.prompt, inspection))
    if artifact != evidence.artifact:
        raise BackendError('artifact:ownership_conflict', 'recovery')


async def recover_source(project, source_id, backend=None):
    async with source_gate(project, source_id):
        from stylelora.sources.runner import _controllers
        runner = _controllers.get((id(project.writer), source_id))
        if runner is not None and runner.task is not None and not runner.task.done():
            raise BackendError('profile:restore_pending', 'recovery')
        from stylelora.project_db import commands
        with read_snapshot(project) as db:
            source = sources.get_source(db, source_id)
        if source.status == 'running':
            await asyncio.wrap_future(submit_write(project, lambda db: commands.transition_source(
                db, source_id, expected='running', target='paused', reason='generation:interrupted')))
        return await recover_locked(project, source_id, backend)


async def recover_locked(project, source_id, backend=None):
    """Caller holds the same gate used for starting and dispatching a cycle."""
    from stylelora.sources.cost import all_items
    from stylelora.project_db.migrations import atomic_json
    cycle = project.writer.cycles.get(source_id)
    token = uuid4().hex
    marker = relative_path(project.root, f'images/.pending/recovery-{source_id}.json')
    marker.parent.mkdir(parents=True, exist_ok=True)
    items = all_items(project, source_id, ('sent', 'done'))
    bindings = []
    for item in items:
        try:
            receipt = read_attempt(project.root, item.item_id, item.attempts)
            bindings.append({'item_id': item.item_id, 'attempt': item.attempts,
                'source_id': receipt.source_id, 'cycle_id': receipt.cycle_id,
                'run_token': receipt.run_token, 'backend_instance': receipt.backend_instance,
                'resource_id': receipt.resource_id, 'request_digest': receipt.request_digest})
        except BackendError:
            bindings.append({'item_id': item.item_id, 'attempt': item.attempts})
    record = {'source_id': source_id, 'run_token': token,
        'cycle_id': cycle[0] if cycle else None,
        'backend_instance': getattr(backend, 'instance', None),
        'attempts': bindings}
    fingerprint = digest(record)
    atomic_json(marker, {**record, 'digest': fingerprint})

    def check_run():
        try:
            current = json.loads(marker.read_text(encoding='utf-8'))
            if current.pop('digest') != fingerprint or digest(current) != fingerprint:
                raise ValueError()
            if project.writer.cycles.get(source_id) != cycle:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise BackendError('profile:restore_pending', 'recovery') from None

    unresolved = []
    for item in items:
        if item.status == 'done' and not receipts.receipt_path(project.root, item.item_id, item.attempts).exists():
            continue
        if backend is not None and hasattr(backend, 'recover_artifact'):
            try:
                receipt = read_attempt(project.root, item.item_id, item.attempts)
                with read_snapshot(project) as db:
                    source = sources.get_source(db, source_id)
                request = build_request(from_snapshot(source.config_json), source, item, project.root)
                if (receipt.source_id == source_id and receipt.request_digest == request_digest(request, project.root)
                        and receipt.config_digest == digest(source.config_json)):
                    await backend.recover_artifact(request, receipt)
            except BackendError:
                pass
        evidence = await probe(project, item, backend)
        check_run()
        if evidence.verdict == 'unknown':
            unresolved.append(item.item_id)
        else:
            prepare_reconciliation(project, item, evidence)
            def commit(db, evidence=evidence):
                check_run()
                return reconcile_item(db, evidence)
            await asyncio.wrap_future(submit_write(project, commit))
    if not unresolved:
        check_run()
        marker.unlink(missing_ok=True)
    return tuple(unresolved)
