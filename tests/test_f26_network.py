import asyncio
from dataclasses import replace
import unittest
from stylelora.llm.types import CancelToken, LlmFailure
from stylelora.resources.network import NetworkController, NetworkPolicy


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_T23_three_limits_and_shared_reference(self):
        controller = NetworkController()
        first = await controller.acquire('a', ('shared',), cancel=CancelToken(), deadline=float('inf'))
        waiting = asyncio.create_task(controller.acquire('b', ('shared',), cancel=CancelToken(), deadline=float('inf')))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        second = await controller.acquire('b', ('other',), cancel=CancelToken(), deadline=float('inf'))
        self.assertEqual(controller.active, 2)
        self.assertEqual(controller.tokens['shared'], 1)
        self.assertEqual(controller.endpoints['b'], 1)
        await first.release()
        third = await waiting
        self.assertEqual(controller.active, 2)
        await second.release()
        await third.release()
        self.assertEqual(controller.active, 0)

    async def test_T23_endpoint_limit_does_not_hold_global_capacity(self):
        controller = NetworkController(NetworkPolicy(global_limit=3, endpoint_default=1))
        first = await controller.acquire('a', ('one',), cancel=CancelToken(), deadline=float('inf'))
        cancel = CancelToken()
        waiting = asyncio.create_task(controller.acquire('a', ('two',), cancel=cancel, deadline=float('inf')))
        await asyncio.sleep(0)
        second = await controller.acquire('b', ('three',), cancel=CancelToken(), deadline=float('inf'))
        self.assertEqual(controller.active, 2)
        self.assertFalse(waiting.done())
        cancel.cancel()
        with self.assertRaises(LlmFailure) as failure:
            await waiting
        self.assertEqual(failure.exception.outcome, 'not_sent')
        await first.release()
        await second.release()

    async def test_T24_cooldown_rotation_and_no_held_slot(self):
        now = [0.0]
        async def advance(seconds):
            self.assertEqual(controller.active, 0)
            now[0] += seconds
        controller = NetworkController(clock=lambda: now[0], sleep=advance)
        first = await controller.acquire('a', ('one', 'two'), cancel=CancelToken(), deadline=10)
        self.assertEqual(first.token_ref, 'one')
        await first.release()
        controller.cooldown('a', 'one', retry_after_s=3, attempt=0)
        second = await controller.acquire('a', ('one', 'two'), cancel=CancelToken(), deadline=10)
        self.assertEqual(second.token_ref, 'two')
        await second.release()
        controller.cooldown('a', 'two', retry_after_s=4, attempt=0)
        third = await controller.acquire('a', ('one', 'two'), cancel=CancelToken(), deadline=10)
        self.assertGreaterEqual(now[0], 3)
        self.assertEqual(third.token_ref, 'one')
        await third.release()

    async def test_T24_retry_after_longer_than_cap_and_deadline(self):
        now = [0.0]
        async def advance(seconds):
            now[0] += seconds
        controller = NetworkController(clock=lambda: now[0], sleep=advance)
        controller.cooldown('a', 'one', retry_after_s=120, attempt=20)
        self.assertEqual(controller.cooldowns['a', 'one'], 120)
        with self.assertRaises(LlmFailure) as failure:
            await controller.acquire('a', ('one',), cancel=CancelToken(), deadline=1)
        self.assertEqual(failure.exception.code, 'resource:timeout')
        self.assertEqual(controller.active, 0)
        self.assertEqual(controller.waiters, [])

    async def test_T25_cancel_and_idempotent_release(self):
        controller = NetworkController()
        token = CancelToken()
        token.cancel()
        with self.assertRaises(LlmFailure) as failure:
            await controller.acquire('a', ('one',), cancel=token, deadline=float('inf'))
        self.assertEqual(failure.exception.outcome, 'not_sent')
        lease = await controller.acquire('a', ('one',), cancel=CancelToken(), deadline=float('inf'))
        await lease.release()
        await lease.release()
        self.assertEqual(controller.active, 0)
        self.assertEqual(controller.tokens['one'], 0)

    async def test_T26_hot_reduce_keeps_inflight(self):
        controller = NetworkController()
        first = await controller.acquire('a', ('one',), cancel=CancelToken(), deadline=float('inf'))
        second = await controller.acquire('b', ('two',), cancel=CancelToken(), deadline=float('inf'))
        self.assertEqual(controller.configure(replace(controller.policy, global_limit=1)), 1)
        waiting = asyncio.create_task(controller.acquire('c', ('three',), cancel=CancelToken(), deadline=float('inf')))
        await asyncio.sleep(0)
        self.assertEqual(controller.active, 2)
        await first.release()
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        await second.release()
        third = await waiting
        self.assertEqual(controller.active, 1)
        await third.release()

    async def test_T26_invalid_policy_and_round_robin(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(LlmFailure):
                NetworkPolicy(global_limit=value)
        controller = NetworkController()
        seen = []
        for _ in range(4):
            async with await controller.acquire('a', ('one', 'two'), cancel=CancelToken(), deadline=float('inf')) as lease:
                seen.append(lease.token_ref)
        self.assertEqual(seen, ['one', 'two', 'one', 'two'])
