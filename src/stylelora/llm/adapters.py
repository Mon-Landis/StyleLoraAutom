"""Pluggable wire protocols; urllib is the only HTTP dependency."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import socket
import time
from threading import Event
from typing import Protocol
from urllib import error, request as urllib_request
from urllib.parse import urlsplit

from .types import EncodedImage, EndpointConfig, LlmFailure, LlmReply, LlmRequest, ModelConfig


class TransportFailure(LlmFailure):
    def __init__(self, code, *, status=0, retry_after=None, outcome='unknown'):
        super().__init__(code, status in (408, 429, 502, 503, 504) or code == 'llm:network', outcome)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict
    body: bytes


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    async def __call__(self, method, url, headers, body, timeout) -> HttpResponse:
        canceled, connection = Event(), []
        try:
            return await asyncio.to_thread(self._send, method, url, headers, body, timeout,
                                           canceled, connection)
        finally:
            canceled.set()
            # Best effort interruption of a connected socket; DNS/connect remain bounded
            # by the transport timeout and cannot promise server-side cancellation.
            for sock in connection:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @staticmethod
    def _send(method, url, headers, body, timeout, canceled, connection):
        deadline = time.monotonic() + timeout
        req = urllib_request.Request(url, data=body, headers=headers, method=method)
        try:
            if canceled.is_set():
                raise LlmFailure('llm:canceled', outcome='not_sent')
            # Do not forward credentials through redirects or implicit proxy settings.
            opener = urllib_request.build_opener(urllib_request.ProxyHandler({}), _NoRedirect())
            with opener.open(req, timeout=timeout) as response:
                sock = response.fp.raw._sock
                connection.append(sock)
                chunks, size = [], 0
                while True:
                    if canceled.is_set():
                        raise LlmFailure('llm:canceled', outcome='unknown')
                    if response.isclosed():
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    sock.settimeout(remaining)
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise LlmFailure('llm:response_too_large', outcome='unknown')
                    chunks.append(chunk)
                return HttpResponse(response.status, dict(response.headers), b''.join(chunks))
        except error.HTTPError as exc:
            status, headers = exc.code, dict(exc.headers)
            exc.close()
            return HttpResponse(status, headers, b'')
        except (TimeoutError, socket.timeout):
            raise TransportFailure('llm:timeout') from None
        except error.URLError as exc:
            code = 'llm:timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'llm:network'
            raise TransportFailure(code) from None
        except (OSError, EOFError):
            raise TransportFailure('llm:network') from None


class ProtocolAdapter(Protocol):
    name: str

    async def complete(self, endpoint: EndpointConfig, token: str, request: LlmRequest,
                       image: EncodedImage | None) -> LlmReply: ...

    async def list_models(self, endpoint: EndpointConfig, token: str) -> tuple[ModelConfig, ...]: ...


def endpoint_url(endpoint: EndpointConfig, suffix: str) -> str:
    base = endpoint.base_url.rstrip('/')
    if urlsplit(base).path in ('', '/'):
        base += '/v1'
    return base + '/' + suffix


class JsonAdapter:
    def __init__(self, name: str, transport=None):
        self.name = name
        self.transport = transport or UrllibTransport()

    def _headers(self, token):
        headers = {'Content-Type': 'application/json'}
        if self.name == 'anthropic_messages':
            headers.update({'x-api-key': token, 'anthropic-version': '2023-06-01'})
        else:
            headers['Authorization'] = 'Bearer ' + token
        return headers

    async def _call(self, endpoint, token, suffix, payload=None, timeout=300):
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
        response = await self.transport('POST' if raw is not None else 'GET', endpoint_url(endpoint, suffix),
                                        self._headers(token), raw, timeout)
        if response.status >= 300:
            code = ('llm:authentication' if response.status in (401, 403) else
                    'llm:rate_limited' if response.status == 429 else 'llm:http_error')
            retry_after = next((v for k, v in response.headers.items() if k.lower() == 'retry-after'), None)
            raise TransportFailure(code, status=response.status, retry_after=retry_after,
                                   outcome='rejected' if response.status < 500 else 'unknown')
        try:
            value = json.loads(response.body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise LlmFailure('llm:response_invalid', outcome='unknown') from None

    async def complete(self, endpoint, token, request, image):
        model = next(m for m in endpoint.models if m.name == request.binding.model)
        payload = {'model': model.wire_name or model.name}
        text = json.dumps(dict(request.payload), ensure_ascii=False, allow_nan=False)
        data = base64.b64encode(image.data).decode('ascii') if image else None
        uri = ('data:' + image.mime_type + ';base64,' + data) if image else None
        sampling = dict(request.sampling)
        allowed = {'temperature', 'top_p', 'max_tokens', 'stop'}
        if set(sampling) - allowed:
            raise LlmFailure('llm:sampling_unsupported', outcome='not_sent')
        if self.name == 'openai_chat':
            content = [{'type': 'text', 'text': text}]
            if image:
                content.append({'type': 'image_url', 'image_url': {'url': uri}})
            payload['messages'] = [{'role': 'system', 'content': request.system_text},
                                   {'role': 'user', 'content': content}]
            payload.update(sampling)
            if endpoint.supports_json_schema:
                payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': 'result', 'strict': True, 'schema': dict(request.response_schema)}}
            suffix = 'chat/completions'
        elif self.name == 'openai_responses':
            content = [{'type': 'input_text', 'text': text}]
            if image:
                content.append({'type': 'input_image', 'image_url': uri})
            payload.update(instructions=request.system_text, input=[{'role': 'user', 'content': content}])
            if 'stop' in sampling:
                raise LlmFailure('llm:sampling_unsupported', outcome='not_sent')
            if 'max_tokens' in sampling:
                sampling['max_output_tokens'] = sampling.pop('max_tokens')
            payload.update(sampling)
            if endpoint.supports_json_schema:
                payload['text'] = {'format': {'type': 'json_schema', 'name': 'result',
                                             'strict': True, 'schema': dict(request.response_schema)}}
            suffix = 'responses'
        else:
            content = [{'type': 'text', 'text': text}]
            if image:
                content.append({'type': 'image', 'source': {'type': 'base64',
                                'media_type': image.mime_type, 'data': data}})
            if 'stop' in sampling:
                sampling['stop_sequences'] = sampling.pop('stop')
            payload.update(system=request.system_text, messages=[{'role': 'user', 'content': content}],
                           max_tokens=4096)
            payload.update(sampling)
            if endpoint.supports_json_schema:
                payload['output_config'] = {'format': {'type': 'json_schema',
                                                      'schema': dict(request.response_schema)}}
            suffix = 'messages'
        value = await self._call(endpoint, token, suffix, payload, request.timeout_s)
        try:
            actual = value['model']
            if actual not in (model.name, model.wire_name, *model.response_aliases):
                raise LlmFailure('llm:model_mismatch')
            if self.name == 'openai_chat':
                choice = value['choices'][0]
                if choice.get('finish_reason') != 'stop':
                    raise ValueError
                text = choice['message']['content']
            elif self.name == 'openai_responses':
                if value.get('status') != 'completed':
                    raise ValueError
                text = ''.join(part['text'] for item in value['output'] if item.get('type') == 'message'
                               for part in item['content'] if part.get('type') == 'output_text')
            else:
                if value.get('stop_reason') not in ('end_turn', 'stop_sequence'):
                    raise ValueError
                text = ''.join(part['text'] for part in value['content'] if part.get('type') == 'text')
            if not isinstance(text, str) or not text.strip():
                raise ValueError
            # Retain numeric usage only, never arbitrary provider metadata.
            usage = {k: v for k, v in (value.get('usage') or {}).items()
                     if k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens')
                     and type(v) is int and v >= 0}
            return LlmReply(text, model.name, request.request_id, usage or None,
                            image.tier if image else None)
        except (KeyError, TypeError, ValueError, IndexError):
            raise LlmFailure('llm:response_contract', outcome='unknown') from None

    async def list_models(self, endpoint, token):
        value = await self._call(endpoint, token, 'models')
        try:
            result = []
            for row in value['data']:
                name = row['id']
                if not isinstance(name, str):
                    raise ValueError
                modalities = row.get('input_modalities', row.get('architecture', {}).get('input_modalities'))
                previous = next((m for m in endpoint.models if (m.wire_name or m.name) == name), ModelConfig(name))
                if isinstance(modalities, list) and all(isinstance(m, str) for m in modalities):
                    from dataclasses import replace
                    previous = replace(previous, supports_images='image' in modalities,
                                       capability_source='models', tested_at=None)
                result.append(previous)
            return tuple(result)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise LlmFailure('llm:models_invalid') from None


_ADAPTERS: dict[str, ProtocolAdapter] = {}


def register_adapter(adapter: ProtocolAdapter) -> None:
    _ADAPTERS[adapter.name] = adapter


def adapter_for(protocol: str) -> ProtocolAdapter:
    try:
        return _ADAPTERS[protocol]
    except KeyError:
        raise LlmFailure('llm:protocol_unsupported', outcome='not_sent') from None


for _name in ('openai_chat', 'openai_responses', 'anthropic_messages'):
    register_adapter(JsonAdapter(_name))
