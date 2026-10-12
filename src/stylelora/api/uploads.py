"""Bounded multipart input using the standard library; never trusts filenames."""
from email.parser import BytesParser
from email.policy import default
from stylelora.runtime.contracts import Failure, fields


async def upload(request, allowed, required):
    content_type = request.headers.get('content-type', '')
    if not content_type.startswith('multipart/form-data;'):
        raise Failure(415, 'request:media_type_unsupported')
    raw = await request.body()
    if len(raw) > 10*1024*1024:
        raise Failure(413, 'request:too_large')
    message = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
    if not message.is_multipart() or message.defects:
        raise Failure(422, 'schema:invalid')
    result = {}
    for part in message.iter_parts():
        key = part.get_param('name', header='content-disposition')
        if part.is_multipart() or part.defects or key in result or key not in allowed:
            raise Failure(422, 'schema:invalid')
        value = part.get_payload(decode=True)
        if not isinstance(value, bytes):
            raise Failure(422, 'schema:invalid')
        try:
            result[key] = value if key == 'file' else value.decode('utf-8')
        except UnicodeError:
            raise Failure(422, 'schema:invalid') from None
    return fields(result, allowed, required)
