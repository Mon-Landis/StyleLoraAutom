"""Domain projections and cooperative stop handles, with no result bodies in DTOs."""
import asyncio
from dataclasses import replace
from .tasks import TaskDTO, ProgressDTO, RecoveryDTO, StopReceipt, StopRequest, ProjectRef, ResourceRef
from .contracts import Failure
from stylelora.project_db import read_snapshot, submit_write, commands
from stylelora.project_db.repositories import sources, jobs
from stylelora.project_db.types import SourceFilter, PageRequest


class DomainAdapter:
    def __init__(self, rt, kind, ctx=None):
        self.rt, self.kind, self.ctx = rt, kind, ctx
        self.last = {}
        self.requests = {}

    def runner(self, identity):
        if self.kind == 'source':
            return self.rt.runners.get((self.ctx.project_id, int(identity)))
        if self.kind == 'job':
            return self.rt.job_runners.get(self.ctx.project_id)
        return self.rt.prompts.service

    def worker_exited(self, identity):
        runner = self.runner(identity)
        if self.kind == 'source':
            return not runner or not runner.task or runner.task.done()
        if self.kind == 'job':
            return not runner or int(identity) not in runner.active
        return identity not in runner._running

    async def snapshot(self, identity):
        if self.ctx is not None and self.rt.current is not self.ctx:
            if identity not in self.last:
                raise Failure(503, 'task:unavailable')
            return replace(self.last[identity], cancelable=False)
        unknown = 0
        reason = None
        if self.kind == 'source':
            with read_snapshot(self.ctx.handle) as db:
                row = sources.get_source(db, int(identity))
                counts = sources.count_item_states(db, int(identity))
            status = 'pending' if row.status == 'draft' else row.status
            unknown = counts.get('sent', 0)
            progress = ProgressDTO(sum(counts.get(k, 0) for k in ('done', 'failed', 'skipped')),
                                   sum(counts.values()), 'images', counts.get('failed', 0), counts.get('skipped', 0))
        elif self.kind == 'job':
            with read_snapshot(self.ctx.handle) as db:
                row = jobs.get_job(db, int(identity))
            status = row.status
            progress = ProgressDTO(row.done_count+row.failed_count+row.skipped_count, row.total, 'images', row.failed_count, row.skipped_count)
            unknown = sum(v.get('reason') == 'llm:outcome_unknown' for v in row.progress_json.get('outcomes', {}).values())
            unknown_ids = {k for k,v in row.progress_json.get('outcomes', {}).items() if v.get('reason') == 'llm:outcome_unknown'}
            unknown_ids.update(k for k,v in row.progress_json.get('dispatch', {}).items() if v.get('phase') == 'unknown' or v.get('phase') == 'inflight' and self.worker_exited(identity))
            unknown = len(unknown_ids)
        else:
            data = self.rt.prompts.service.store.read(identity)
            status, counts = data['status'], data['counts']
            progress = ProgressDTO(sum(counts.get(k, 0) for k in ('ok', 'failed', 'skipped')), len(data['outcomes']),
                                   'items', counts['failed'], counts['skipped'])
            unknown = sum(v['outcome']['reason_code'] == 'llm:outcome_unknown' for v in data['outcomes'])
        resumable = status == 'paused'
        if resumable:
            reason = self.rt.stop_reason(str(self.ctx.handle.root) if self.ctx else 'global', self.kind, identity)
        recovery = RecoveryDTO(resumable, bool(unknown), unknown,
                               'reconcile_resource' if unknown else 'resume_resource' if resumable else 'none')
        pending_cancelable = self.kind == 'prompt' or self.kind == 'job' and self.runner(identity) is not None
        result = TaskDTO('', self.kind, None, status, progress, status == 'running' or pending_cancelable and status == 'pending',
                         (ResourceRef(self.kind, identity),), reason_code=reason, recovery=recovery)
        self.last[identity] = result
        return result

    async def stop(self, identity, request):
        self.requests[identity] = request
        runner = self.runner(identity)
        if self.kind == 'source':
            if runner:
                await runner.pause('user:paused')
            else:
                await asyncio.wrap_future(submit_write(self.ctx.handle, lambda db: commands.transition_source(
                    db, int(identity), expected='running', target='paused', reason='user:paused')))
        elif self.kind == 'job':
            if runner:
                await runner.control(int(identity), 'pause')
            else:
                await asyncio.wrap_future(submit_write(self.ctx.handle, lambda db: commands.transition_job(
                    db, int(identity), expected='running', target='paused')))
        else:
            current = runner.get_task(identity)
            await runner.control_task(identity, 'pause', expected_revision=current.revision)
        await self.rt.record_stop(str(self.ctx.handle.root) if self.ctx else 'global', self.kind, identity, request.reason)
        return StopReceipt('', 'canceling', True, False, '0')

    async def wait(self, identity):
        while not self.worker_exited(identity):
            request = self.requests.setdefault(identity, StopRequest('shutdown', self.rt.clock()+30, 'drain'))
            if self.rt.clock() >= request.deadline_monotonic:
                runner = self.runner(identity)
                if self.kind == 'source' and runner and runner.task:
                    runner.task.cancel()
                    await runner.wait()
                elif self.kind == 'job' and runner:
                    job = runner.get(int(identity))
                    for image_id, receipt in job.progress_json.get('dispatch', {}).items():
                        if receipt.get('phase') == 'inflight':
                            await runner.write(lambda db, image_id=int(image_id): jobs.dispatch_receipt(db, int(identity), image_id, 'unknown'))
                    token = runner.controls.get(int(identity))
                    if token:
                        token.cancel()
                elif self.kind == 'prompt':
                    with runner.store.lock:
                        data = runner.store.read(identity)
                        changed = False
                        for row in data['outcomes']:
                            if row['outcome']['status'] == 'running':
                                row['outcome'].update(status='failed', reason_code='llm:outcome_unknown', entry=None)
                                changed = True
                        if changed:
                            data['generation'] += 1
                            runner.store.write(data, expected_revision=data['revision'])
                    for token in getattr(runner, 'request_tokens', {}).get(identity, ()):
                        token.cancel()
            await asyncio.sleep(.01)


class OperationAdapter:
    def __init__(self, rt, kind):
        self.rt, self.kind = rt, kind
        self.requests = {}

    def worker_exited(self, identity):
        task = self.rt.supervisor.tasks.get(identity)
        return task is None or task.done()

    async def snapshot(self, identity):
        record = self.rt.supervisor.get(identity)
        return TaskDTO('', self.kind, None, record['status'],
                       ProgressDTO(record['completed'], record['total'], 'files'),
                       identity in self.rt.supervisor.cancel_handles and record['status'] in ('pending', 'running'),
                       (ResourceRef(self.kind, record.get('resource_id', identity)),),
                       reason_code=record.get('stop_reason') or (record.get('error') or {}).get('code'))

    async def stop(self, identity, request):
        self.requests[identity] = request
        handle = self.rt.supervisor.cancel_handles.get(identity)
        if handle is None:
            raise Failure(409, 'task:not_cancelable')
        self.rt.supervisor.records[identity]['stop_reason'] = request.reason
        self.rt.supervisor.persist()
        handle()
        await self.rt.record_stop('global', self.kind, identity, request.reason)
        return StopReceipt('', 'canceling', True, False, '0')

    async def wait(self, identity):
        task = self.rt.supervisor.tasks.get(identity)
        if task:
            if self.kind == 'model_download':
                request = self.requests.get(identity)
                while not task.done():
                    if request is not None and self.rt.clock() >= request.deadline_monotonic:
                        task.cancel()
                        break
                    await asyncio.sleep(.01)
            await asyncio.shield(task)


class ExportAdapter(OperationAdapter):
    pass


class CleanupAdapter(OperationAdapter):
    pass


class ModelDownloadAdapter(OperationAdapter):
    pass


async def refresh(rt):
    ctx = rt.current
    if ctx is not None and not ctx.closing:
        project = ProjectRef(ctx.project_id, ctx.handle.metadata.name)
        with read_snapshot(ctx.handle) as db:
            for kind in ('source', 'job'):
                page = PageRequest(limit=1000)
                adapter = rt.task_adapters.setdefault((ctx.project_id, kind), DomainAdapter(rt, kind, ctx))
                while True:
                    result = sources.list_sources(db, SourceFilter(), page) if kind == 'source' else jobs.list_jobs(db, page=page)
                    for row in result.items:
                        identity = str(row.source_id if kind == 'source' else row.job_id)
                        rt.tasks.register(adapter, ResourceRef(kind, identity), project)
                    if result.next_after_id is None:
                        break
                    page = PageRequest(result.next_after_id, 1000, result.upper_id)
    if hasattr(rt, 'prompts'):
        adapter = rt.task_adapters.setdefault(('global', 'prompt'), DomainAdapter(rt, 'prompt'))
        for path in rt.prompts.service.store.root.glob('*.json'):
            rt.tasks.register(adapter, ResourceRef('prompt', path.stem), None)
    for identity, record in tuple(rt.supervisor.records.items()):
        kind = record['kind']
        if kind not in ('export', 'cleanup', 'model_download'):
            continue
        if record['project_id'] and (ctx is None or record['project_id'] != ctx.project_id):
            continue
        adapter = rt.task_adapters.setdefault(('global', kind),
            {'export': ExportAdapter, 'cleanup': CleanupAdapter, 'model_download': ModelDownloadAdapter}[kind](rt, kind))
        project = ProjectRef(record['project_id'], ctx.handle.metadata.name if ctx and ctx.project_id == record['project_id'] else '') if record['project_id'] else None
        rt.tasks.register(adapter, ResourceRef(kind, identity), project)
    for identity in tuple(rt.tasks.records):
        await rt.tasks.get(identity)
    rt.tasks.evict()
