import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from f02_support import *
from stylelora.backends.nai_launcher.discovery import discover
from stylelora.backends.nai_launcher.client import (connect, encode_request, BODY_LIMIT, cancel_request,
                                                    close_session, response_messages)
from stylelora.backends.nai_launcher.compatibility import verify_compatibility, ServerInfo


class DiscoveryTests(unittest.TestCase):
    def test_T01_discovery_priorities_restart_invalid_and_redaction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(BackendError) as error:
                discover(root, environ={})
            self.assertEqual(error.exception.code, 'discovery:not_running')
            path = root / 'nai-launcher/mcp-server.json'
            path.parent.mkdir()
            path.write_text('{', encoding='utf-8')
            with self.assertRaises(BackendError):
                discover(root, environ={})
            value = {'endpoint': 'http://127.0.0.1:12345/mcp', 'port': 12345, 'token': 'file-secret',
                     'pid': 3, 'protocol_versions': ['2025-06-18'], 'app_version': 'fake-1'}
            path.write_text(json.dumps(value), encoding='utf-8')
            self.assertEqual(discover(root, environ={}).token, 'file-secret')
            self.assertEqual(discover(root, environ={'NAI_LAUNCHER_MCP_TOKEN': 'env-secret'}).token, 'env-secret')
            info = discover(root, explicit_token='explicit-secret', environ={'NAI_LAUNCHER_MCP_TOKEN': 'env-secret'})
            self.assertEqual(info.token, 'explicit-secret')
            self.assertNotIn('secret', repr(info))
            value.update(port=23456, endpoint='http://127.0.0.1:23456/mcp')
            path.write_text(json.dumps(value), encoding='utf-8')
            self.assertEqual(discover(root, environ={}).port, 23456)
            for endpoint in ('http://example.com:23456/mcp', 'http://user:password@127.0.0.1:23456/mcp', 'http://127.0.0.1:12345/mcp'):
                value['endpoint'] = endpoint
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(BackendError):
                    discover(root, environ={})

    def test_T04_utf8_body_exact_limit(self):
        base = len(encode_request({'value': ''}))
        payload = {'value': '中' * ((BODY_LIMIT - base) // 3)}
        payload['value'] += 'x' * (BODY_LIMIT - len(encode_request(payload)))
        self.assertEqual(len(encode_request(payload)), BODY_LIMIT)
        payload['value'] += 'x'
        with self.assertRaises(BackendError) as caught:
            encode_request(payload)
        self.assertEqual(caught.exception.outcome, 'not_sent')

    def test_T06_manifest_order_types_versions_and_missing(self):
        self.assertEqual(schema_hash(TOOLS), schema_hash(tuple(reversed(TOOLS))))
        kwargs = dict(app_version='fake-1', protocol_version='2025-06-18', manifest=MANIFEST)
        self.assertTrue(verify_compatibility(ServerInfo('fake', 'fake-1'), TOOLS, **kwargs).accepted)
        self.assertFalse(verify_compatibility(ServerInfo('fake', '4.3.0'), TOOLS, **kwargs).accepted)
        self.assertFalse(verify_compatibility(ServerInfo('fake', 'fake-1'), TOOLS[:-1], **kwargs).accepted)
        changed = (replace(TOOLS[0], input_schema={'type': 'string'}), *TOOLS[1:])
        self.assertFalse(verify_compatibility(ServerInfo('fake', 'fake-1'), changed, **kwargs).accepted)
        self.assertFalse(verify_compatibility(ServerInfo('fake', 'fake-1'), TOOLS,
            app_version='fake-1', protocol_version='2025-06-18').accepted)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_T02_json_sse_pagination_headers_notification(self):
        for sse in (False, True):
            fake = FakeLauncher(sse=sse)
            fake.settings['prompt'] = '中文'
            session = await connect(fake.info, transport=fake)
            self.assertEqual([v['method'] for v in fake.calls[:4]], ['initialize', 'notifications/initialized', 'tools/list', 'tools/list'])
            self.assertEqual(len(session.tools), 8)
            reply = await session.call('get_generation_settings', {}, kind='read')
            self.assertEqual(reply.payload['settings']['prompt'], '中文')
            for headers in fake.headers:
                self.assertEqual(headers['Authorization'], 'Bearer DO-NOT-LOG-SECRET')
            self.assertNotIn('Mcp-Session-Id', fake.headers[0])
            self.assertEqual(fake.headers[-1]['MCP-Protocol-Version'], '2025-06-18')
            self.assertEqual(fake.headers[-1]['Mcp-Session-Id'], 'session-1')
            await cancel_request(session, 'request')
            self.assertNotIn('id', fake.calls[-1])
            await close_session(session)

    async def test_T03_single_flight_read_404(self):
        fake = FakeLauncher()
        session = await connect(fake.info, transport=fake, sleep=no_sleep)
        fake.expired.add('session-1')
        await asyncio.gather(*(session.call('get_generation_settings', {}, kind='read') for _ in range(2)))
        self.assertEqual(fake.counts['initialize'], 2)
        self.assertEqual(fake.counts['submit_generation'], 0)

    async def test_T03_unknown_submit_never_replayed(self):
        for fault in (404, 502, 'timeout', 'eof'):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                fake = FakeLauncher(submit_fault=fault)
                backend = await fake.backend(Path(directory) / 'journal')
                handle = await backend.apply_profile(PROFILE)
                request = GenerationRequest(item_id=1, attempt=1, positive='style', negative='', width=64, height=64,
                                             save_path=Path(directory) / 'out.png')
                prepared = await backend.prepare(request)
                with self.assertRaises(BackendError) as error:
                    await backend.submit(prepared)
                self.assertEqual(error.exception.outcome, 'unknown')
                self.assertNotIn(fake.info.token, str(error.exception))
                with self.assertRaises(BackendError):
                    await backend.submit(prepared)
                with self.assertRaises(BackendError):
                    await backend.restore(handle)
                self.assertEqual(fake.counts['submit_generation'], 1)
                self.assertTrue(request.save_path.exists())
                # Explicit simulated operator resolution, solely for fixture cleanup.
                backend.inflight = False
                backend.session.resolve_write_barrier()
                await backend.close()

    async def test_T04_total_deadline_and_long_approval(self):
        for elapsed, success in ((301, True), (661, False)):
            with TemporaryDirectory() as directory:
                fake = FakeLauncher()
                fake.submit_elapsed = elapsed
                backend = await fake.backend(Path(directory) / 'journal')
                await backend.apply_profile(PROFILE)
                preparation = await backend.prepare(GenerationRequest(item_id=1, attempt=1, positive='style',
                    negative='', width=64, height=64, save_path=Path(directory) / 'out.png'))
                if success:
                    await backend.submit(preparation)
                else:
                    with self.assertRaises(BackendError):
                        await backend.submit(preparation)
                    backend.inflight = False
                    backend.session.resolve_write_barrier()
                self.assertEqual(fake.virtual_time, elapsed)
                self.assertEqual(fake.counts['submit_generation'], 1)
                await backend.close()
        fake = FakeLauncher()
        observed = []
        real_wait = asyncio.wait_for
        async def virtual_wait(awaitable, timeout):
            observed.append(timeout)
            return await real_wait(awaitable, 1)
        with patch('stylelora.backends.nai_launcher.client.asyncio.wait_for', virtual_wait):
            session = await connect(fake.info, transport=fake)
            await session.call('get_generation_settings', {}, kind='read')
        self.assertTrue(all(t == 660 for t in observed))
        async def deadline(awaitable, timeout):
            awaitable.close()
            raise TimeoutError()
        with patch('stylelora.backends.nai_launcher.client.asyncio.wait_for', deadline):
            with self.assertRaises(BackendError) as caught:
                await session.call('update_generation_settings', {'steps': 14}, kind='write')
        self.assertEqual(caught.exception.outcome, 'unknown')
        self.assertEqual(fake.counts['update_generation_settings'], 0)

    async def test_T05_serial_writes_concurrent_reads_and_unknown_barrier(self):
        fake = FakeLauncher()
        session = await connect(fake.info, transport=fake)
        await asyncio.gather(*(session.call('update_generation_settings', {'steps': n}, kind='write') for n in range(5)))
        self.assertEqual(fake.max_writes, 1)
        await asyncio.gather(*(session.call('get_generation_settings', {}, kind='read') for _ in range(4)))
        self.assertEqual(fake.max_reads, 2)
        fake.update_fault = 'timeout'
        with self.assertRaises(BackendError):
            await session.call('update_generation_settings', {'steps': 20}, kind='write')
        count = fake.counts['update_generation_settings']
        with self.assertRaises(BackendError):
            await session.call('update_generation_settings', {'steps': 20}, kind='write')
        await session.call('get_generation_settings', {}, kind='read')
        await cancel_request(session, 'id')
        with self.assertRaises(BackendError):
            await close_session(session)
        self.assertEqual(count, fake.counts['update_generation_settings'])
        session.resolve_write_barrier()
        await close_session(session)
        await close_session(session)

    async def test_T23_read_retry_bounded_backoff(self):
        fake = FakeLauncher()
        delays = []
        async def sleep(delay):
            delays.append(delay)
        session = await connect(fake.info, transport=fake, sleep=sleep, random_value=lambda: 0)
        fake.tool_fault = 502
        with self.assertRaises(BackendError):
            await session.call('get_generation_settings', {}, kind='read')
        self.assertEqual(delays, [1, 2, 4])
        self.assertEqual(fake.counts['get_generation_settings'], 4)
        self.assertEqual(fake.counts['submit_generation'], 0)
