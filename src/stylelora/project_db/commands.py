"""Atomic business commands; submit each command as one writer task."""
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Literal
from uuid import uuid4
from .connection import WriteSession

from .errors import StateConflict, ValidationError, OutcomeConflict, PreviewStale, UndoConflict, BatchTooLarge
from .types import *
from .validation import choice, digest, dumps, integer, now, safe_snapshot, scope_json, text, error_reason
from .repositories import sources, images, annotations, jobs, operations
from .repositories.common import cas, get

SOURCE_EDGES = {('running','paused'),('paused','running'),('running','done'),('paused','done'),
                ('draft','canceled'),('running','canceled'),('paused','canceled')}
JOB_EDGES = {('running','paused'),('paused','running'),('running','done'),('running','failed'),
             ('running','canceled'),('pending','canceled'),('paused','canceled'),('pending','failed'),
             ('paused','done'),('paused','failed')}


@contextmanager
def authority(db, name):
    old = db.authority
    db.authority = name
    try:
        yield
    finally:
        db.authority = old


def _cycle(db, source_id):
    if db.writer:
        db.after_commit.append(lambda: db.writer.cycles.__setitem__(source_id,(uuid4().hex,False)))


def start_source(db: WriteSession, source_id: int, items: Sequence[ItemSeed]) -> SourceRow:
    source = sources.get_source(db,source_id)
    items = tuple(items)
    if not items or [i.seq for i in items] != list(range(1,len(items)+1)):
        raise ValidationError('Nonempty contiguous ordered seeds required')
    if source.status != 'draft':
        with authority(db,'start_source'):
            existing = sources.seed_items(db,source_id,items)
        count = db.execute('SELECT count(*) FROM source_items WHERE source_id=?',(source_id,)).fetchone()[0]
        if not source.locked or count != len(items):
            raise StateConflict('Start request differs')
        return source
    config = source.config_json
    safe_snapshot(config)
    if source.type == 'import':
        if config.get('reader') != 'txt_sidecar' or not isinstance(config.get('params'),dict):
            raise ValidationError('Import reader snapshot required')
        safe_snapshot(config['params'])
        manifest = config.get('import_manifest')
        # Wire shape is unspecified: use ordered root-relative paths, one per seq.
        if not isinstance(manifest,list) or len(manifest) != len(items):
            raise ValidationError('Complete import manifest required')
        from .validation import relative_path
        for value in manifest:
            relative_path(db.root,value)
    else:
        if not config.get('model') or not isinstance(config.get('params'),dict) or config.get('quality_toggle') is not False or config.get('uc_preset','missing') is not None:
            raise ValidationError('Invalid NAI snapshot')
        choice(config.get('default_aspect'),('landscape','portrait','square'))
        dimensions = config.get('dimensions',{})
        for aspect in ('landscape','portrait','square'):
            pair = dimensions.get(aspect)
            if not isinstance(pair,list) or len(pair) != 2:
                raise ValidationError('Three actual pixel pairs required')
            for size in pair:
                integer(size,64)
                if size % 64:
                    raise ValidationError('Pixel sizes must be multiples of 64')
        if source.target_count != len(items) or any(i.aspect is None for i in items):
            raise ValidationError('NAI target/aspect mismatch')
    with authority(db,'start_source'):
        sources.seed_items(db,source_id,items)
    if db.execute('SELECT count(*) FROM source_items WHERE source_id=?',(source_id,)).fetchone()[0] != len(items):
        raise StateConflict('Extra seeds present')
    cas(db,'sources',source_id,{'status':'running','locked':1,'started_at':now()}, {'status':'draft','locked':0})
    _cycle(db,source_id)
    return sources.get_source(db,source_id)


def transition_source(db: WriteSession, source_id: int, *, expected: SourceStatus, target: SourceStatus, reason: str) -> WriteResult[SourceRow]:
    source = sources.get_source(db,source_id)
    if source.status == target:
        return WriteResult(source,False)
    if source.status != expected or (expected,target) not in SOURCE_EDGES:
        raise StateConflict('Illegal source transition')
    counts = sources.count_item_states(db,source_id)
    if target == 'running' and counts['sent']:
        raise StateConflict('Unresolved sent items')
    if target == 'done' and (counts['pending'] or counts['sent'] or not sum(counts.values())):
        raise StateConflict('Source has unfinished items')
    cas(db,'sources',source_id,{'status':target,'finished_at':now() if target in ('done','canceled') else None},{'status':expected})
    _cycle(db,source_id)
    return WriteResult(sources.get_source(db,source_id),True)


def mark_sent(db: WriteSession, item_id: int) -> WriteResult[SourceItemRow]:
    item = sources.get_item(db,item_id)
    if item.status == 'sent':
        return WriteResult(item,False)
    if item.status != 'pending' or sources.get_source(db,item.source_id).status != 'running':
        raise StateConflict('Item cannot be sent')
    cas(db,'source_items',item_id,{'status':'sent','attempts':max(1,item.attempts),'error':None,'updated_at':now()}, {'status':'pending','attempts':item.attempts})
    return WriteResult(sources.get_item(db,item_id),True)


def skip_duplicate(db: WriteSession, item_id: int, *, sha256: str) -> WriteResult[SourceItemRow]:
    item = sources.get_item(db,item_id)
    source = sources.get_source(db,item.source_id)
    if source.type != 'import' or not images.find_by_sha256(db,sha256):
        raise StateConflict('Import duplicate evidence required')
    if item.status == 'skipped':
        return WriteResult(item,False)
    if item.status not in ('pending','sent') or (item.status == 'pending' and source.status != 'running'):
        raise StateConflict('Cannot skip item')
    cas(db,'source_items',item_id,{'status':'skipped','error':'duplicate_sha256','updated_at':now()}, {'status':item.status})
    return WriteResult(sources.get_item(db,item_id),True)


def set_cost_policy(db: WriteSession, source_id: int, *, policy: Literal['deny','ask','allow']) -> WriteResult[SourceRow]:
    return sources.update_source(db,source_id,SourcePatch(cost_policy=policy))


def fail_item(db: WriteSession, item_id: int, *, attempt: int, error: str) -> WriteResult[SourceItemRow]:
    integer(attempt,1)
    error_reason(error)
    item = sources.get_item(db,item_id)
    if item.attempts != attempt:
        raise StateConflict('Attempt changed')
    if item.status == 'failed' and item.error == error:
        return WriteResult(item,False)
    if item.status != 'sent':
        raise StateConflict('Only sent items can fail')
    cas(db,'source_items',item_id,{'status':'failed','error':error,'updated_at':now()}, {'status':'sent','attempts':attempt})
    return WriteResult(sources.get_item(db,item_id),True)


def retry_failed_item(db: WriteSession, item_id: int, *, expected_attempt: int) -> WriteResult[SourceItemRow]:
    item = sources.get_item(db,item_id)
    source = sources.get_source(db,item.source_id)
    if item.status == 'pending' and item.attempts == expected_attempt+1:
        return WriteResult(item,False)
    if item.status != 'failed' or item.attempts != expected_attempt or source.status not in ('running','paused','done'):
        raise StateConflict('Retry precondition changed')
    if source.status == 'done':
        cas(db,'sources',source.source_id,{'status':'paused','finished_at':None},{'status':'done'})
        _cycle(db,source.source_id)
    cas(db,'source_items',item_id,{'status':'pending','attempts':item.attempts+1,'error':None,'updated_at':now()}, {'status':'failed','attempts':expected_attempt})
    return WriteResult(sources.get_item(db,item_id),True)


def complete_item(db: WriteSession, artifact: ImageArtifact, originals: Sequence[OriginalInput]) -> WriteResult[ImageRow]:
    from .receipts import register_artifact
    return register_artifact(db,artifact,tuple(originals))


def transition_job(db: WriteSession, job_id: int, *, expected: JobStatus, target: JobStatus, error: str | None = None) -> WriteResult[JobRow]:
    job = jobs.get_job(db,job_id)
    if job.status == target and job.error == error:
        return WriteResult(job,False)
    if job.status != expected or (expected,target) not in JOB_EDGES:
        raise StateConflict('Illegal job transition')
    if target == 'done' and job.done_count+job.failed_count+job.skipped_count != job.total:
        raise StateConflict('Unfinished job')
    if error is not None:
        text(error)
        if len(error)>200 or '://' in error or '\n' in error:
            raise ValidationError('Sanitized error required')
    cas(db,'jobs',job_id,{'status':target,'error':error,'updated_at':now(job.updated_at),
                        'finished_at':now() if target in ('done','failed','canceled') else None},{'status':expected})
    if target == 'running':
        jobs.begin_run(db,job_id)
    elif target in ('done','failed','canceled') and db.writer:
        db.after_commit.append(lambda: db.writer.job_runs.pop(job_id,None))
    return WriteResult(jobs.get_job(db,job_id),True)


def commit_job_outcome(db: WriteSession, job_id: int, outcome: JobOutcome) -> WriteResult[JobRow]:
    job = jobs.get_job(db,job_id)
    choice(outcome.kind,('done','failed','skipped'))
    if outcome.reason is not None:
        error_reason(outcome.reason)
    progress = job.progress_json
    if outcome.image_id not in progress['image_ids']:
        raise OutcomeConflict('Image not in frozen scope')
    payload = asdict(outcome)
    payload.pop('run_token')
    signature = digest(payload)
    existing = progress['outcomes'].get(str(outcome.image_id))
    if existing:
        if existing['digest'] != signature:
            raise OutcomeConflict('Different terminal outcome')
        return WriteResult(job,False)
    if job.status == 'canceled':
        return WriteResult(job,False)
    if job.status not in ('running','paused'):
        raise StateConflict('Job does not accept results')
    if not outcome.run_token or db.writer is None or db.writer.job_runs.get(job_id) != outcome.run_token:
        raise StateConflict('Stale job run')
    if outcome.kind != 'done' and outcome.annotations:
        raise ValidationError('Failure/skip cannot create annotations')
    if outcome.kind == 'skipped' and outcome.reason == 'cache_hit':
        if job.mode == 'all':
            raise StateConflict('All mode ignores other jobs cache')
        targets = job.config_snapshot_json['target_types']
        if len(outcome.annotation_ids) != len(targets):
            raise OutcomeConflict('Cache evidence must cover all target layers')
        allowed = job.config_snapshot_json.get('allowed_cache_keys',{}).get(str(outcome.image_id),{})
        seen = set()
        for identity in outcome.annotation_ids:
            annotation = get(db,'annotations',AnnotationRow,identity)
            if annotation.image_id != outcome.image_id or annotation.type in seen or annotation.type not in targets or not annotation.is_current:
                raise OutcomeConflict('Invalid current cache evidence')
            expected_source = 'wd' if job.executor == 'wd' else 'llm'
            if annotation.source != expected_source or annotation.job_id is None or jobs.get_job(db,annotation.job_id).executor != job.executor:
                raise OutcomeConflict('Cache evidence executor mismatch')
            if annotation.cache_key not in allowed.get(annotation.type,[]):
                raise OutcomeConflict('Cache key outside frozen configuration')
            seen.add(annotation.type)
    ids = []
    keys = []
    if outcome.kind == 'done':
        targets = job.config_snapshot_json['target_types']
        if sorted(a.type for a in outcome.annotations) != sorted(targets):
            raise ValidationError('All target layers must commit together')
        for value in outcome.annotations:
            if value.image_id != outcome.image_id or value.job_id != job_id or value.source != ('wd' if job.executor == 'wd' else 'llm') or value.model_name != job.config_snapshot_json['model']:
                raise ValidationError('Result does not match frozen job')
            current = annotations.get_current(db,value.image_id,value.source,value.type)
            with authority(db,'job'):
                result = annotations.append_version(db,value,expected=CurrentRef(value.image_id,value.source,value.type,current.annotation_id if current else None),
                                                    policy='force_new_job' if job.mode=='all' else 'cache')
            ids.append(result.value.annotation_id)
            keys.append(value.cache_key)
    elif outcome.annotation_ids:
        for identity in outcome.annotation_ids:
            annotation = get(db,'annotations',AnnotationRow,identity)
            if annotation.image_id != outcome.image_id:
                raise ValidationError('Cache evidence belongs to another image')
            ids.append(identity)
            keys.append(annotation.cache_key)
    progress['outcomes'][str(outcome.image_id)] = {'kind':outcome.kind,'reason':outcome.reason,'annotation_ids':ids,'cache_keys':keys,'digest':signature}
    counts = {kind:sum(o['kind']==kind for o in progress['outcomes'].values()) for kind in ('done','failed','skipped')}
    cas(db,'jobs',job_id,{'progress_json':dumps(progress),'done_count':counts['done'],'failed_count':counts['failed'],'skipped_count':counts['skipped'],'updated_at':now(job.updated_at)}, {'updated_at':job.updated_at,'status':job.status})
    return WriteResult(jobs.get_job(db,job_id),True)


def verify_read_set(db, read_set, error=PreviewStale):
    for ref in read_set:
        current = annotations.get_current(db,ref.image_id,ref.source,ref.type)
        if (current.annotation_id if current else None) != ref.annotation_id:
            raise error('Annotation read set changed')


def rule_digest(plan: RulePlan) -> str:
    return digest({'rule':plan.rule,'scope':asdict(plan.scope),'changes':plan.changes,'read_set':plan.read_set})


def apply_rule(db: WriteSession, plan: RulePlan) -> RuleApplyResult:
    if len(plan.changes)>5000 or len(dumps(plan.changes).encode())>16*1024*1024:
        raise BatchTooLarge('Split rule explicitly into independent operations')
    safe_snapshot(plan.rule)
    scope_json(plan.scope)
    if plan.preview_digest != rule_digest(plan):
        raise PreviewStale('Preview digest differs')
    verify_read_set(db,plan.read_set)
    if len({c.image_id for c in plan.changes}) != len(plan.changes):
        raise ValidationError('Duplicate rule image')
    changes = []
    from .merge_metadata import make_snapshot
    for change in plan.changes:
        expected = CurrentRef(change.image_id,'rule','tags',change.previous_rule_id)
        if expected not in plan.read_set:
            raise PreviewStale('Rule reference absent from read set')
        value = make_snapshot(db,change.image_id,'rule',change.tags,plan.read_set)
        # Supplied metadata is evidence, not permission to forge cutoff/origin.
        # Empty object requests server construction; nonempty must match exactly.
        if change.merge_meta and change.merge_meta != value.params['merge_meta']:
            raise ValidationError('Rule merge evidence differs from server construction')
        with authority(db,'rule'):
            result = annotations.append_version(db,value,expected=expected,policy='content')
        if result.changed:
            changes.append({'image_id':change.image_id,'new_annotation_id':result.value.annotation_id,'previous_annotation_id':change.previous_rule_id})
    if not changes:
        return RuleApplyResult(None,0)
    cursor = db.execute('INSERT INTO operation_log(rule_json,scope_json,affected_count,changes_json,created_at) VALUES(?,?,?,?,?)',
                        (dumps(plan.rule),scope_json(plan.scope),len(changes),dumps(changes),now()))
    return RuleApplyResult(cursor.lastrowid,len(changes))


def undo_rule(db: WriteSession, op_id: int) -> WriteResult[OperationRow]:
    operation = operations.get_operation(db,op_id)
    if operation.status == 'undone':
        return WriteResult(operation,False)
    if operations.latest_applied(db).op_id != op_id:
        raise UndoConflict('Only latest applied operation can be undone')
    ignored = set()
    for record in db.execute("SELECT changes_json FROM operation_log WHERE status='undone'"):
        import json
        ignored.update(c['new_annotation_id'] for c in json.loads(record[0]))
    for change in operation.changes_json:
        operations.validate_change(db,change)
        new = get(db,'annotations',AnnotationRow,change['new_annotation_id'])
        previous = get(db,'annotations',AnnotationRow,change['previous_annotation_id']) if change['previous_annotation_id'] is not None else None
        if not new.is_current or (previous and previous.is_current):
            raise UndoConflict('Rule current identity changed')
        for row in db.execute('SELECT annotation_id FROM annotations WHERE image_id=? AND annotation_id>?',(change['image_id'],new.annotation_id)):
            if row[0] not in ignored:
                raise UndoConflict('Subsequent annotation depends on this operation')
    for change in operation.changes_json:
        db.execute('UPDATE annotations SET is_current=0 WHERE annotation_id=?',(change['new_annotation_id'],))
        if change['previous_annotation_id'] is not None:
            db.execute('UPDATE annotations SET is_current=1 WHERE annotation_id=?',(change['previous_annotation_id'],))
    cas(db,'operation_log',op_id,{'status':'undone','undone_at':now()},{'status':'applied'})
    return WriteResult(operations.get_operation(db,op_id),True)


def bulk_review(db: WriteSession, image_ids: Sequence[int], *, expected: Mapping[int, ReviewStatus], target: ReviewStatus) -> tuple[ImageRow, ...]:
    return tuple(images.set_review(db,identity,expected=expected[identity],target=target).value for identity in dict.fromkeys(image_ids))


def edit_manual_tags(db: WriteSession, image_id: int, *, tags: Sequence[Tag], read_set: Sequence[CurrentRef]) -> WriteResult[AnnotationRow]:
    verify_read_set(db,read_set)
    from .merge_metadata import make_snapshot
    value = make_snapshot(db,image_id,'manual',tuple(tags),read_set)
    current = annotations.get_current(db,image_id,'manual','tags')
    with authority(db,'manual'):
        return annotations.append_version(db,value,expected=CurrentRef(image_id,'manual','tags',current.annotation_id if current else None),policy='content')


def evaluate_cost(project: ProjectHandle, source_id: int, *, estimated_anlas: int) -> Literal['submit','paused']:
    integer(estimated_anlas)
    def work(db):
        source = sources.get_source(db,source_id)
        if source.status != 'running':
            raise StateConflict('Source is not running')
        cycle = db.writer.cycles.setdefault(source_id,(uuid4().hex,False))
        if not estimated_anlas or source.cost_policy == 'allow' or (source.cost_policy == 'ask' and cycle[1]):
            return 'submit'
        transition_source(db,source_id,expected='running',target='paused',reason='cost')
        return 'paused'
    return project.writer.submit(work).result()


def confirm_cost_and_resume(project: ProjectHandle, source_id: int, *, expected_cycle_id: str) -> SourceRow:
    def work(db):
        source = sources.get_source(db,source_id)
        cycle = db.writer.cycles.get(source_id)
        if source.cost_policy != 'ask' or source.status != 'paused' or cycle is None or cycle[0] != expected_cycle_id:
            raise StateConflict('Stale or forbidden cost confirmation')
        result = transition_source(db,source_id,expected='paused',target='running',reason='cost_confirmed').value
        db.after_commit.append(lambda: db.writer.cycles.__setitem__(source_id,(uuid4().hex,True)))
        return result
    return project.writer.submit(work).result()
