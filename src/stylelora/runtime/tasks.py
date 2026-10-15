"""Read-only aggregate of domain tasks. Domain adapters remain the sole writers."""
import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import time
from typing import Protocol, Literal
from uuid import uuid4
from .contracts import Failure, plain

TaskKind = Literal['source', 'job', 'prompt', 'export', 'cleanup', 'model_download']
TaskState = Literal['pending', 'running', 'canceling', 'paused', 'done', 'failed', 'canceled']
KINDS = ('source', 'job', 'prompt', 'export', 'cleanup', 'model_download')
STATES = ('pending', 'running', 'canceling', 'paused', 'done', 'failed', 'canceled')
TERMINAL = ('done', 'failed', 'canceled')


@dataclass(frozen=True)
class ProjectRef:
    project_id: str
    display_name: str


@dataclass(frozen=True)
class ResourceRef:
    kind: str
    id: str


@dataclass(frozen=True)
class ProgressDTO:
    completed: int = 0
    total: int | None = None
    unit: str = 'items'
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class RecoveryDTO:
    resumable: bool = False
    reconciliation_required: bool = False
    unknown_count: int = 0
    action: str = 'none'


@dataclass(frozen=True)
class TaskDTO:
    task_id: str
    kind: TaskKind
    project: ProjectRef | None
    status: TaskState
    progress: ProgressDTO = field(default_factory=ProgressDTO)
    cancelable: bool = False
    resources: tuple[ResourceRef, ...] = ()
    version: str = '0'
    reason_code: str | None = None
    recovery: RecoveryDTO = field(default_factory=RecoveryDTO)


@dataclass(frozen=True)
class StopRequest:
    reason: str
    deadline_monotonic: float
    request_key: str


@dataclass(frozen=True)
class StopReceipt:
    task_id: str
    status: TaskState
    accepted: bool
    no_op: bool
    version: str


@dataclass(frozen=True)
class TaskQuery:
    kind: str | None = None
    status: str | None = None
    project_id: str | None = None
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self):
        if (self.kind is not None and self.kind not in KINDS or
                self.status is not None and self.status not in STATES or
                type(self.limit) is not int or not 1 <= self.limit <= 200):
            raise Failure(422, 'input:invalid')


@dataclass(frozen=True)
class TaskPage:
    items: tuple[TaskDTO, ...]
    next_cursor: str | None
    event_cursor: str


class TaskAdapter(Protocol):
    async def snapshot(self, resource_id: str) -> TaskDTO: ...
    async def stop(self, resource_id: str, request: StopRequest) -> StopReceipt: ...
    async def wait(self, resource_id: str) -> None: ...


class TaskRegistry:
    def __init__(self, events, *, instance_id=None, clock=time.monotonic, ttl=600, max_terminal=1000):
        self.events, self.clock = events, clock
        self.instance_id = instance_id or uuid4().hex
        self.ttl, self.max_terminal = ttl, max_terminal
        self.records, self.values, self.expired, self.pages = {}, {}, set(), {}
        self.keys, self.stopping, self.stopped, self.terminal_since = {}, {}, {}, {}
        self.locks = {}
        self.snapshot_locks = {}
        self.coordinator = None

    def register(self, adapter, resource, project):
        identity = hashlib.sha256(f'{self.instance_id}:{project.project_id if project else "global"}:{resource.kind}:{resource.id}'.encode()).hexdigest()
        if identity not in self.expired:
            self.records.setdefault(identity, (adapter, resource, project))
        return identity

    def _record(self, identity):
        if identity in self.expired:
            raise Failure(410, 'task:expired')
        if identity not in self.records:
            raise Failure(404, 'task:not_found')
        return self.records[identity]

    async def get(self, task_id):
        self._record(task_id)
        async with self.snapshot_locks.setdefault(task_id, asyncio.Lock()):
            return await self._get(task_id)

    async def _get(self, task_id):
        adapter, resource, project = self._record(task_id)
        try:
            current = await adapter.snapshot(resource.id)
        except Exception:
            raise Failure(503, 'task:unavailable') from None
        old = self.values.get(task_id)
        if current.kind not in KINDS or current.status not in STATES:
            raise Failure(503, 'task:unavailable')
        current = replace(current, task_id=task_id, project=project, version=old.version if old else '0',
                          resources=current.resources or (resource,))
        if task_id in self.stopping:
            current = replace(current, status='canceling', reason_code=self.stopping[task_id].reason)
        elif task_id in self.stopped and current.status == 'paused':
            current = replace(current, reason_code=self.stopped[task_id])
        elif task_id in self.stopped and current.status in ('pending', 'running'):
            self.stopped.pop(task_id, None)
            if self.coordinator is not None:
                self.coordinator.receipts.pop(task_id, None)
        if current != old:
            current = replace(current, version=str(int(old.version)+1 if old else 1))
            self.values[task_id] = current
            kind = ('created' if old is None else 'finished' if current.status in TERMINAL or current.status == 'paused' and old.status == 'canceling'
                    else 'progress' if replace(current, progress=old.progress, version=old.version) == old else 'updated')
            self.events.publish('task.'+kind, project_id=project.project_id if project else None,
                                resource={'kind': 'task', 'id': task_id}, version=current.version, payload=plain(current))
        if current.status in TERMINAL and getattr(adapter, 'worker_exited', lambda _: True)(resource.id):
            self.terminal_since.setdefault(task_id, self.clock())
        else:
            self.terminal_since.pop(task_id, None)
        return current

    def evict(self):
        terminal = sorted(self.terminal_since, key=self.terminal_since.get)
        for identity in terminal:
            if self.clock()-self.terminal_since[identity] < self.ttl and len(self.terminal_since) <= self.max_terminal:
                continue
            old = self.values.pop(identity)
            self.records.pop(identity, None)
            self.terminal_since.pop(identity, None)
            self.expired.add(identity)
            self.events.publish('task.removed', project_id=old.project.project_id if old.project else None,
                                resource={'kind': 'task', 'id': identity}, version=str(int(old.version)+1),
                                payload={'task_id': identity, 'version': str(int(old.version)+1)})

    async def list(self, query=TaskQuery()):
        binding = (query.kind, query.status, query.project_id, query.limit)
        if query.cursor:
            page = self.pages.get(query.cursor)
            if page is None or page[0] <= self.clock():
                raise Failure(410, 'task:cursor_expired')
            expiry, expected, values, event_cursor = page
            if binding != expected:
                raise Failure(422, 'input:invalid')
        else:
            values = tuple(await asyncio.gather(*(self.get(i) for i in tuple(self.records))))
            self.evict()
            values = tuple(v for v in values if v.task_id not in self.expired and
                           (query.kind is None or v.kind == query.kind) and
                           (query.status is None or v.status == query.status) and
                           (query.project_id is None or v.project is not None and v.project.project_id == query.project_id))
            expiry = self.clock()+60
            event_cursor = f'{self.events.epoch}:{self.events.sequence}'
        cursor = None
        if len(values) > query.limit:
            cursor = uuid4().hex
            self.pages[cursor] = (expiry, binding, values[query.limit:], event_cursor)
        self.pages = {k:v for k,v in self.pages.items() if v[0] > self.clock()}
        return TaskPage(values[:query.limit], cursor, event_cursor)

    async def cancel(self, task_id, request_key):
        from .cancellation import StopCoordinator
        if self.coordinator is None:
            self.coordinator = StopCoordinator(self)
        return await self.coordinator.stop(task_id, StopRequest('user', self.clock()+30, request_key))
