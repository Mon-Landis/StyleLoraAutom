"""Pause-first recovery. Local evidence only; external uncertainty never resubmits."""
from dataclasses import replace

from .types import RecoveryReport, RecoveryEvidence, WriteResult, SourceItemRow
from .types import ProjectHandle
from .connection import WriteSession
from .errors import ArtifactConflict, ValidationError
from .validation import now, relative_path
from .repositories import sources, images
from .repositories.common import row
from .receipts import read_receipt, receipt_path, file_matches


def pause_interrupted(db: WriteSession) -> RecoveryReport:
    source_ids = tuple(r[0] for r in db.execute("SELECT source_id FROM sources WHERE status='running' ORDER BY source_id"))
    job_ids = tuple(r[0] for r in db.execute("SELECT job_id FROM jobs WHERE status='running' ORDER BY job_id"))
    db.execute("UPDATE sources SET status='paused' WHERE status='running'")
    db.execute("UPDATE jobs SET status='paused',updated_at=? WHERE status='running'",(now(),))
    if db.writer:
        db.after_commit.append(db.writer.cycles.clear)
    return RecoveryReport(paused_source_ids=source_ids,paused_job_ids=job_ids)


def inspect_sent(project: ProjectHandle, item: SourceItemRow) -> RecoveryEvidence:
    try:
        receipt,artifact,_ = read_receipt(project.root,item.item_id,item.attempts)
        paths = [relative_path(project.root,artifact.relative_staging_path)]
        if receipt.get('filename'):
            paths.append(relative_path(project.root/'images',receipt['filename'],filename=True))
        if any(file_matches(path,artifact) for path in paths):
            return RecoveryEvidence(item.item_id,item.attempts,'complete',artifact,'local_receipt')
    except (OSError, ValueError, KeyError, TypeError, ArtifactConflict, ValidationError):
        pass
    # Lack of local evidence is not proof that external generation finished.
    return RecoveryEvidence(item.item_id,item.attempts,'unknown',reason='manual_probe_required')


def reconcile_item(db: WriteSession, evidence: RecoveryEvidence) -> WriteResult[SourceItemRow]:
    from .commands import fail_item, complete_item
    item = sources.get_item(db,evidence.item_id)
    if item.attempts != evidence.attempt:
        raise ArtifactConflict('Recovery attempt changed')
    if evidence.verdict == 'unknown':
        return WriteResult(item,False)
    if evidence.verdict == 'absent':
        return fail_item(db,item.item_id,attempt=item.attempts,error='recovery:no_complete_artifact')
    if evidence.verdict != 'complete' or evidence.artifact is None:
        raise ValidationError('Invalid recovery evidence')
    _,_,originals = read_receipt(db.root,item.item_id,item.attempts)
    result = complete_item(db,evidence.artifact,originals)
    return WriteResult(sources.get_item(db,item.item_id),result.changed)


def recover_project(project: ProjectHandle) -> RecoveryReport:
    from .connection import read_snapshot
    report = project.writer.submit(pause_interrupted).result()
    recovered,failed,unresolved,missing = [],[],[],[]
    with read_snapshot(project) as db:
        items = tuple(row(SourceItemRow,r) for r in db.execute("SELECT * FROM source_items WHERE status='sent' OR (status='done' AND attempts>0) ORDER BY item_id"))
    for item in items:
        if item.status == 'done' and not receipt_path(project.root,item.item_id,item.attempts).exists():
            continue
        evidence = inspect_sent(project,item)
        if evidence.verdict == 'complete':
            try:
                project.writer.submit(lambda db,e=evidence: reconcile_item(db,e)).result()
            except ArtifactConflict:
                # A receipt with contested ownership is unresolved, not grounds
                # to prevent opening the project or to adopt another image.
                unresolved.append(item.item_id)
            else:
                recovered.append(item.item_id)
        elif evidence.verdict == 'absent':
            project.writer.submit(lambda db,e=evidence: reconcile_item(db,e)).result()
            failed.append(item.item_id)
        else:
            unresolved.append(item.item_id)
    def audit(db):
        known = set()
        for value in db.execute('SELECT image_id,filename FROM images ORDER BY image_id'):
            known.add(value['filename'])
            if not relative_path(project.root/'images',value['filename'],filename=True).is_file():
                images.set_file_missing(db,value['image_id'],missing=True)
                missing.append(value['image_id'])
        return known
    known = project.writer.submit(audit).result()
    orphan = tuple(sorted(p.name for p in (project.root/'images').iterdir() if p.is_file() and p.name not in known))
    return replace(report,recovered_item_ids=tuple(recovered),failed_item_ids=tuple(failed),unresolved_item_ids=tuple(unresolved),missing_image_ids=tuple(missing),orphan_filenames=orphan)
