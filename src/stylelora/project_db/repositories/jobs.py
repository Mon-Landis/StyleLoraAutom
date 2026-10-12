from ..errors import SnapshotMismatch, StateConflict, ValidationError
from collections.abc import Sequence
from ..connection import ReadSession, WriteSession
from ..types import Page, JobStatus
from ..types import JobRow, JobRequest, JobSnapshot, PageRequest, WriteResult
from ..validation import choice, dumps, integer, now, safe_snapshot, scope_json, text
from .common import get, page as paginate, row, cas
from .images import get_image


def get_job(db: ReadSession, job_id: int) -> JobRow:
    return get(db, 'jobs', JobRow, job_id)


def list_jobs(db: ReadSession, *, status: JobStatus | None = None, batch_id: str | None = None, page: PageRequest) -> Page[JobRow]:
    return paginate(db, 'jobs', JobRow, page, lambda r: (status is None or r.status == status) and (batch_id is None or r.batch_id == batch_id))


def create_batch(db: WriteSession, batch_id: str, requests: Sequence[JobRequest]) -> tuple[JobRow, ...]:
    text(batch_id, nonempty=True)
    if not requests:
        raise ValidationError('Empty batch')
    requests = sorted(requests, key=lambda r: ('wd','llm_vision').index(r.executor) if r.executor in ('wd','llm_vision') else 2)
    for request in requests:
        choice(request.executor, ('wd','llm_vision'))
        choice(request.mode, ('selected','uncovered','all'))
        scope_json(request.scope)
    existing = tuple(row(JobRow,r) for r in db.execute('SELECT * FROM jobs WHERE batch_id=? ORDER BY job_id', (batch_id,)))
    if existing:
        if [(r.executor,r.mode,dumps(r.scope_json)) for r in existing] != [(r.executor,r.mode,scope_json(r.scope)) for r in requests]:
            raise StateConflict('Batch request differs')
        return existing
    result = []
    for request in requests:
        clock = now()
        cursor = db.execute('INSERT INTO jobs(executor,batch_id,mode,scope_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                            (request.executor,batch_id,request.mode,scope_json(request.scope),clock,clock))
        result.append(get_job(db,cursor.lastrowid))
    return tuple(result)


def freeze_job(db: WriteSession, job_id: int, snapshot: JobSnapshot, *, image_ids: Sequence[int]) -> WriteResult[JobRow]:
    safe_snapshot(snapshot)
    # Concrete executor payload keys were unspecified. Require the common frozen
    # contract and preserve executor-specific params without guessing their schema.
    required = ('role','model','protocol','params','target_types','skill_hash','concurrency','timeout','retries','routing')
    if any(key not in snapshot for key in required):
        raise ValidationError('Incomplete executor snapshot')
    if not snapshot['model'] or not snapshot['role'] or not snapshot['protocol']:
        raise ValidationError('Missing frozen model identity')
    targets = snapshot['target_types']
    if not isinstance(targets,list) or not targets or len(set(targets)) != len(targets) or any(t not in ('tags','nl') for t in targets):
        raise ValidationError('Invalid target layers')
    integer(snapshot['concurrency'],1)
    integer(snapshot['retries'])
    integer(snapshot['timeout'],1)
    ids = list(image_ids)
    if len(set(ids)) != len(ids):
        raise ValidationError('Duplicate frozen image')
    for identity in ids:
        get_image(db, identity)
    job = get_job(db,job_id)
    if job.executor == 'wd' and targets != ['tags']:
        raise ValidationError('WD produces tags only')
    if job.status != 'pending':
        if job.config_snapshot_json == snapshot and job.progress_json['image_ids'] == ids:
            return WriteResult(job,False)
        raise SnapshotMismatch('Frozen job differs')
    clock = now()
    cas(db,'jobs',job_id,{'status':'running','started_at':clock,'updated_at':clock,'total':len(ids),
                        'config_snapshot_json':dumps(snapshot),'progress_json':dumps({'schema_version':1,'image_ids':ids,'outcomes':{}})}, {'status':'pending'})
    return WriteResult(get_job(db,job_id),True)
