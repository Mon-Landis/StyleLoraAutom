"""Streamable HTTP MCP, with injectable transport and no write retries."""
import asyncio
import codecs
import http.client
import json
import random
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import uuid4
from ..types import BackendError, Immutable, canonical
from .compatibility import READ_TOOLS, WRITE_TOOLS, PROTOCOLS, ServerInfo, ToolSchema

BODY_LIMIT = 8 * 1024 * 1024


@dataclass
class HttpReply:
    status: int
    headers: dict
    body: object = b''  # bytes or async byte iterator


class HttpTransport:
    """Standard library transport: redirects are never followed."""
    async def request(self, method, endpoint, headers, body, timeout):
        url = urlsplit(endpoint)
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=min(10, timeout))
        def begin():
            connection.connect()
            connection.sock.settimeout(timeout)
            connection.request(method, url.path or '/', body, headers)
            return connection.getresponse()
        try:
            response = await asyncio.to_thread(begin)
        except BaseException:
            connection.close()
            raise
        async def chunks():
            try:
                while chunk := await asyncio.to_thread(response.read1, 65536):
                    yield chunk
            finally:
                connection.close()
        return HttpReply(response.status, dict(response.getheaders()), chunks())


@dataclass(frozen=True)
class ToolReply(Immutable):
    payload: dict
    is_error: bool = False


def encode_request(payload, limit=BODY_LIMIT):
    body = canonical(payload)
    if len(body) > limit:
        raise BackendError('mcp:body_too_large', 'transport', retry='fatal')
    return body


async def byte_chunks(body):
    if isinstance(body, bytes):
        yield body
    else:
        async for chunk in body:
            yield chunk


async def response_messages(reply):
    headers = {k.lower(): v for k, v in reply.headers.items()}
    if 'text/event-stream' not in headers.get('content-type', ''):
        data = b''.join([chunk async for chunk in byte_chunks(reply.body)])
        yield json.loads(data)
        return
    decoder, buffer, data = codecs.getincrementaldecoder('utf-8')(), '', []
    async for chunk in byte_chunks(reply.body):
        buffer += decoder.decode(chunk)
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.removesuffix('\r')
            if not line:
                if data:
                    yield json.loads('\n'.join(data))
                    data = []
            elif line.startswith('data:'):
                data.append(line[5:].removeprefix(' '))
    buffer += decoder.decode(b'', final=True)
    # Unterminated event is not a final JSON-RPC response.


class HttpFailure(Exception):
    def __init__(self, status, retry_after=None):
        self.status = status
        self.retry_after = retry_after


@dataclass(eq=False)
class McpSession:
    info: object = field(repr=False)
    transport: object = field(repr=False)
    session_id: str = ''
    protocol_version: str = ''
    generation: int = 0
    tool_timeout_s: float = 660
    read_retry_limit: int = 3
    sleep: object = asyncio.sleep
    random_value: object = random.random
    rediscover: object = field(default=None, repr=False)

    def __post_init__(self):
        if self.tool_timeout_s < 600:
            raise ValueError('Tool timeout must be at least 600 seconds')
        self.write_lock = asyncio.Lock()
        self.read_slots = asyncio.Semaphore(2)
        self.handshake_lock = asyncio.Lock()
        self.unknown_write = False
        self.server = None
        self.tools = ()

    async def _rpc(self, method, params=None, *, notification=False, initializing=False, verb='POST'):
        identity = uuid4().hex
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        if not notification:
            payload['id'] = identity
        body = encode_request(payload) if verb == 'POST' else b''
        headers = {'Authorization': 'Bearer ' + self.info.token,
                   'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if self.session_id and not initializing:
            headers['Mcp-Session-Id'] = self.session_id
            headers['MCP-Protocol-Version'] = self.protocol_version
        async def exchange():
            reply = await self.transport.request(verb, self.info.endpoint, headers, body, self.tool_timeout_s)
            try:
                if verb == 'DELETE' and reply.status == 404:
                    return None
                if not 200 <= reply.status < 300:
                    raise HttpFailure(reply.status, next((v for k, v in reply.headers.items() if k.lower() == 'retry-after'), None))
                if notification:
                    if reply.status not in (200, 202, 204):
                        raise ValueError()
                    return None
                if verb == 'DELETE':
                    return None
                async for message in response_messages(reply):
                    if not isinstance(message, dict) or message.get('id') != identity:
                        continue
                    if message.get('jsonrpc') != '2.0':
                        raise ValueError()
                    if 'error' in message:
                        raise BackendError('mcp:rpc_error', 'transport', 'unknown')
                    if not isinstance(message.get('result'), dict):
                        raise ValueError()
                    if initializing:
                        self.session_id = next((v for k, v in reply.headers.items() if k.lower() == 'mcp-session-id'), '')
                        if not self.session_id:
                            raise ValueError()
                    return message['result']
                raise ValueError()
            finally:
                if hasattr(reply.body, 'aclose'):
                    await reply.body.aclose()
        return await asyncio.wait_for(exchange(), timeout=self.tool_timeout_s)

    async def initialize(self, expected_generation=None):
        async with self.handshake_lock:
            if expected_generation is not None and self.generation != expected_generation:
                return
            if self.rediscover is not None:
                self.info = self.rediscover()
            supported = [p for p in PROTOCOLS if p in self.info.protocol_versions]
            if not supported:
                raise BackendError('backend:unsupported_protocol', 'initialize')
            try:
                result = await self._rpc('initialize', {'protocolVersion': supported[0], 'capabilities': {},
                    'clientInfo': {'name': 'StyleLoraAutom', 'version': '1'}}, initializing=True)
                self.protocol_version = result['protocolVersion']
                if self.protocol_version not in supported:
                    raise BackendError('backend:unsupported_protocol', 'initialize')
                self.server = ServerInfo(**result['serverInfo'])
                await self._rpc('notifications/initialized', notification=True)
                tools, cursors, cursor = [], set(), None
                while True:
                    page = await self._rpc('tools/list', {} if cursor is None else {'cursor': cursor})
                    tools.extend(ToolSchema(t['name'], t['inputSchema'], t.get('outputSchema')) for t in page['tools'])
                    cursor = page.get('nextCursor')
                    if cursor is None:
                        break
                    if cursor in cursors:
                        raise ValueError()
                    cursors.add(cursor)
                self.tools = tuple(tools)
                self.generation += 1
            except BackendError:
                raise
            except Exception:
                raise BackendError('mcp:disconnected', 'initialize') from None

    async def call(self, name, arguments, *, kind):
        if (kind == 'read' and name not in READ_TOOLS) or (kind == 'write' and name not in WRITE_TOOLS) or kind not in ('read', 'write'):
            raise BackendError('backend:incompatible_schema', 'transport')
        # Check serialized size before claiming that a request may have been sent.
        encode_request({'jsonrpc': '2.0', 'id': '0' * 32, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
        async with self.write_lock if kind == 'write' else self.read_slots:
            if kind == 'write' and self.unknown_write:
                raise BackendError('mcp:write_barrier', name)
            for attempt in range(self.read_retry_limit + 1):
                generation = self.generation
                try:
                    result = await self._rpc('tools/call', {'name': name, 'arguments': arguments})
                    payload = result.get('structuredContent')
                    if payload is None:
                        text = [c['text'] for c in result.get('content', ()) if c.get('type') == 'text']
                        if len(text) != 1:
                            raise ValueError()
                        payload = json.loads(text[0])
                    if not isinstance(payload, dict):
                        raise ValueError()
                    if result.get('isError') or payload.get('ok') is False:
                        # No unaudited approval code is interpreted as proof of no generation.
                        code = 'artifact:save_path_exists' if payload.get('code') == 'save_path_exists' else 'generation:rejected'
                        if name == 'submit_generation':
                            self.unknown_write = True
                            raise BackendError('mcp:write_outcome_unknown', name, 'unknown')
                        raise BackendError(code, name, 'rejected')
                    return ToolReply(payload)
                except BackendError as exc:
                    if exc.outcome == 'unknown' and kind == 'write':
                        self.unknown_write = True
                    raise
                except BaseException as exc:
                    if kind == 'write':
                        self.unknown_write = True
                        raise BackendError('mcp:write_outcome_unknown', name, 'unknown') from None
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    if isinstance(exc, HttpFailure) and exc.status == 401:
                        raise BackendError('mcp:unauthorized', name) from None
                    if attempt == self.read_retry_limit:
                        raise BackendError('mcp:read_timeout', name, retry='read_only') from None
                    if isinstance(exc, HttpFailure) and exc.status == 404:
                        await self.initialize(generation)
                    else:
                        delay = min(30, 2 ** attempt * (1 + .25 * self.random_value()))
                        if isinstance(exc, HttpFailure) and exc.status == 429 and exc.retry_after:
                            try:
                                delay = max(delay, min(30, max(0, float(exc.retry_after))))
                            except ValueError:
                                pass
                        await self.sleep(delay)

    def resolve_write_barrier(self):
        """Only the adapter may call after exact side-effect evidence is verified."""
        self.unknown_write = False


async def connect(info, *, transport=None, **options):
    session = McpSession(info, transport if transport is not None else HttpTransport(), **options)
    await session.initialize()
    return session


async def call_tool(session, name, arguments, *, kind):
    return await session.call(name, arguments, kind=kind)


async def cancel_request(session, request_id, reason='user_requested'):
    try:
        await session._rpc('notifications/cancelled', {'requestId': request_id, 'reason': 'user_requested'}, notification=True)
    except Exception:
        raise BackendError('mcp:disconnected', 'cancel') from None


async def close_session(session):
    if session.unknown_write or session.write_lock.locked():
        raise BackendError('profile:restore_pending', 'close')
    try:
        await session._rpc('', verb='DELETE')
        session.session_id = ''
    except Exception:
        raise BackendError('mcp:disconnected', 'close') from None
