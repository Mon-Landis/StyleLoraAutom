import asyncio
from dataclasses import replace
import unittest
from stylelora.runtime.tasks import *
from stylelora.runtime.events import EventBridge
from stylelora.runtime.cancellation import StopCoordinator
from stylelora.runtime.contracts import Failure, plain


class FakeAdapter:
    def __init__(self, kind='job', status='running', cancelable=True):
        self.value = TaskDTO('', kind, None, status, cancelable=cancelable)
        self.calls = []
        self.exited = asyncio.Event()

    async def snapshot(self, identity):
        return self.value

    async def stop(self, identity, request):
        self.calls.append(request.reason)
        self.value = replace(self.value, status='paused')
        return StopReceipt('', 'canceling', True, False, '0')

    async def wait(self, identity):
        await self.exited.wait()

    def worker_exited(self, identity):
        return self.exited.is_set()


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.events = EventBridge(clock=lambda: self.now)
        self.registry = TaskRegistry(self.events, clock=lambda: self.now)

    def register(self, adapter, identity='one', project=None):
        return self.registry.register(adapter, ResourceRef(adapter.value.kind, identity), project)

    async def test_T12_six_kinds_and_alias(self):
        for kind in KINDS:
            adapter = FakeAdapter(kind)
            identity = self.register(adapter)
            self.assertEqual(identity, self.register(adapter))
            self.assertIsNone((await self.registry.get(identity)).project)
        self.assertEqual(len((await self.registry.list()).items), 6)

    async def test_T13_stop_replay_and_cross_task_key(self):
        adapter = FakeAdapter()
        identity = self.register(adapter)
        receipt = await self.registry.cancel(identity, 'key')
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.status, 'canceling')
        self.assertTrue((await self.registry.cancel(identity, 'key')).no_op)
        other = self.register(FakeAdapter(), 'two')
        with self.assertRaises(Failure) as error:
            await self.registry.cancel(other, 'key')
        self.assertEqual(error.exception.status, 409)
        adapter.exited.set()
        await self.registry.coordinator.workers[identity]
        value = await self.registry.get(identity)
        self.assertEqual((value.status, value.reason_code), ('paused', 'user'))
        self.assertEqual(adapter.calls, ['user'])

    async def test_T13_terminal_and_not_cancelable(self):
        terminal = self.register(FakeAdapter(status='done'))
        self.assertTrue((await self.registry.cancel(terminal, 'done')).no_op)
        locked = self.register(FakeAdapter(cancelable=False), 'locked')
        with self.assertRaises(Failure) as error:
            await self.registry.cancel(locked, 'locked')
        self.assertEqual(error.exception.code, 'task:not_cancelable')
        with self.assertRaises(Failure) as error:
            await self.registry.get('unknown')
        self.assertEqual(error.exception.status, 404)

    async def test_T14_project_generation_isolation(self):
        old = self.register(FakeAdapter(), project=ProjectRef('generation1', 'project'))
        new = self.register(FakeAdapter(), project=ProjectRef('generation2', 'project'))
        self.assertNotEqual(old, new)
        self.assertEqual(len((await self.registry.list(TaskQuery(project_id='generation2'))).items), 1)

    async def test_T15_frozen_pagination_expiry_and_eviction(self):
        adapter = FakeAdapter(status='done')
        adapter.exited.set()
        first = self.register(adapter)
        active = self.register(FakeAdapter(), 'active')
        page = await self.registry.list(TaskQuery(limit=1))
        self.register(FakeAdapter(), 'new')
        second = await self.registry.list(TaskQuery(limit=1, cursor=page.next_cursor))
        self.assertEqual([v.task_id for v in second.items], [active])
        self.assertIsNone(second.next_cursor)
        self.now = 61
        with self.assertRaises(Failure) as error:
            await self.registry.list(TaskQuery(limit=1, cursor=page.next_cursor))
        self.assertEqual(error.exception.code, 'task:cursor_expired')
        self.now = 601
        await self.registry.list()
        with self.assertRaises(Failure) as error:
            await self.registry.get(first)
        self.assertEqual(error.exception.status, 410)
        self.assertEqual((await self.registry.get(active)).status, 'running')

    async def test_T16_T17_sse_version_throttle_snapshot_boundary(self):
        key, queue, _ = self.events.connect(topics=('task',))
        self.addCleanup(lambda: self.events.disconnect(key))
        adapter = FakeAdapter()
        identity = self.register(adapter)
        page = await self.registry.list()
        adapter.value = replace(adapter.value, progress=ProgressDTO(1, 3))
        one = await self.registry.get(identity)
        adapter.value = replace(adapter.value, progress=ProgressDTO(2, 3))
        two = await self.registry.get(identity)
        adapter.value = replace(adapter.value, status='done', progress=ProgressDTO(3, 3))
        final = await self.registry.get(identity)
        self.assertEqual((one.version, two.version, final.version), ('2', '3', '4'))
        kinds = [entry[1] for entry in self.events.history]
        self.assertEqual(kinds, ['task.created', 'task.progress', 'task.finished'])
        self.assertEqual(page.event_cursor, self.events.epoch+':1')
        self.assertEqual(self.events.history[-1][2]['payload']['progress']['completed'], 3)
        self.assertFalse({'path', 'token', 'prompt', 'result'} & set(plain(final)))

    async def test_T18_notify_all_before_wait_user_shutdown(self):
        for reason in ('user', 'shutdown'):
            registry = TaskRegistry(EventBridge())
            coordinator = StopCoordinator(registry)
            adapters = [FakeAdapter(kind) for kind in KINDS]
            for adapter in adapters:
                registry.register(adapter, ResourceRef(adapter.value.kind, 'one'), None)
            drain = asyncio.create_task(coordinator.drain(reason=reason))
            for _ in range(20):
                await asyncio.sleep(0)
                if all(a.calls for a in adapters):
                    break
            self.assertTrue(all(a.calls == [reason] for a in adapters))
            self.assertFalse(drain.done())
            for adapter in adapters:
                adapter.exited.set()
            await drain

    async def test_T21_persistence_failure_remains_canceling(self):
        class Broken(FakeAdapter):
            async def stop(self, identity, request):
                raise OSError('injected')
        identity = self.register(Broken())
        with self.assertRaises(Failure) as error:
            await self.registry.cancel(identity, 'stop')
        self.assertEqual(error.exception.code, 'task:stop_persist_failed')
        self.assertEqual((await self.registry.get(identity)).status, 'canceling')

    async def test_T14_completion_wins_stop_CAS(self):
        from stylelora.project_db.errors import StateConflict
        class Racing(FakeAdapter):
            async def stop(self, identity, request):
                self.value = replace(self.value, status='done', progress=ProgressDTO(1, 1))
                raise StateConflict('completed')
        adapter = Racing()
        identity = self.register(adapter)
        receipt = await self.registry.cancel(identity, 'race')
        self.assertTrue(receipt.no_op)
        self.assertEqual(receipt.status, 'done')
        self.assertEqual((await self.registry.get(identity)).progress.completed, 1)

    async def test_T16_concurrent_refresh_keeps_monotonic_versions(self):
        class Yielding(FakeAdapter):
            async def snapshot(self, identity):
                await asyncio.sleep(0)
                return self.value
        adapter = Yielding()
        identity = self.register(adapter)
        results = await asyncio.gather(*(self.registry.get(identity) for _ in range(5)))
        self.assertEqual({r.version for r in results}, {'1'})
        self.assertEqual(len(self.events.history), 1)

    async def test_T15_T22_restart_recovers_persisted_operations_only(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from stylelora.runtime.product import ProductStore
        from stylelora.runtime.supervisor import TaskSupervisor
        with TemporaryDirectory() as root:
            product = ProductStore(Path(root))
            try:
                supervisor = TaskSupervisor(EventBridge())
                supervisor.attach_store(product)
                async def completed():
                    return {'items': [{'status':'deleted'}]}
                persistent = supervisor.submit('cleanup', completed)
                transient = supervisor.submit('picker', completed)
                await supervisor.join()
                restored = TaskSupervisor(EventBridge())
                restored.attach_store(product)
                self.assertIn(persistent['operation_id'], restored.records)
                self.assertNotIn(transient['operation_id'], restored.records)
                self.assertEqual(restored.records[persistent['operation_id']]['completed'], 1)
                self.assertEqual(restored.tasks, {})
                self.assertEqual(restored.cancel_handles, {})
            finally:
                product.close()
