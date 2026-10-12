"""One control lock and one task per source. DB commits precede all submissions."""
import asyncio
import time
import weakref
from uuid import uuid4
from stylelora.backends.types import BackendError, digest
from stylelora.project_db import commands, read_snapshot, submit_write
from stylelora.project_db.errors import StateConflict, CommitUnknown
from stylelora.project_db.repositories import sources
from stylelora.project_db.recovery import reconcile_item
from stylelora.generation.profile import from_snapshot, validate_profile
from stylelora.generation.profile import build_request
from stylelora.generation.artifacts import AttemptReceipt, write_receipt, request_digest, accept_result
from stylelora.generation.recovery import probe, prepare_reconciliation, source_gate, recover_locked
from .cost import CostCoordinator, all_items
from .events import EventBus, ProgressEvent

_controllers = weakref.WeakValueDictionary()


class SourceRunner:
    def __init__(self, project, source_id, backend_factory, *, events=None, catalog=None,
                 submit_interval_s=1, clock=time.monotonic, sleep=asyncio.sleep):
        key = (id(project.writer), source_id)
        if key in _controllers:
            raise StateConflict('Source already has a controller')
        _controllers[key] = self
        self.project, self.source_id, self.backend_factory = project, source_id, backend_factory
        self.events, self.catalog = events or EventBus(), catalog
        self.control = source_gate(project, source_id)
        self.task = None
        self.cycle_id = None
        self.sequence = 0
        self.cost = CostCoordinator(project, source_id)
        self.interval, self.clock, self.sleep = submit_interval_s, clock, sleep
        self.last_submit = None
        self.reason = None
        self.existing_artifacts = set()
        self.blocked_backend = None

    def source(self):
        with read_snapshot(self.project) as db:
            return sources.get_source(db, self.source_id)

    async def write(self, work):
        return await asyncio.wrap_future(submit_write(self.project, work))

    async def emit(self, phase, item_id=None, estimate=None):
        with read_snapshot(self.project) as db:
            source = sources.get_source(db, self.source_id)
            counts = sources.count_item_states(db, self.source_id)
        self.sequence += 1
        await self.events.publish(ProgressEvent(self.source_id, self.cycle_id, self.sequence,
            source.status, counts, item_id, phase, self.reason, estimate, self.cost.account_status()))

    async def _preflight(self):
        if self.blocked_backend is not None:
            raise BackendError('profile:restore_pending', 'preflight')
        try:
            backend = await self.backend_factory()
        except Exception:
            if self.source().status == 'running':
                await self._transition('paused', 'discovery:not_running')
            raise
        try:
            if await recover_locked(self.project, self.source_id, backend):
                raise BackendError('profile:restore_pending', 'preflight')
            config = from_snapshot(self.source().config_json)
            caps = await backend.capabilities()
            validate_profile(config.profile, caps)
            if config.profile.effort == 'medium' and self.source().negative_prompt.strip():
                raise BackendError('profile:effort_conflict', 'preflight')
            return backend, config, caps
        except BaseException:
            await backend.close()
            raise

    def _launch(self, backend, config, caps):
        cycle = self.project.writer.cycles.get(self.source_id)
        self.cycle_id = cycle[0] if cycle else uuid4().hex
        self.reason = None
        self.task = asyncio.create_task(self.run_cycle(self.cycle_id, backend, config, caps))

    async def start(self, items):
        async with self.control:
            if self.task is not None and not self.task.done():
                return await self.write(lambda db: commands.start_source(db, self.source_id, items))
            backend, config, caps = await self._preflight()
            try:
                source = await self.write(lambda db: commands.start_source(db, self.source_id, items))
                if source.status == 'running':
                    self._launch(backend, config, caps)
                else:
                    await backend.close()
                return source
            except BaseException:
                await backend.close()
                raise

    async def resume(self, *, expected_cycle_id=None):
        # Wait for the previous cycle's finally before acquiring a new page lease.
        if self.task is not None and not self.task.done():
            if self.source().status == 'running':
                if expected_cycle_id is None:
                    return self.source()
            else:
                await asyncio.shield(self.task)
        async with self.control:
            if expected_cycle_id in self.cost.confirmations:
                return self.cost.confirmations[expected_cycle_id]
            if expected_cycle_id is not None:
                current = self.project.writer.cycles.get(self.source_id)
                if self.source().status != 'paused' or current is None or current[0] != expected_cycle_id:
                    raise StateConflict('Stale cost confirmation')
            if self.source().status == 'running':
                return self.source()
            backend, config, caps = await self._preflight()
            try:
                if expected_cycle_id is not None:
                    source = await self.cost.confirm(expected_cycle_id)
                else:
                    source = (await self.write(lambda db: commands.transition_source(db, self.source_id,
                        expected='paused', target='running', reason='resume'))).value
                self._launch(backend, config, caps)
                return source
            except BaseException:
                await backend.close()
                raise

    async def confirm_cost_and_resume(self, expected_cycle_id):
        return await self.resume(expected_cycle_id=expected_cycle_id)

    async def _transition(self, target, reason):
        source = self.source()
        if source.status == target or source.status in ('done', 'canceled'):
            return source
        result = await self.write(lambda db: commands.transition_source(db, self.source_id,
            expected=source.status, target=target, reason=reason))
        self.reason = reason
        self.cycle_id = self.project.writer.cycles.get(self.source_id, (None, False))[0]
        await self.emit(target)
        return result.value

    async def pause(self, reason='user:paused'):
        from stylelora.project_db.validation import error_reason
        error_reason(reason)
        async with self.control:
            return await self._transition('paused', reason)

    async def stop(self):
        async with self.control:
            return await self._transition('canceled', 'user:stopped')

    async def set_cost_policy(self, policy):
        async with self.control:
            return await self.write(lambda db: commands.set_cost_policy(db, self.source_id, policy=policy))

    async def retry_failed(self, item_id, expected_attempt):
        async with self.control:
            with read_snapshot(self.project) as db:
                if sources.get_item(db, item_id).source_id != self.source_id:
                    raise StateConflict('Item belongs to another source')
            return (await self.write(lambda db: commands.retry_failed_item(db, item_id, expected_attempt=expected_attempt))).value

    def _running(self, cycle):
        current = self.project.writer.cycles.get(self.source_id)
        return self.source().status == 'running' and self.cycle_id == cycle and current is not None and current[0] == cycle

    async def run_cycle(self, cycle_id, backend, config, caps):
        handle, preparation, sent_item = None, None, None
        try:
            async with self.control:
                if not self._running(cycle_id) or all_items(self.project, self.source_id, ('sent',)):
                    raise BackendError('profile:restore_pending', 'preflight')
                handle = await backend.apply_profile(config.profile)
            for item in all_items(self.project, self.source_id, ('pending',)):
                if not self._running(cycle_id):
                    break
                source = self.source()
                request = build_request(config, source, item, self.project.root)
                if request.save_path.exists() or item.item_id in self.existing_artifacts:
                    evidence = await probe(self.project, item, backend)
                    if item.item_id not in self.existing_artifacts or evidence.verdict != 'complete':
                        self.existing_artifacts.add(item.item_id)
                        raise BackendError('artifact:save_path_exists', 'prepare')
                    async with self.control:
                        if not self._running(cycle_id):
                            break
                        prepare_reconciliation(self.project, item, evidence)
                        await self.write(lambda db: commands.mark_sent(db, item.item_id))
                        await self.write(lambda db: reconcile_item(db, evidence))
                    continue
                receipt = AttemptReceipt(source.source_id, item.item_id, request.attempt,
                    request_digest(request, self.project.root), digest(source.config_json),
                    request.save_path.relative_to(self.project.root).as_posix(),
                    cycle_id=cycle_id, run_token=cycle_id,
                    backend_instance=getattr(backend, 'instance', backend.__class__.__name__))
                write_receipt(self.project.root, receipt)
                preparation = await backend.prepare(request)
                if self.last_submit is not None:
                    delay = self.interval - (self.clock() - self.last_submit)
                    if delay > 0:
                        await self.sleep(delay)
                async with self.control:
                    if not self._running(cycle_id):
                        break
                    if caps.supports_cost:
                        decision = await self.cost.evaluate(item, preparation.estimated_anlas)
                        if decision != 'submit':
                            self.reason = 'cost:denied' if self.source().cost_policy == 'deny' else 'cost:confirmation_required'
                            self.cycle_id = self.project.writer.cycles[self.source_id][0]
                            await self.emit('cost', item.item_id, preparation.estimated_anlas)
                            break
                    marked = await self.write(lambda db: commands.mark_sent(db, item.item_id))
                    if not marked.changed:
                        raise BackendError('dal:state_conflict', 'mark_sent')
                    sent_item = marked.value
                # Pause/stop after COMMIT does not revoke this one authorized submission.
                self.last_submit = self.clock()
                submitted = preparation
                preparation = None
                result = await backend.submit(submitted)
                await accept_result(self.project, source, sent_item, request, result, catalog=self.catalog)
                sent_item = None
                await self.emit('complete', item.item_id)
            if self._running(cycle_id):
                with read_snapshot(self.project) as db:
                    counts = sources.count_item_states(db, self.source_id)
                if not counts['pending'] and not counts['sent']:
                    # Terminal transition occurs after settings restoration below.
                    pass
        except BackendError as exc:
            if exc.code == 'artifact:save_path_exists' and 'item' in locals():
                self.existing_artifacts.add(item.item_id)
            if sent_item is not None and exc.outcome == 'rejected':
                await self.write(lambda db: commands.fail_item(db, sent_item.item_id,
                    attempt=sent_item.attempts, error=exc.code))
            await self.pause(exc.code)
        except CommitUnknown:
            # Never turn an uncertain DB COMMIT into permission to send.
            self.reason = 'dal:commit_unknown'
            self.cycle_id = None
            self.project.writer.cycles.pop(self.source_id, None)
            await self.emit('error')
        except asyncio.CancelledError:
            await self.pause('user:paused')
        except Exception:
            await self.pause('generation:interrupted')
        finally:
            try:
                if preparation is not None:
                    await backend.cancel_preparation(preparation)
                if handle is not None:
                    await backend.restore(handle)
                await backend.close()
            except Exception:
                self.blocked_backend = backend
                await self.pause('profile:restore_pending')
            if self._running(cycle_id):
                with read_snapshot(self.project) as db:
                    counts = sources.count_item_states(db, self.source_id)
                if not counts['pending'] and not counts['sent']:
                    async with self.control:
                        await self._transition('done', 'generation:complete')

    async def wait(self):
        if self.task is not None:
            await asyncio.shield(self.task)

    async def close(self):
        if self.source().status == 'running':
            await self.pause('user:paused')
        await self.wait()
        self.project.writer.cycles.pop(self.source_id, None)
        _controllers.pop((id(self.project.writer), self.source_id), None)
