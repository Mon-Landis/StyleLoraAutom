import asyncio
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from stylelora.runtime import Runtime
from stylelora.runtime.contracts import Failure
from stylelora.runtime.events import EventBridge
from stylelora.runtime.instance import bind_socket, InstanceLock
from stylelora.executors.events import EventBus


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_T02_retained_socket_and_options(self):
        sock = bind_socket()
        try:
            self.assertEqual(sock.getsockname()[0], '127.0.0.1')
            with self.assertRaises(OSError):
                bind_socket(port=sock.getsockname()[1])
            for options in ({'host':'0.0.0.0'}, {'workers':2}, {'reload':True}):
                with self.assertRaises(Failure):
                    bind_socket(**options)
        finally:
            sock.close()

    async def test_T03_T05_open_reuse_failed_switch(self):
        with TemporaryDirectory() as temporary:
            rt = Runtime(Path(temporary)/'product')
            try:
                path = Path(temporary)/'a.slproj'
                one = await rt.open(path, create={'name':'a','trigger_word':''})
                two = await rt.open(path)
                self.assertEqual(one['project_id'], two['project_id'])
                with self.assertRaises(Exception):
                    await rt.open(Path(temporary)/'absent.slproj')
                self.assertIsNone(rt.current)
                with self.assertRaises(Failure):
                    rt.require_project(one['project_id'])
            finally:
                await rt.close()

    async def test_T27_T31_supervisor_owns_work_before_return(self):
        with TemporaryDirectory() as temporary:
            rt = Runtime(temporary)
            gate = asyncio.Event()
            async def work():
                await gate.wait()
                return {'done':True}
            value = rt.supervisor.submit('test', work)
            self.assertIn(value['operation_id'], rt.supervisor.tasks)
            self.assertEqual(rt.supervisor.get(value['operation_id'])['status'], 'pending')
            gate.set()
            await rt.supervisor.join()
            self.assertEqual(rt.supervisor.get(value['operation_id'])['result'], {'done':True})
            await asyncio.gather(rt.close(), rt.close())
            with self.assertRaises(Failure):
                rt.supervisor.submit('late', work)

    async def test_T06_T07_rotation_and_expiry(self):
        with TemporaryDirectory() as temporary:
            clock = [0]
            rt = Runtime(temporary, clock=lambda:clock[0])
            other = Runtime(temporary)
            try:
                self.assertNotEqual(rt.token, other.token)
                import base64
                self.assertEqual(len(base64.urlsafe_b64decode(rt.token+'=')), 32)
                code = rt.bootstrap_code()
                clock[0] = 60
                with self.assertRaises(Failure):
                    rt.redeem(code, 'http://127.0.0.1:8000')
            finally:
                await rt.close()
                await other.close()

    async def test_T34_T35_job_bridge_replay_gap(self):
        bridge, bus = EventBridge(capacity=2), EventBus()
        unsubscribe = bridge.attach_jobs(bus, 'project')
        for i in range(3):
            bus.emit(1, 'job.image_completed', image_id=i, counts={'done':i+1})
        key, queue, replay = bridge.connect(bridge.epoch+':1')
        self.assertEqual([e['sequence'] for _, e in replay], [2,3])
        bridge.disconnect(key)
        key, queue, replay = bridge.connect(bridge.epoch+':0')
        self.assertEqual(replay[0][0], 'stream.resync_required')
        bridge.disconnect(key)
        key, queue, replay = bridge.connect('old:10')
        self.assertEqual(replay[0][1]['reason'], 'epoch_changed')
        bridge.disconnect(key)
        unsubscribe()
        with self.assertRaises(Failure):
            bridge.connect('bad')

    async def test_T36_T37_backpressure_subscriptions_and_filter(self):
        bridge = EventBridge()
        subscriptions = [bridge.connect() for _ in range(4)]
        with self.assertRaises(Failure):
            bridge.connect()
        for i in range(300):
            bridge.publish('job.image_completed', payload={'image_id':i})
        await asyncio.sleep(.01)
        key, queue, _ = subscriptions[0]
        self.assertEqual((await queue.get())[0], 'stream.resync_required')
        for key, _, _ in subscriptions:
            bridge.disconnect(key)
        key, queue, replay = bridge.connect(bridge.epoch+':299', topics=('source',))
        self.assertEqual(replay, [])
        bridge.publish('source.progress', resource={'id':1})
        bridge.publish('source.progress', resource={'id':1})
        self.assertEqual(bridge.sequence, 301)
        bridge.disconnect(key)


class InstanceTests(unittest.TestCase):
    def test_T01_mutex_blocks_other_thread(self):
        import concurrent.futures
        name = 'Local\\StyleLoraAutom-test-'+uuid4().hex
        lock = InstanceLock(name)
        try:
            def contender():
                try:
                    other = InstanceLock(name)
                except Failure as exc:
                    return exc.status
                else:
                    other.close()
                    return 200
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                self.assertEqual(pool.submit(contender).result(), 423)
        finally:
            lock.close()
