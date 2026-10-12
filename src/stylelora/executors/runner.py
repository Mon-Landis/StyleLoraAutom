"""Single-worker jobs, frozen scope, and atomic per-image outcomes."""
import asyncio
from dataclasses import replace
import hashlib
import json
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db.repositories import annotations, images, jobs
from stylelora.project_db import commands
from stylelora.project_db.errors import Busy, CommitUnknown, DiskFull, StateConflict
from stylelora.project_db.types import JobOutcome, PageRequest
from stylelora.llm.types import LlmFailure
from .cache import cache_keys, covered_group
from .events import EventBus
from .scope import resolve_in_snapshot, scope_from_json
from .types import ImageContext, ImageInput, CancelToken, ExecutionResult, Unavailable
from stylelora.vision.evidence import evidence_in_snapshot


def context_in_snapshot(project, db, identity, snapshot, cancel):
    image = images.get_image(db, identity)
    return ImageContext(ImageInput(identity, project.root/'images'/image.filename,
        image.sha256, image.file_size, image.width, image.height), evidence_in_snapshot(db, identity),
        images.rating_of(image), snapshot, cancel)


class JobRunner:
    def __init__(self, project, executors, *, snapshots=None, events=None):
        self.project, self.executors = project, executors
        self.snapshots, self.events = snapshots or {}, events or EventBus()
        self.controls, self.active, self.retained = {}, set(), {}
        self._dispatch = asyncio.Lock()

    async def write(self, work):
        return await asyncio.wrap_future(submit_write(self.project, work))

    def get(self, job_id):
        with read_snapshot(self.project) as db:
            return jobs.get_job(db, job_id)

    def emit(self, job, kind, identity=None, reason=None):
        self.events.emit(job.job_id, kind, image_id=identity, reason=reason,
            counts={'total': job.total, 'done': job.done_count, 'failed': job.failed_count, 'skipped': job.skipped_count})

    def predecessor_blocked(self, job):
        if not job.batch_id:
            return False
        with read_snapshot(self.project) as db:
            after = 0
            while True:
                page = jobs.list_jobs(db, batch_id=job.batch_id, page=PageRequest(after_id=after, limit=1000))
                if any(row.job_id < job.job_id and row.status != 'done' for row in page.items):
                    return True
                if page.next_after_id is None:
                    return False
                after = page.next_after_id

    def _cache(self, db, context, executor):
        snapshot = context.snapshot
        source = 'wd' if snapshot['role'] == 'wd' else 'llm'
        rows = [annotations.get_current(db, context.image.image_id, source, kind) for kind in snapshot['target_types']]
        if not rows or any(row is None for row in rows):
            return {}, ()
        actual = rows[0].params_json.get('actual', {})
        if source == 'wd':
            config = getattr(executor, 'config', None)
            if config and any(actual.get(key) != getattr(config, key) for key in
                    ('backend', 'revision', 'general_threshold', 'character_threshold', 'manifest_hash')):
                return {}, ()
            if rows[0].model_name != snapshot['model']:
                return {}, ()
        else:
            router = getattr(executor, 'router', None)
            decision = router.local(context) if router else None
            if decision is None:
                if snapshot['routing'].get('use_rating_model'):
                    return {}, ()
                from stylelora.vision.rating import decision_for, RatingRouter
                decision = decision_for('questionable', False, 'fallback') if snapshot['routing'].get('nsfwfallback') else RatingRouter().default()
            if any(actual.get(key) != value for key, value in {
                    'route': decision.route, 'rating': decision.rating,
                    'rating_verified': decision.verified, 'rating_source': decision.source}.items()):
                return {}, ()
            role = 'vision_nsfw' if decision.route == 'nsfw' else 'vision'
            frozen = snapshot.get('roles', {}).get(role, {})
            if rows[0].model_name != frozen.get('model', snapshot['model']):
                return {}, ()
        keys = cache_keys(context, actual_model=rows[0].model_name, actual_params=actual)
        return keys, covered_group(db, context.image.image_id, keys)

    async def prepare(self, job, executor):
        if job.status == 'pending':
            if job.executor not in self.snapshots:
                raise Unavailable('snapshot_unavailable')
            snapshot = json.loads(json.dumps(self.snapshots[job.executor], allow_nan=False))
        else:
            snapshot = job.config_snapshot_json
        if hasattr(executor, 'validate'):
            await executor.validate(snapshot)
        if hasattr(executor, 'availability'):
            capability = executor.availability()
            if capability['status'] != 'available':
                raise Unavailable(capability['reason'])
        if job.status == 'pending':
            allowed = {}
            with read_snapshot(self.project) as db:
                ids = resolve_in_snapshot(db, scope_from_json(job.scope_json), mode=job.mode)
                for identity in ids:
                    context = context_in_snapshot(self.project, db, identity, snapshot, CancelToken())
                    keys, hit = self._cache(db, context, executor)
                    if hit:
                        allowed[str(identity)] = {kind: [key] for (_, kind), key in keys.items()}
            snapshot['allowed_cache_keys'] = allowed
            job = (await self.write(lambda db: jobs.freeze_job(db, job.job_id, snapshot, image_ids=ids))).value
        return job

    async def control(self, job_id, action):
        if action not in ('pause', 'resume', 'cancel'):
            raise ValueError('action_invalid')
        job = self.get(job_id)
        target = {'pause': 'paused', 'resume': 'running', 'cancel': 'canceled'}[action]
        if job.status == target:
            return job
        if action == 'resume':
            if job.status != 'paused' or job_id in self.active or self.predecessor_blocked(job):
                raise StateConflict('Job cannot resume')
            try:
                await self.prepare(job, self.executors[job.executor])
            except Unavailable:
                return job
        result = (await self.write(lambda db: commands.transition_job(db, job_id,
            expected=job.status, target=target))).value
        if action == 'cancel' and job_id in self.controls:
            self.controls[job_id].cancel()
        self.emit(result, 'job.'+target)
        return result

    async def _commit(self, job_id, outcome, context, expected_rating):
        def work(db):
            current = jobs.get_job(db, job_id)
            if current.status == 'canceled':
                return current, False
            value = outcome
            if context is not None and outcome.kind == 'done':
                row = images.get_image(db, outcome.image_id)
                reason = None
                kind = 'failed'
                if row.file_missing or not context.image.path.is_file():
                    kind, reason = 'skipped', 'file_missing'
                elif images.rating_of(row) != expected_rating:
                    reason = 'rating_conflict'
                else:
                    with context.image.path.open('rb') as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != context.image.sha256:
                            reason = 'image_changed'
                    for ref in context.evidence.refs:
                        annotation = annotations.get_current(db, ref.image_id, ref.source, ref.type)
                        if (annotation.annotation_id if annotation else None) != ref.annotation_id:
                            reason = 'input_changed'
                            break
                if reason:
                    value = replace(outcome, kind=kind, reason=reason, annotations=())
            result = commands.commit_job_outcome(db, job_id, value)
            return result.value, result.changed
        for attempt in range(3):
            try:
                return await self.write(work)
            except Busy:
                if attempt == 2:
                    raise
                await asyncio.sleep(.05*(attempt+1))
            except CommitUnknown:
                job = self.get(job_id)
                if str(outcome.image_id) in job.progress_json['outcomes']:
                    return job, True
                raise

    async def run(self, job_id):
        async with self._dispatch:
            job = self.get(job_id)
            if job.status not in ('pending', 'running') or self.predecessor_blocked(job):
                return job
            executor = self.executors[job.executor]
            if job.executor == 'llm_vision' and hasattr(executor, 'project'):
                if executor.project is None:
                    executor.project = self.project
                elif executor.project is not self.project:
                    raise StateConflict('Executor belongs to another project')
            try:
                job = await self.prepare(job, executor)
            except Unavailable as exc:
                target = 'failed' if job.status == 'pending' else 'paused'
                job = (await self.write(lambda db: commands.transition_job(db, job_id,
                    expected=job.status, target=target, error=exc.reason))).value
                self.emit(job, 'job.'+target, reason=exc.reason)
                return job
            self.active.add(job_id)
            token = self.controls[job_id] = CancelToken()
            run_token = await self.write(lambda db: jobs.get_run_token(db, job_id))
            self.emit(job, 'job.started')
            try:
                for identity in job.progress_json['image_ids']:
                    current = self.get(job_id)
                    if current.status != 'running' or token.canceled:
                        break
                    if str(identity) in current.progress_json['outcomes']:
                        continue
                    with read_snapshot(self.project) as db:
                        context = context_in_snapshot(self.project, db, identity, job.config_snapshot_json, token)
                        row = images.get_image(db, identity)
                        keys, hit = self._cache(db, context, executor)
                    expected_rating = context.rating
                    retained = self.retained.pop((job_id, identity), None)
                    if retained:
                        outcome, context, expected_rating = retained
                        outcome = replace(outcome, run_token=run_token)
                    elif row.file_missing or not context.image.path.is_file():
                        outcome = JobOutcome(identity, 'skipped', reason='file_missing', run_token=run_token)
                    else:
                        with context.image.path.open('rb') as stream:
                            unchanged = hashlib.file_digest(stream, 'sha256').hexdigest() == context.image.sha256
                        if not unchanged:
                            outcome = JobOutcome(identity, 'failed', reason='image_changed', run_token=run_token)
                        elif job.mode != 'all' and hit and all(key in job.config_snapshot_json.get('allowed_cache_keys', {}).get(str(identity), {}).get(kind, []) for (_, kind), key in keys.items()):
                            outcome = JobOutcome(identity, 'skipped', reason='cache_hit', annotation_ids=hit, run_token=run_token)
                        else:
                            try:
                                result = await executor.execute(context)
                            except asyncio.CancelledError:
                                break
                            except Unavailable as exc:
                                if exc.reason == 'rating_conflict':
                                    result = ExecutionResult('failed', exc.reason)
                                else:
                                    await self.control(job_id, 'pause')
                                    break
                            except LlmFailure as exc:
                                from stylelora.vision.executor import CONFIG_ERRORS
                                if exc.code in CONFIG_ERRORS:
                                    await self.control(job_id, 'pause')
                                    break
                                result = ExecutionResult('failed', exc.code)
                            except Exception:
                                result = ExecutionResult('failed', 'decode_failed')
                            if token.canceled or self.get(job_id).status == 'canceled':
                                break
                            expected_rating = result.rating if job.executor == 'llm_vision' else context.rating
                            values = tuple(replace(value, job_id=job_id) for value in result.annotations)
                            outcome = JobOutcome(identity, result.kind, values, result.reason, run_token=run_token)
                    try:
                        committed, changed = await self._commit(job_id, outcome, context, expected_rating)
                    except (Busy, DiskFull, CommitUnknown):
                        self.retained[job_id, identity] = (outcome, context, expected_rating)
                        try:
                            await self.control(job_id, 'pause')
                        except (Busy, DiskFull, CommitUnknown):
                            pass
                        break
                    if changed:
                        self.emit(committed, 'job.image_completed', identity,
                                  committed.progress_json['outcomes'][str(identity)]['reason'])
                final = self.get(job_id)
                if final.status in ('running', 'paused') and final.done_count+final.failed_count+final.skipped_count == final.total:
                    final = (await self.write(lambda db: commands.transition_job(db, job_id, expected=final.status, target='done'))).value
                    self.emit(final, 'job.done')
                return final
            finally:
                self.active.discard(job_id)
                self.controls.pop(job_id, None)


async def create_jobs(project, requests, *, batch_id):
    normalized = []
    with read_snapshot(project) as db:
        for request in requests:
            scope = request.scope
            if scope.selected_ids is not None:
                scope = replace(scope, selected_ids=tuple(sorted(set(scope.selected_ids))))
            resolve_in_snapshot(db, scope, mode=request.mode)
            normalized.append(replace(request, scope=scope))
    return await asyncio.wrap_future(submit_write(project, lambda db: jobs.create_batch(db, batch_id, normalized)))


async def run_job(project, job_id, *, runner):
    return await runner.run(job_id)


async def control_job(project, job_id, action, *, runner):
    return await runner.control(job_id, action)
