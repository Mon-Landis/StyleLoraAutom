import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import secrets
import time
from uuid import uuid4
from stylelora.project_db import project as projects, read_snapshot, submit_write, commands
from stylelora.project_db.repositories import sources, jobs
from stylelora.project_db.types import PageRequest, SourceFilter
from .contracts import Failure, plain, version
from .events import EventBridge
from .product import ProductStore
from .supervisor import TaskSupervisor


@dataclass
class ProjectContext:
    project_id: str
    generation: int
    handle: object
    closing: bool = False
    users: int = 0


class Runtime:
    def __init__(self, product_root, *, authority='127.0.0.1:8000', dev_origin=None,
                 picker=None, backend_factory=None, clock=time.monotonic, catalog=None,
                 executors=None, snapshots=None, llm=None, secret_store=None):
        self.token, self.secret = secrets.token_urlsafe(32), secrets.token_bytes(32)
        if dev_origin is not None:
            from urllib.parse import urlsplit
            origin = urlsplit(dev_origin)
            if origin.scheme != 'http' or origin.hostname != '127.0.0.1' or not origin.port or origin.path or origin.query or origin.fragment or origin.username:
                raise Failure(422, 'input:invalid')
        self.instance_id, self.authority = str(uuid4()), authority
        self.dev_origin, self.clock, self.picker = dev_origin, clock, picker
        self.backend_factory = backend_factory
        self.catalog, self.executors, self.snapshots, self.llm = catalog, executors or {}, snapshots or {}, llm
        self.job_runners = {}
        self.events = EventBridge(clock=clock)
        self.supervisor = TaskSupervisor(self.events)
        self.product = ProductStore(product_root)
        self.product.secret_store = secret_store
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='stylelora-work')
        self.current, self.generation, self.draining = None, 0, False
        self.transition = asyncio.Lock()
        self.codes, self.grants, self.known, self.runners, self.idempotency = {}, {}, {}, {}, {}
        from stylelora.llm.config import read_json
        self.known = {k: Path(v) for k, v in read_json(self.product.root/'recent.json', {}).items()}
        self.close_task = None
        self.shutdown_callback = None
        self.metadata_revision = 0
        from .images import ImageService
        self.images = ImageService(self)

    async def blocking(self, work):
        # Shield file workers: their owner must wait before releasing project locks.
        task = asyncio.wrap_future(self.pool.submit(work))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def bootstrap_code(self, origin=None):
        code = secrets.token_urlsafe(32)
        self.codes[hashlib.sha256(code.encode()).digest()] = (self.clock()+60, origin or self.dev_origin or 'http://'+self.authority)
        return code

    def redeem(self, code, origin):
        if not isinstance(code, str):
            raise Failure(401, 'auth:bootstrap_invalid')
        entry = self.codes.pop(hashlib.sha256(code.encode()).digest(), None)
        if not entry or entry[0] <= self.clock() or not origin or origin != entry[1]:
            raise Failure(401, 'auth:bootstrap_invalid')
        return dict(token=self.token, instance_id=self.instance_id, epoch=self.events.epoch,
                    api_base='/api', expires='process')

    def grant(self, path, purpose, project_id=None):
        if purpose not in ('project_create', 'project_open', 'import_root'):
            raise Failure(422, 'input:invalid')
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or any(p.endswith((' ', '.')) for p in path.parts):
            raise Failure(422, 'path:unsafe')
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise Failure(422, 'path:unsafe')
        identity = str(uuid4())
        self.grants[identity] = (path, purpose, project_id, self.clock()+900)
        return dict(grant_id=identity, purpose=purpose, display_name=path.name,
                    project_id=project_id, expires_at=self.clock()+900, version='1')

    def grant_path(self, identity, purpose):
        entry = self.grants.get(identity)
        if not entry or entry[1] != purpose or entry[3] <= self.clock() or (entry[2] is not None and (not self.current or entry[2] != self.current.project_id)):
            raise Failure(403, 'path:grant_denied')
        return entry[0]

    def require_project(self, identity):
        ctx = self.current
        if ctx is None or ctx.project_id != identity or ctx.closing:
            raise Failure(410, 'project:handle_expired')
        return ctx

    @asynccontextmanager
    async def lease(self, identity):
        ctx = self.require_project(identity)
        if any(r['project_id'] == identity and r['kind'] == 'cleanup'
               and r['status'] in ('pending', 'running') for r in self.supervisor.records.values()):
            raise Failure(409, 'storage:leased')
        ctx.users += 1
        try:
            yield ctx
        finally:
            ctx.users -= 1

    def project_view(self, ctx=None):
        ctx = ctx or self.current
        if ctx is None:
            return None
        return dict(project_id=ctx.project_id, metadata=plain(ctx.handle.metadata),
                    schema_version=ctx.handle.schema_version, recovery=plain(ctx.handle.writer.report),
                    version=version(self.secret, ctx.project_id, ctx.handle.metadata, self.metadata_revision))

    async def update_project(self, identity, value, expected):
        from .contracts import fields, check
        fields(value, {'name', 'trigger_word'})
        async with self.transition:
            ctx = self.require_project(identity)
            check(expected, self.project_view(ctx)['version'])
            old = ctx.handle.metadata
            name, trigger = value.get('name', old.name), value.get('trigger_word', old.trigger_word)
            if name != old.name or trigger != old.trigger_word:
                await self.blocking(lambda: projects.update_metadata(ctx.handle, name=name, trigger_word=trigger))
                self.metadata_revision += 1
            return self.project_view(ctx)

    async def open(self, path, *, create=None):
        async with self.transition:
            if self.draining:
                raise Failure(503, 'process:draining')
            path = Path(path).resolve()
            if self.current and create is not None:
                raise Failure(409, 'state:conflict')
            if self.current and self.current.handle.root == path:
                return self.project_view()
            await self._close_project()
            handle = await self.blocking(lambda: projects.create_project(path, **create) if create is not None else projects.open_project(path))
            self.generation += 1
            ctx = self.current = ProjectContext(str(uuid4()), self.generation, handle)
            known_id = next((key for key, value in self.known.items() if value == path), str(uuid4()))
            self.known[known_id] = path
            from stylelora.llm.config import atomic_json
            await self.product.call(lambda: atomic_json(self.product.root/'recent.json', {k: str(v) for k, v in self.known.items()}))
            self.events.publish('project.opened', project_id=ctx.project_id,
                                payload={'project_id': ctx.project_id, 'generation': ctx.generation, 'recovery_pending': False})
            return self.project_view()

    async def _close_project(self):
        ctx = self.current
        if ctx is None:
            return
        ctx.closing = True
        for (pid, _), runner in tuple(self.runners.items()):
            if pid == ctx.project_id and runner.source().status == 'running':
                await runner.pause('user:paused')
        def pause(db):
            page = sources.list_sources(db, SourceFilter(('running',)), PageRequest(limit=1000))
            while True:
                for row in page.items:
                    commands.transition_source(db, row.source_id, expected='running', target='paused', reason='user:paused')
                if page.next_after_id is None:
                    break
                page = sources.list_sources(db, SourceFilter(('running',)), PageRequest(page.next_after_id, 1000, page.upper_id))
            page = jobs.list_jobs(db, status='running', page=PageRequest(limit=1000))
            while True:
                for row in page.items:
                    commands.transition_job(db, row.job_id, expected='running', target='paused')
                if page.next_after_id is None:
                    break
                page = jobs.list_jobs(db, status='running', page=PageRequest(page.next_after_id, 1000, page.upper_id))
        await asyncio.wrap_future(submit_write(ctx.handle, pause))
        await self.supervisor.join(ctx.project_id)
        while ctx.users:
            await asyncio.sleep(.01)
        await self.blocking(lambda: projects.close_project(ctx.handle))
        self.current = None
        self.runners = {k: v for k, v in self.runners.items() if k[0] != ctx.project_id}
        self.events.publish('project.closed', project_id=ctx.project_id,
                            payload={'project_id': ctx.project_id, 'generation': ctx.generation, 'recovery_pending': False})

    async def close_project(self):
        async with self.transition:
            await self._close_project()

    async def close(self, reason='shutdown'):
        if self.close_task is None:
            async def finish():
                self.draining = self.supervisor.draining = True
                if hasattr(self, 'prompts'):
                    await self.prompts.pause()
                await self.close_project()
                await self.supervisor.join()
                self.product.close()
                self.pool.shutdown(wait=True)
            self.close_task = asyncio.create_task(finish())
        await asyncio.shield(self.close_task)
