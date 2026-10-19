"""Bounded multipart input using the standard library; never trusts filenames."""
import asyncio
from email.parser import BytesHeaderParser
from email.policy import default
from stylelora.runtime.contracts import Failure, fields

IMPORT_BODY_LIMIT = 101 * 1024**2
IMPORT_FILE_LIMIT = 100 * 1024**2
IMPORT_FILE_COUNT = 1000


async def bounded_body(request, limit, code='request:too_large'):
    length = request.headers.get('content-length')
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise Failure(400, 'request:malformed_body')
        if int(length) > limit:
            raise Failure(413, code)
    raw = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(raw):
            raise Failure(413, code)
        raw.extend(chunk)
    return bytes(raw)


async def upload(request, allowed, required, *, limit=10*1024*1024, binary=('file',),
                 repeated=(), file_limit=None, file_count=None, limit_code='request:too_large'):
    content_type = request.headers.get('content-type', '')
    if not content_type.startswith('multipart/form-data;'):
        raise Failure(415, 'request:media_type_unsupported')
    try:
        message = BytesHeaderParser(policy=default).parsebytes(
            b'Content-Type: ' + content_type.encode('ascii') + b'\r\n\r\n')
        boundary = message.get_boundary().encode('ascii')
    except (UnicodeError, AttributeError):
        raise Failure(422, 'schema:invalid') from None
    if not 1 <= len(boundary) <= 70 or b'\r' in boundary or b'\n' in boundary:
        raise Failure(422, 'schema:invalid')
    length = request.headers.get('content-length')
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise Failure(400, 'request:malformed_body')
        if int(length) > limit:
            raise Failure(413, limit_code)

    async def chunks():
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                raise Failure(413, limit_code)
            # Bound each synchronous parsing step even when ASGI supplies a huge chunk.
            for start in range(0, len(chunk), 64 * 1024):
                yield chunk[start:start + 64 * 1024]
                await asyncio.sleep(0)

    stream = chunks().__aiter__()
    buffer = bytearray()

    async def fill():
        try:
            buffer.extend(await anext(stream))
        except StopAsyncIteration:
            raise Failure(422, 'schema:invalid') from None

    async def header_until(marker):
        while True:
            index = buffer.find(marker)
            if index >= 0:
                if index > 16 * 1024:
                    raise Failure(413, limit_code)
                value = bytes(buffer[:index])
                del buffer[:index + len(marker)]
                return value
            if len(buffer) > 16 * 1024:
                raise Failure(413, limit_code)
            await fill()

    if await header_until(b'\r\n') != b'--' + boundary:
        raise Failure(422, 'schema:invalid')
    result = {}
    total, count = 0, 0
    marker = b'\r\n--' + boundary
    while True:
        part = BytesHeaderParser(policy=default).parsebytes(await header_until(b'\r\n\r\n') + b'\r\n\r\n')
        key = part.get_param('name', header='content-disposition')
        if part.get_content_maintype() == 'multipart' or part.defects or (key in result and key not in repeated) or key not in allowed:
            raise Failure(422, 'schema:invalid')
        if part.get('content-transfer-encoding', 'binary').lower() not in ('binary', '8bit', '7bit'):
            raise Failure(422, 'schema:invalid')
        is_file = key in binary or key in repeated
        if is_file:
            count += 1
            if file_count is not None and count > file_count:
                raise Failure(413, limit_code)
        value = bytearray()
        while True:
            index = buffer.find(marker)
            if index >= 0 and len(buffer) < index + len(marker) + 2:
                await fill()
                continue
            found = index >= 0 and buffer[index + len(marker):index + len(marker) + 2] in (b'\r\n', b'--')
            take = index if found else index + 2 if index >= 0 else max(0, len(buffer) - len(marker) - 1)
            if is_file:
                total += take
                if file_limit is not None and total > file_limit:
                    raise Failure(413, limit_code)
            value.extend(buffer[:take])
            del buffer[:take]
            if found:
                closing = buffer[len(marker):len(marker) + 2] == b'--'
                del buffer[:len(marker) + 2]
                break
            if index < 0:
                await fill()
        value = bytes(value)
        if key in repeated:
            filename = part.get_filename()
            if filename is None:
                raise Failure(422, 'schema:invalid')
            result.setdefault(key, []).append((filename, value))
        else:
            try:
                result[key] = value if key in binary else value.decode('utf-8')
            except UnicodeError:
                raise Failure(422, 'schema:invalid') from None
        if closing:
            # Consume bounded epilogue so the request body cap still applies.
            async for _ in stream:
                pass
            break
    return fields(result, allowed, required)
