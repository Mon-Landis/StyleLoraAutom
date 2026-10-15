"""One stop path for user requests, project close and process drain."""
import asyncio
from dataclasses import replace
from .contracts import Failure
from .tasks import StopRequest, StopReceipt, TERMINAL


class StopCoordinator:
    def __init__(self, registry):
        self.registry, self.workers, self.receipts = registry, {}, {}
        self.timeout = 30
        registry.coordinator = self

    async def stop(self, task_id, request):
        registry = self.registry
        registry._record(task_id)
        if request.reason not in ('user', 'project_close', 'shutdown') or not isinstance(request.request_key, str) or not request.request_key:
            raise Failure(422, 'input:invalid')
        if request.request_key in registry.keys and registry.keys[request.request_key] != task_id:
            raise Failure(409, 'task:request_key_conflict')
        registry.keys[request.request_key] = task_id
        async with registry.locks.setdefault(task_id, asyncio.Lock()):
            current = await registry.get(task_id)
            if task_id in self.receipts:
                if task_id in registry.stopping:
                    old = registry.stopping[task_id]
                    registry.stopping[task_id] = replace(old, deadline_monotonic=min(old.deadline_monotonic, request.deadline_monotonic))
                    adapter, resource, _ = registry._record(task_id)
                    if hasattr(adapter, 'requests'):
                        adapter.requests[resource.id] = registry.stopping[task_id]
                return replace(self.receipts[task_id], status=current.status, no_op=True, version=current.version)
            if current.status in TERMINAL or current.status == 'paused':
                return StopReceipt(task_id, current.status, False, True, current.version)
            if not current.cancelable:
                raise Failure(409, 'task:not_cancelable')
            registry.stopping[task_id] = request
            adapter, resource, _ = registry._record(task_id)
            try:
                await adapter.stop(resource.id, request)
            except Exception as exc:
                from stylelora.project_db.errors import StateConflict
                if isinstance(exc, StateConflict):
                    registry.stopping.pop(task_id, None)
                    latest = await registry.get(task_id)
                    if latest.status in TERMINAL:
                        return StopReceipt(task_id, latest.status, False, True, latest.version)
                    raise Failure(409, 'task:state_conflict') from None
                await registry.get(task_id)
                raise Failure(503, 'task:stop_persist_failed') from None
            current = await registry.get(task_id)
            receipt = self.receipts[task_id] = StopReceipt(task_id, 'canceling', True, False, current.version)
            async def finish():
                await adapter.wait(resource.id)
                registry.stopped[task_id] = registry.stopping.pop(task_id).reason
                await registry.get(task_id)
            self.workers[task_id] = asyncio.create_task(finish())
            return receipt

    async def drain(self, project_id=None, reason='shutdown'):
        registry = self.registry
        ids = [i for i, (_, _, project) in registry.records.items()
               if project_id is None or project is not None and project.project_id == project_id]
        values = await asyncio.gather(*(registry.get(i) for i in ids))
        requests = [self.stop(v.task_id, StopRequest(reason, registry.clock()+self.timeout, reason+':'+v.task_id))
                    for v in values if v.cancelable and v.status not in TERMINAL and v.status != 'paused']
        # All adapters receive the notification before any worker is joined.
        results = await asyncio.gather(*requests, return_exceptions=True)
        errors = [value for value in results if isinstance(value, BaseException)]
        if errors:
            raise errors[0]
        await asyncio.gather(*(self.workers[i] for i in ids if i in self.workers))
        await asyncio.gather(*(registry.records[i][0].wait(registry.records[i][1].id) for i in ids if i not in self.workers))
