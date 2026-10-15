import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from stylelora.llm import (CancelToken, ConfigStore, DEFAULT_TIERS, EndpointConfig, HttpResponse,
                           ImageInput, LlmFailure, LlmRequest, LlmService, ModelConfig,
                           RepairBudget, SecretStore, TransportFailure, capability_fixture)


SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}


def response(text='{"answer":"ok"}', model='model'):
    return HttpResponse(200, {}, json.dumps({'model': model, 'choices': [
        {'finish_reason': 'stop', 'message': {'content': text}}],
        'usage': {'prompt_tokens': 12, 'unsafe': 'token-primary'}}).encode())


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'STYLELORA_LLM_TOKEN_PRIMARY': 'token-primary',
                                          'STYLELORA_LLM_TOKEN_SECONDARY': 'token-secondary'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = ConfigStore(self.root / 'llm.json')
        self.endpoint = self.config.save_endpoint(EndpointConfig(
            'local', 'http://127.0.0.1:9999/v1', 'openai_chat', ('PRIMARY', 'SECONDARY'),
            (ModelConfig('model', True),)), expected_revision=0)
        self.config.bind_role('prompt', 'local', 'model')
        self.calls = []

    async def make(self, transport=None, **kwargs):
        async def default(*args):
            self.calls.append(args)
            return response()
        service = LlmService(self.config, SecretStore(self.root / 'secrets.json'),
                             transport=transport or default, backoff_s=0.001, **kwargs)
        binding = await service.resolve_role('prompt')
        req = LlmRequest('request', binding, 'system', {'input': 'text'}, SCHEMA)
        return service, req

    async def test_minimum_dto_and_rotation_and_usage(self):
        service, req = await self.make()
        first = await service.complete(req)
        await service.complete(replace(req, request_id='second'))
        self.assertEqual(first.actual_model, 'model')
        self.assertEqual(first.usage, {'prompt_tokens': 12})
        self.assertEqual([call[2]['Authorization'] for call in self.calls],
                         ['Bearer token-primary', 'Bearer token-secondary'])
        self.assertEqual(self.calls[0][-1], 300)
        audit = (self.root / 'llm.audit.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('token-primary', audit)
        self.assertNotIn('system', audit)
        self.assertNotIn('127.0.0.1', audit)

    async def test_429_rotates_without_compression(self):
        async def transport(*args):
            self.calls.append(args)
            return HttpResponse(429, {'Retry-After': '0.01'}, b'secret error') if len(self.calls) == 1 else response()
        service, req = await self.make(transport)
        reply = await service.complete(replace(req, image_input=ImageInput(data=capability_fixture()),
                                               timeout_policy='vision_downgrade'))
        self.assertEqual(reply.attempts, 2)
        self.assertEqual(reply.actual_tier, DEFAULT_TIERS[0])
        self.assertNotEqual(self.calls[0][2], self.calls[1][2])
        self.assertEqual(self.calls[0][3], self.calls[1][3])

    async def test_all_tokens_cooldown_and_budget(self):
        async def transport(*args):
            self.calls.append(args)
            return HttpResponse(429, {'Retry-After': '0.01'}, b'')
        service, req = await self.make(transport)
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(req)
        self.assertEqual(caught.exception.code, 'llm:rate_limited')
        self.assertEqual(len(self.calls), 3)

    async def test_retry_after_respects_deadline(self):
        async def transport(*args):
            self.calls.append(args)
            return HttpResponse(429, {'Retry-After': '99999'}, b'')
        service, req = await self.make(transport)
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(replace(req, timeout_s=0.1))
        self.assertEqual(caught.exception.code, 'llm:retry_deadline')
        self.assertEqual(len(self.calls), 2)

    async def test_auth_and_bad_request_not_retried(self):
        for status in (400, 401, 403):
            self.calls.clear()
            async def transport(*args):
                self.calls.append(args)
                return HttpResponse(status, {}, b'token-primary')
            service, req = await self.make(transport)
            with self.assertRaises(LlmFailure) as caught:
                await service.complete(req)
            self.assertNotIn('token-primary', str(caught.exception))
            self.assertEqual(len(self.calls), 1)

    async def test_text_timeout_unknown_no_retry(self):
        async def transport(*args):
            self.calls.append(args)
            raise TransportFailure('llm:timeout')
        service, req = await self.make(transport)
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(req)
        self.assertEqual(caught.exception.outcome, 'unknown')
        self.assertEqual(len(self.calls), 1)

    async def test_vision_timeout_two_downgrades(self):
        async def transport(*args):
            self.calls.append(args)
            if len(self.calls) < 3:
                raise TransportFailure('llm:timeout')
            return response()
        service, req = await self.make(transport)
        reply = await service.complete(replace(req, image_input=ImageInput(data=capability_fixture()),
                                               timeout_policy='vision_downgrade'))
        self.assertEqual(reply.actual_tier, DEFAULT_TIERS[2])
        self.assertEqual(reply.attempts, 3)
        self.assertEqual(len({c[2]['Authorization'] for c in self.calls}), 1)

    async def test_503_retry_keeps_image(self):
        async def transport(*args):
            self.calls.append(args)
            return HttpResponse(503, {}, b'') if len(self.calls) < 3 else response()
        service, req = await self.make(transport)
        reply = await service.complete(replace(req, image_input=ImageInput(data=capability_fixture()),
                                               timeout_policy='vision_downgrade'))
        self.assertEqual(reply.actual_tier, DEFAULT_TIERS[0])
        self.assertEqual(len({c[3] for c in self.calls}), 1)

    async def test_json_local_repair_and_one_reask(self):
        async def local(*args):
            self.calls.append(args)
            return response('```json\n{"answer":"ok"}\n```')
        service, req = await self.make(local)
        result = await service.complete_json(req)
        self.assertTrue(result.repair_used)
        self.assertEqual(result.value['answer'], 'ok')
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        async def remote(*args):
            self.calls.append(args)
            return response('untrusted malicious reply') if len(self.calls) == 1 else response()
        service, req = await self.make(remote)
        result = await service.complete_json(req)
        self.assertEqual(result.reply.attempts, 2)
        self.assertNotIn(b'untrusted malicious reply', self.calls[1][3])

    async def test_shared_repair_budget_and_semantic_error(self):
        async def bad(*args):
            self.calls.append(args)
            return response('invalid')
        service, req = await self.make(bad)
        budget = RepairBudget()
        with self.assertRaises(LlmFailure):
            await service.complete_json(replace(req, repair_budget=budget))
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(LlmFailure):
            await service.complete_json(replace(req, repair_budget=budget))
        self.assertEqual(len(self.calls), 3)
        def reject(value, schema):
            raise LlmFailure('llm:invalid_input')
        service, req = await self.make(validator=reject)
        self.calls.clear()
        with self.assertRaises(LlmFailure) as caught:
            await service.complete_json(req)
        self.assertEqual(caught.exception.code, 'llm:invalid_input')
        self.assertEqual(len(self.calls), 1)

    async def test_global_concurrency_duplicate_and_cancel(self):
        active = peak = 0
        started = asyncio.Event()
        release = asyncio.Event()
        async def transport(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.set()
            try:
                await release.wait()
                return response()
            finally:
                active -= 1
        service, req = await self.make(transport)
        first = asyncio.create_task(service.complete(req))
        await started.wait()
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(req)
        self.assertEqual(caught.exception.code, 'llm:duplicate_request')
        others = [asyncio.create_task(service.complete(replace(req, request_id=str(i)))) for i in range(4)]
        await asyncio.sleep(0.03)
        await service.cancel('3')
        await service.cancel('request')
        await asyncio.sleep(0.06)
        release.set()
        results = await asyncio.gather(first, *others, return_exceptions=True)
        self.assertEqual(peak, 2)
        self.assertEqual(results[0].outcome, 'unknown')
        self.assertEqual(results[-1].outcome, 'not_sent')
        self.assertEqual(service.network.active, 0)

    async def test_precanceled_no_dispatch(self):
        service, req = await self.make()
        cancel = CancelToken()
        cancel.cancel()
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(replace(req, cancel=cancel))
        self.assertEqual(caught.exception.outcome, 'not_sent')
        self.assertFalse(self.calls)

    async def test_frozen_endpoint_and_model_mismatch(self):
        service, req = await self.make()
        self.config.save_endpoint(replace(self.endpoint, base_url='http://localhost:1111'), expected_revision=1)
        await service.complete(req)
        self.assertIn('9999', self.calls[0][1])
        async def mismatch(*args):
            return response(model='unexpected')
        service, req = await self.make(mismatch)
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(req)
        self.assertEqual(caught.exception.code, 'llm:model_mismatch')

    async def test_reply_redacts_all_endpoint_tokens(self):
        async def transport(*args):
            return response('token-primary token-secondary')
        service, req = await self.make(transport)
        self.assertEqual((await service.complete(req)).text, '******** ********')

    async def test_models_and_semantic_capability_test(self):
        async def transport(method, *args):
            if method == 'GET':
                return HttpResponse(200, {}, b'{"data":[{"id":"model","input_modalities":["text","image"]}]}')
            return response('red')
        service, _ = await self.make(transport)
        models = await service.probe_models('local')
        self.assertEqual(models[0].capability_source, 'models')
        tested = await service.test_image_capability('local', 'model', fixture=capability_fixture(), cancel=CancelToken())
        self.assertEqual(tested.capability_source, 'tested')
        self.assertIsNotNone(tested.tested_at)
        async def wrong(*args):
            return response('blue')
        service, _ = await self.make(wrong)
        with self.assertRaises(LlmFailure):
            await service.test_image_capability('local', 'model')
        self.assertEqual(self.config.endpoint('local').models[0], tested)

    async def test_models_unavailable_preserves_manual(self):
        async def transport(*args):
            return HttpResponse(404, {}, b'')
        service, _ = await self.make(transport)
        self.assertEqual(await service.probe_models('local'), self.endpoint.models)

    async def test_models_name_alone_does_not_infer_vision(self):
        async def transport(*args):
            return HttpResponse(200, {}, b'{"data":[{"id":"model"},{"id":"vision-name-only"}]}')
        service, _ = await self.make(transport)
        models = await service.probe_models('local')
        self.assertEqual(models[0], self.endpoint.models[0])
        self.assertFalse(models[1].supports_images)
        self.assertEqual(models[1].capability_source, 'manual')

    async def test_repair_preserves_downgraded_tier(self):
        async def transport(*args):
            self.calls.append(args)
            if len(self.calls) == 1:
                raise TransportFailure('llm:timeout')
            return response('invalid') if len(self.calls) == 2 else response()
        service, req = await self.make(transport)
        result = await service.complete_json(replace(req, image_input=ImageInput(data=capability_fixture()),
                                                    timeout_policy='vision_downgrade'))
        self.assertEqual(result.reply.actual_tier, DEFAULT_TIERS[1])
        self.assertEqual(result.reply.attempts, 3)
        first = json.loads(self.calls[1][3])['messages'][1]['content'][1]
        repaired = json.loads(self.calls[2][3])['messages'][1]['content'][1]
        self.assertEqual(first, repaired)

    async def test_actual_timeout_and_queue_deadline(self):
        async def transport(*args):
            await asyncio.sleep(0.04)
            return response()
        service, req = await self.make(transport, concurrency=1)
        replies = await asyncio.gather(*(service.complete(replace(req, request_id=str(i), timeout_s=1))
                                         for i in range(3)))
        self.assertEqual(len(replies), 3)
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(replace(req, timeout_s=0.01))
        self.assertEqual(caught.exception.code, 'llm:timeout')
        lease = await service.network.acquire(req.binding.binding_id, ('held',), cancel=CancelToken(), deadline=float('inf'))
        try:
            with self.assertRaises(LlmFailure) as queued:
                await service.complete(replace(req, timeout_s=.01, transport_retry_limit=0))
            self.assertEqual(queued.exception.code, 'resource:timeout')
            self.assertEqual(queued.exception.outcome, 'not_sent')
        finally:
            await lease.release()

    async def test_plugin_registration(self):
        from stylelora.llm import register_adapter, adapter_for, LlmReply
        class Plugin:
            name = 'test_plugin'

            async def complete(self, endpoint, token, request, image):
                return LlmReply('plugin-result', request.binding.model, request.request_id)

            async def list_models(self, endpoint, token):
                return endpoint.models
        plugin = Plugin()
        register_adapter(plugin)
        self.assertIs(adapter_for('test_plugin'), plugin)
        endpoint = replace(self.endpoint, protocol='test_plugin')
        self.config.save_endpoint(endpoint, expected_revision=1)
        service, req = await self.make()
        self.assertEqual((await service.complete(req)).text, 'plugin-result')

    async def test_network_retry_and_truncated_response_rejected(self):
        async def transport(*args):
            self.calls.append(args)
            if len(self.calls) == 1:
                raise TransportFailure('llm:network')
            return response()
        service, req = await self.make(transport)
        self.assertEqual((await service.complete(req)).attempts, 2)
        async def truncated(*args):
            value = json.loads(response().body)
            value['choices'][0]['finish_reason'] = 'length'
            return HttpResponse(200, {}, json.dumps(value).encode())
        service, req = await self.make(truncated)
        with self.assertRaises(LlmFailure) as caught:
            await service.complete(req)
        self.assertEqual(caught.exception.code, 'llm:response_contract')


class HttpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_protocols_against_local_server(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, dict(self.headers), body))
                if self.path == '/v1/chat/completions':
                    raw = response().body
                elif self.path == '/v1/responses':
                    raw = json.dumps({'model': 'model', 'status': 'completed', 'output': [
                        {'type': 'message', 'content': [{'type': 'output_text', 'text': '{"answer":"ok"}'}]}]}).encode()
                else:
                    raw = json.dumps({'model': 'model', 'stop_reason': 'end_turn',
                                      'content': [{'type': 'text', 'text': '{"answer":"ok"}'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'STYLELORA_LLM_TOKEN_HTTP': 'local-test-key'}):
                root = Path(directory)
                config = ConfigStore(root / 'llm.json')
                service = LlmService(config, SecretStore(root / 'secrets.json'))
                for protocol in ('openai_chat', 'openai_responses', 'anthropic_messages'):
                    config.save_endpoint(EndpointConfig(protocol, f'http://127.0.0.1:{server.server_port}/v1',
                        protocol, ('HTTP',), (ModelConfig('model', True),)), expected_revision=0)
                    config.bind_role('vision', protocol, 'model')
                    binding = await service.resolve_role('vision')
                    reply = await service.complete_json(LlmRequest(protocol, binding, 'system', {'text': 'hello'}, SCHEMA,
                        image_input=ImageInput(data=capability_fixture()), sampling={'max_tokens': 100}))
                    self.assertEqual(reply.value['answer'], 'ok')
                self.assertEqual([c[0] for c in calls], ['/v1/chat/completions', '/v1/responses', '/v1/messages'])
                self.assertTrue(calls[0][2]['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
                self.assertEqual(calls[1][2]['input'][0]['content'][1]['type'], 'input_image')
                self.assertEqual(calls[1][2]['max_output_tokens'], 100)
                self.assertEqual(calls[2][2]['messages'][0]['content'][1]['source']['type'], 'base64')
                self.assertEqual(calls[2][1]['X-Api-Key'], 'local-test-key')
                self.assertNotIn('response_format', calls[0][2])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
