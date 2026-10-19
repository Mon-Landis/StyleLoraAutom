import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event
import unittest
from stylelora.resources.compute import *
from stylelora.vision.device import DeviceInfo
from stylelora.llm.types import CancelToken
from stylelora.runtime.product import ProductStore
from stylelora.runtime.contracts import Failure


class ComputeTests(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        return ComputeController(ComputePolicy(reserve_bytes=10, reserve_ratio=.2),
                                 probe=lambda: DeviceInfo('cuda', 100, 100))

    async def test_T27_shared_slots_and_resident_reference_count(self):
        controller = self.controller()
        controller.register_resident('model', 'gpu:0', 20)
        controller.register_resident('model', 'gpu:0', 20)
        demand = ComputeDemand('wd', 'gpu:0', 'onnx', 'model', 20, 10, 4)
        first = await controller.acquire(demand, cancel=CancelToken(), deadline=float('inf'))
        self.assertEqual((first.actual_batch, first.reserved_bytes), (4, 40))
        waiting = asyncio.create_task(controller.acquire(replace(demand, owner='fake-vlm'), cancel=CancelToken(), deadline=float('inf')))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        await first.release()
        second = await waiting
        await second.release()
        controller.release_resident('model', 'gpu:0')
        self.assertIn(('gpu:0', 'model'), controller.residents)
        controller.release_resident('model', 'gpu:0')
        self.assertEqual(controller.residents, {})

    async def test_T28_budget_and_impossible_demand(self):
        controller = self.controller()
        lease = await controller.acquire(ComputeDemand('wd', 'gpu:0', 'onnx', 'm', 60, 10, 4), cancel=CancelToken(), deadline=float('inf'))
        self.assertEqual(lease.actual_batch, 2)
        await lease.release()
        with self.assertRaises(LlmFailure) as error:
            await controller.acquire(ComputeDemand('wd', 'gpu:0', 'onnx', 'm', 80, 1, 1), cancel=CancelToken(), deadline=float('inf'))
        self.assertEqual(error.exception.code, 'resource:insufficient')
        self.assertEqual(controller.reserved['gpu:0'], 0)

    async def test_T29_worker_exit_precedes_release(self):
        controller = self.controller()
        entered, finish = Event(), Event()
        def work():
            entered.set()
            finish.wait(5)
        async def infer():
            async with await controller.acquire(ComputeDemand('wd', 'cpu', 'onnx', 'm', 0, 0, 4), cancel=CancelToken(), deadline=float('inf')):
                await run_worker(work)
        task = asyncio.create_task(infer())
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        self.assertEqual(controller.slots['cpu'], 1)
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(controller.slots['cpu'], 0)

    async def test_T30_settings_restart_CAS_and_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ProductStore(Path(temporary))
            try:
                original = store.get_resources()
                value = dict(expected_revision=original['revision'], network=original['network'], compute=original['compute'])
                value['compute']['gpu_budget_bytes'] = 50
                saved = store.update_resources(value)
                self.assertEqual(saved['revision'], '1')
                with self.assertRaises(Failure):
                    store.update_resources(value)
            finally:
                store.close()
            reopened = ProductStore(Path(temporary))
            try:
                self.assertEqual(reopened.get_resources(), saved)
            finally:
                reopened.close()
        for value in (-.1, 1, float('nan')):
            with self.assertRaises(LlmFailure):
                ComputePolicy(reserve_ratio=value)

    def test_legacy_resources_migrate_defaults_keep_explicit(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'resources.json'
            base = ProductStore(Path(temporary))
            try:
                network = base.get_resources()['network'] | {'global_limit': 2, 'endpoint_default': 2, 'token_default': 3}
                compute = base.get_resources()['compute']
            finally:
                base.close()
            path.write_text(json.dumps(dict(revision='5', network=network, compute=compute)), encoding='utf-8')
            store = ProductStore(Path(temporary))
            try:
                migrated = store.get_resources()
            finally:
                store.close()
            self.assertEqual(migrated['revision'], '5')
            self.assertEqual(migrated['network']['global_limit'], -1)
            self.assertEqual(migrated['network']['endpoint_default'], 4)
            self.assertEqual(migrated['network']['token_default'], 3)
            self.assertNotIn('_policy_version', migrated)
            stored = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(stored['_policy_version'], 2)
            # 迁移后用户再设回 2 不应被再次迁移
            store = ProductStore(Path(temporary))
            try:
                value = dict(expected_revision='5', network=migrated['network'] | {'global_limit': 2}, compute=migrated['compute'])
                store.update_resources(value)
                self.assertEqual(store.get_resources()['network']['global_limit'], 2)
            finally:
                store.close()
