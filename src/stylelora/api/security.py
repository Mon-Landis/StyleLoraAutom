"""Authentication precedes body parsing, project lookup and conditional media."""
import asyncio
import hashlib
import hmac
import json
from uuid import UUID, uuid4
from starlette.responses import Response
from stylelora.runtime.contracts import Failure, canonical
from .errors import response


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def invalid(_):
        raise ValueError
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError):
        raise Failure(400, 'request:malformed_json') from None


async def protect(request, call_next):
    rt = request.app.state.runtime
    try:
        request_id = str(UUID(request.headers.get('x-request-id', '')))
    except ValueError:
        request_id = str(uuid4())
    request.state.request_id = request_id
    try:
        hosts = request.headers.getlist('host')
        if hosts != [rt.authority]:
            raise Failure(403, 'security:host_denied')
        origin = request.headers.get('origin')
        origins = {'http://' + rt.authority}
        if rt.dev_origin:
            origins.add(rt.dev_origin)
        if (origin is not None and origin not in origins) or request.headers.get('sec-fetch-site') == 'cross-site':
            raise Failure(403, 'security:origin_denied')
        if request.method == 'OPTIONS':
            if origin not in origins:
                raise Failure(403, 'security:origin_denied')
            return Response(status_code=204, headers={'Access-Control-Allow-Origin': origin,
                'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
                'Access-Control-Allow-Headers': 'Authorization,Content-Type,If-Match,If-None-Match,Idempotency-Key,Last-Event-ID,X-Expected-Versions',
                'X-Request-ID': request_id, 'Vary': 'Origin'})
        path = request.url.path
        f27 = path.startswith(('/api/prompt-groups', '/api/prompt-imports', '/api/safety/')) or path == '/api/settings' or path == '/api/tasks' and request.method == 'POST'
        if 'token' in request.query_params or 'access_token' in request.query_params:
            raise Failure(422, 'input:invalid')
        if path.startswith('/api') and path != '/api/session/bootstrap':
            auth = request.headers.getlist('authorization')
            if len(auth) != 1 or not hmac.compare_digest(auth[0], 'Bearer ' + rt.token):
                raise Failure(401, 'auth:required' if not auth else 'auth:invalid')
        write = request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
        if write and rt.draining:
            raise Failure(503, 'process:draining')
        if request.headers.get('content-encoding'):
            raise Failure(415, 'request:encoding_unsupported')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > (101 if path == '/api/prompt-imports/preview' else 16)*1024*1024:
                raise Failure(413, 'request:too_large')
        request._body = bytes(raw)
        content_type = request.headers.get('content-type', '')
        if raw and write and 'json' not in content_type and 'multipart/form-data' not in content_type:
            raise Failure(415, 'request:media_type_unsupported')
        if 'multipart/form-data' in content_type and len(raw) > 11*1024*1024 and path != '/api/prompt-imports/preview':
            raise Failure(413, 'request:too_large')
        if path.startswith(('/api/dictionaries', '/api/rules', '/api/tags/imports')) and len(raw) > 10*1024*1024:
            raise Failure(413, 'request:too_large')
        value = strict_json(raw) if raw and 'json' in content_type else None
        lock, cached, identity = None, None, None
        if f27 and write and not request.headers.get('idempotency-key'):
            raise Failure(428, 'revision:required')
        if request.method == 'POST' and path != '/api/session/bootstrap' and not f27:
            try:
                key = str(UUID(request.headers.get('idempotency-key', '')))
            except ValueError:
                raise Failure(428, 'concurrency:precondition_required') from None
            binding = (request.method, path, key)
            digest = hashlib.sha256(canonical(value) if value is not None else raw).hexdigest()
            entry = rt.idempotency.get(binding)
            if entry is None:
                entry = rt.idempotency[binding] = [digest, asyncio.Lock(), None]
            if entry[0] != digest:
                raise Failure(409, 'idempotency:key_conflict')
            lock = entry[1]
            await lock.acquire()
            cached = entry[2]
            identity = entry
        try:
            if cached:
                return Response(cached[0], status_code=cached[1], headers=cached[2])
            result = await call_next(request)
            result.headers['X-Request-ID'] = request_id
            if origin == rt.dev_origin and origin:
                result.headers['Access-Control-Allow-Origin'] = origin
                result.headers['Vary'] = 'Origin'
            if write:
                content = b''.join([chunk async for chunk in result.body_iterator])
                headers = dict(result.headers)
                unknown = result.status_code == 503 and b'"storage:commit_unknown"' in content
                if identity is not None and (result.status_code < 400 or unknown):
                    identity[2] = (content, result.status_code, headers)
                return Response(content, status_code=result.status_code, headers=headers)
            return result
        finally:
            if lock:
                lock.release()
    except Exception as exc:
        return response(exc, request_id)
