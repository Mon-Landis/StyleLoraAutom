"""Synthetic protocol fixture, NOT an audited Launcher v4.3.0 schema."""
import asyncio
from collections import Counter
from dataclasses import replace
import io
import json
from pathlib import Path
from PIL import Image, PngImagePlugin
from stylelora.backends.types import *
from stylelora.backends.nai_launcher.discovery import DiscoveryInfo
from stylelora.backends.nai_launcher.client import HttpReply, connect
from stylelora.backends.nai_launcher.compatibility import CORE_TOOLS, ToolSchema, schema_hash
from stylelora.backends.nai_launcher.adapter import LauncherBackend
from stylelora.generation.profile import profile_values, SMALL

PROFILE = GenerationProfile(backend='nai_launcher', effort='high', sampler='test_sampler', steps=14,
    scale=5.0, cfg_rescale=0.0, noise_schedule='test_schedule', smea=False, smea_dyn=False,
    variety_plus=False, decrisp=False)
VALUES = profile_values(PROFILE)
SPECS = tuple(ParameterSpec(k, {'type': 'null' if v is None else 'boolean' if type(v) is bool
    else 'integer' if type(v) is int else 'number' if type(v) is float else 'string'}) for k, v in VALUES.items())
CAPS = BackendCapabilities('nai_launcher', 1, (PROFILE.model,), SPECS,
                           ('width', 'height', 'seed'), True, True, True)
TOOLS = tuple(ToolSchema(name, {'type': 'object', 'properties': {'width': {'type': 'integer', 'maximum': 2048},
    'height': {'type': 'integer', 'maximum': 2048}, 'seed': {'type': 'integer'}}}) for name in sorted(CORE_TOOLS))
MANIFEST = frozenset({('fake-1', '2025-06-18', schema_hash(TOOLS))})


def png_bytes(prompt='style, content', size=(640, 640), comment=True):
    info = PngImagePlugin.PngInfo()
    if comment:
        info.add_text('Comment', json.dumps({'prompt': prompt, 'seed': 42, 'steps': 14, 'scale': 5}))
    out = io.BytesIO()
    Image.new('RGB', size, 'white').save(out, format='PNG', pnginfo=info)
    return out.getvalue()


class FakeLauncher:
    def __init__(self, *, estimated_anlas=0, save_path_exists=False, submit_fault=None,
                 sse=False, prepare_drift=None):
        self.settings = {**VALUES, 'steps': 20, 'prompt': 'page prompt', 'characters': ['page role']}
        self.estimated_anlas = estimated_anlas
        self.save_path_exists, self.submit_fault, self.sse = save_path_exists, submit_fault, sse
        self.prepare_drift = prepare_drift or {}
        self.calls, self.headers, self.counts = [], [], Counter()
        self.prepared = {}
        self.session_number = 0
        self.expired = set()
        self.active_writes = self.max_writes = self.active_reads = self.max_reads = 0
        self.submit_entered, self.submit_release = asyncio.Event(), None
        self.update_fault = None
        self.tool_fault = None
        self.comment_prompt = None
        self.submit_elapsed = 0
        self.virtual_time = 0

    @property
    def info(self):
        return DiscoveryInfo('http://127.0.0.1:12345/mcp', 12345, 'DO-NOT-LOG-SECRET', 99,
                             ('2025-06-18',), 'fake-1')

    async def request(self, verb, endpoint, headers, body, timeout):
        self.headers.append(dict(headers))
        if verb == 'DELETE':
            self.counts['DELETE'] += 1
            return HttpReply(404, {})
        request = json.loads(body)
        method, identity = request['method'], request.get('id')
        self.calls.append(request)
        self.counts[method] += 1
        if method == 'initialize':
            self.session_number += 1
            result = {'protocolVersion': '2025-06-18', 'serverInfo': {'name': 'FakeLauncher', 'version': 'fake-1'}}
        elif method.startswith('notifications/'):
            return HttpReply(202, {}, b'')
        elif method == 'tools/list':
            start = 4 if request.get('params', {}).get('cursor') else 0
            result = {'tools': [{'name': t.name, 'inputSchema': plain(t.input_schema)} for t in TOOLS[start:start + 4]]}
            if start == 0:
                result['nextCursor'] = 'second'
        else:
            name = request['params']['name']
            args = request['params']['arguments']
            self.counts[name] += 1
            if headers.get('Mcp-Session-Id') in self.expired:
                return HttpReply(404, {})
            if self.tool_fault is not None:
                return HttpReply(self.tool_fault, {})
            write = name not in {'get_generation_settings', 'get_generation_status', 'get_recent_images'}
            if write:
                self.active_writes += 1
                self.max_writes = max(self.max_writes, self.active_writes)
            else:
                self.active_reads += 1
                self.max_reads = max(self.max_reads, self.active_reads)
            try:
                await asyncio.sleep(0)
                if name == 'get_generation_settings':
                    payload = {'settings': dict(self.settings)}
                elif name == 'update_generation_settings':
                    self.settings.update(args)
                    if self.update_fault:
                        self.update_fault = None
                        raise TimeoutError('DO-NOT-LOG-SECRET')
                    payload = {'ok': True}
                elif name == 'prepare_generation':
                    if self.save_path_exists or Path(args['save_path']).exists():
                        payload = {'ok': False, 'code': 'save_path_exists'}
                    else:
                        pid = str(self.counts[name])
                        self.prepared[pid] = args
                        effective = {**self.settings, **args, **self.prepare_drift}
                        estimate = self.estimated_anlas
                        if isinstance(estimate, list):
                            estimate = estimate[min(self.counts[name] - 1, len(estimate) - 1)]
                        payload = {'preparation_id': pid, 'estimated_anlas': estimate, 'effective': effective}
                elif name == 'cancel_generation_preparation':
                    self.prepared.pop(args['preparation_id'], None)
                    payload = {'ok': True}
                elif name == 'submit_generation':
                    prepared = self.prepared.pop(args['preparation_id'])
                    self.submit_entered.set()
                    self.virtual_time += self.submit_elapsed
                    if self.submit_elapsed > timeout:
                        raise TimeoutError()
                    if self.submit_release is not None:
                        await self.submit_release.wait()
                    path = Path(prepared['save_path'])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(png_bytes(self.comment_prompt or prepared['prompt'], (prepared['width'], prepared['height'])))
                    if self.submit_fault == 'timeout':
                        raise TimeoutError('DO-NOT-LOG-SECRET')
                    if self.submit_fault in (404, 502):
                        return HttpReply(self.submit_fault, {})
                    if self.submit_fault == 'eof':
                        return HttpReply(200, {'Content-Type': 'text/event-stream'}, b': heartbeat\n\n')
                    payload = {'saved_path': str(path), 'resource_ref': {'resourceId': 'fake-resource'}, 'metadata_stripped': False}
                elif name == 'get_generation_status':
                    payload = {'status': 'idle'}
                elif name == 'get_recent_images':
                    payload = {'images': []}
                else:
                    payload = {'ok': True}
                result = {'structuredContent': payload}
            finally:
                if write:
                    self.active_writes -= 1
                else:
                    self.active_reads -= 1
        response = {'jsonrpc': '2.0', 'id': identity, 'result': result}
        output_headers = {'Content-Type': 'application/json', 'Mcp-Session-Id': f'session-{self.session_number}'}
        encoded = json.dumps(response, ensure_ascii=False).encode()
        if self.sse:
            output_headers['Content-Type'] = 'text/event-stream'
            text = ': ping\r\n\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\n'
            # Legal multiline data and UTF-8 split at arbitrary byte offsets.
            text += 'data: ' + encoded.decode().replace(', "result":', ',\r\ndata: "result":', 1) + '\r\n\r\n'
            raw = text.encode()
            async def chunks():
                for start in range(0, len(raw), 7):
                    yield raw[start:start + 7]
            return HttpReply(200, output_headers, chunks())
        return HttpReply(200, output_headers, encoded)

    async def backend(self, journal_dir):
        session = await connect(self.info, transport=self, sleep=no_sleep, random_value=lambda: 0)
        return LauncherBackend(session, CAPS, journal_dir=journal_dir, manifest=MANIFEST)


async def no_sleep(delay):
    await asyncio.sleep(0)


class LocalBackend:
    """Stateless backend proves runner has no dependency on Launcher tool names."""
    def __init__(self, fault=None):
        self.profile = replace(PROFILE, backend='local')
        self.caps = replace(CAPS, backend='local', session_settings=False, supports_cost=False)
        self.requests = {}
        self.submits = 0
        self.fault = fault
        self.entered, self.release = asyncio.Event(), None

    async def capabilities(self):
        return self.caps

    async def account_status(self):
        return None

    async def apply_profile(self, profile):
        return ProfileHandle('', {}, {}, (), 'local')

    async def restore(self, handle):
        pass

    async def prepare(self, request):
        self.requests[str(request.item_id)] = request
        return Preparation(str(request.item_id), digest(request), None, {})

    async def submit(self, preparation):
        self.submits += 1
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.fault:
            raise self.fault
        request = self.requests[preparation.handle]
        return GenerationResult(image_bytes=png_bytes(request.positive, (request.width, request.height)))

    async def cancel_preparation(self, preparation):
        pass

    async def reconcile(self, item, receipt):
        from stylelora.project_db.types import RecoveryEvidence
        return RecoveryEvidence(item.item_id, item.attempts, 'unknown')

    async def close(self):
        pass
