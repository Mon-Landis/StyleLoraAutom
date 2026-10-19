"""Bounded, deterministic PNG positive-prompt extraction (F31 T2).

Text is preserved verbatim. Diagnostics contain only fixed messages, never
metadata values. Batch accounting and prompt normalization belong to callers.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from io import BytesIO
import re
import struct
import warnings
import zlib

from PIL import Image

from .types import Issue, PromptError
from .validation import strict_loads


@dataclass(frozen=True)
class MetadataLimits:
    file_bytes: int = 10 * 1024 * 1024
    text_chunk_bytes: int = 1024 * 1024
    text_decoded_bytes: int = 2 * 1024 * 1024
    total_text_bytes: int = 4 * 1024 * 1024
    pixels: int = 40_000_000

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or not 0 < value <= item.default:
                raise ValueError('Metadata limits must be positive and within F31 caps')


@dataclass(frozen=True)
class ExtractedPrompt:
    text: str
    source: str | None
    issues: tuple[Issue, ...]


_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_TEXT = {b'tEXt', b'iTXt', b'zTXt'}
_PIXEL_CHUNKS = {b'IHDR', b'PLTE', b'IDAT', b'IEND', b'tRNS'}
_MAX_ISSUES = 32


class _Invalid(Exception):
    pass


class _Limit(Exception):
    pass


class _Context:
    def __init__(self, limits):
        self.limits = limits
        self.total = 0
        self.issues = []

    def issue(self, code='metadata:invalid', severity='warning'):
        issue = Issue(code, 'PNG metadata is invalid or exceeds limits.', severity=severity)
        if len(self.issues) < _MAX_ISSUES:
            self.issues.append(issue)
        elif severity == 'error':
            self.issues[-1] = issue

    def budget(self):
        return min(self.limits.text_decoded_bytes, self.limits.total_text_bytes - self.total)

    def account(self, size):
        if size > self.budget():
            raise _Limit
        self.total += size


def _inflate(data, maximum, *, gzip=False):
    decoder = zlib.decompressobj(31 if gzip else 15)
    try:
        result = decoder.decompress(data, maximum + 1)
    except zlib.error:
        raise _Invalid from None
    if len(result) > maximum or decoder.unconsumed_tail:
        raise _Limit
    if not decoder.eof or decoder.unused_data:
        raise _Invalid
    return result


def _split(data):
    head, sep, tail = data.partition(b'\0')
    if not sep:
        raise _Invalid
    return head, tail


def _text_chunk(kind, data, ctx):
    keyword, body = _split(data)
    if not 1 <= len(keyword) <= 79:
        raise _Invalid
    encoding = 'latin-1'
    if kind == b'zTXt':
        if not body or body[0] != 0:
            raise _Invalid
        body = _inflate(body[1:], ctx.budget())
    elif kind == b'iTXt':
        if len(body) < 2 or body[0] not in (0, 1) or body[1] != 0:
            raise _Invalid
        flag = body[0]
        language, rest = _split(body[2:])
        translated, body = _split(rest)
        language.decode('ascii')
        translated.decode('utf-8')
        if flag:
            body = _inflate(body, ctx.budget())
        encoding = 'utf-8'
    ctx.account(len(body))
    return keyword.decode('latin-1'), body.decode(encoding)


def _container(data, ctx):
    if len(data) > ctx.limits.file_bytes:
        raise _Limit
    if not data.startswith(_SIGNATURE):
        raise _Invalid
    pos = 8
    pixels = bytearray(_SIGNATURE)
    texts = []
    seen_header = seen_data = ended_data = False
    while pos < len(data):
        if len(data) - pos < 12:
            raise _Invalid
        length, kind = struct.unpack_from('>I4s', data, pos)
        end = pos + 12 + length
        if end > len(data) or length > 0x7fffffff:
            raise _Invalid
        if any(not (65 <= c <= 90 or 97 <= c <= 122) for c in kind) or kind[2] & 32:
            raise _Invalid
        body = memoryview(data)[pos + 8:end - 4]
        crc = zlib.crc32(body, zlib.crc32(kind)) & 0xffffffff
        if crc != struct.unpack_from('>I', data, end - 4)[0]:
            raise _Invalid
        if not seen_header:
            if kind != b'IHDR' or length != 13:
                raise _Invalid
            width, height = struct.unpack_from('>II', body)
            if not width or not height:
                raise _Invalid
            if width * height > ctx.limits.pixels:
                raise _Limit
            seen_header = True
        elif kind == b'IHDR':
            raise _Invalid
        if kind == b'IDAT':
            if ended_data:
                raise _Invalid
            seen_data = True
        elif seen_data:
            ended_data = True
        if not kind[0] & 32 and kind not in _PIXEL_CHUNKS:
            raise _Invalid
        if kind in _TEXT:
            if length > ctx.limits.text_chunk_bytes:
                raise _Limit
            try:
                keyword, text = _text_chunk(kind, bytes(body), ctx)
                if keyword in ('Comment', 'Description', 'parameters'):
                    texts.append((keyword, text))
            except (_Invalid, UnicodeError):
                ctx.issue()
        if kind in _PIXEL_CHUNKS:
            pixels.extend(memoryview(data)[pos:end])
        pos = end
        if kind == b'IEND':
            if length or pos != len(data) or not seen_data:
                raise _Invalid
            return texts, pixels
    raise _Invalid


def _json(text, ctx):
    try:
        value = strict_loads(text)
        if not isinstance(value, dict):
            raise _Invalid
        return value
    except (PromptError, _Invalid):
        ctx.issue()
        return None


def _v4(obj):
    v4 = obj['v4_prompt']
    if not isinstance(v4, dict) or not isinstance(v4.get('caption'), dict):
        raise _Invalid
    caption = v4['caption']
    base = caption.get('base_caption')
    characters = caption.get('char_captions', [])
    if not isinstance(base, str) or not isinstance(characters, list):
        raise _Invalid
    parts = [base]
    for character in characters:
        if not isinstance(character, dict) or not isinstance(character.get('char_caption'), str):
            raise _Invalid
        text = character['char_caption']
        if text.strip():
            parts.append(text)
    return '\n\n'.join(parts)


def _positive(objects, texts, ctx):
    for field in ('v4_prompt', 'prompt'):
        for obj in objects:
            if field not in obj:
                continue
            try:
                text = _v4(obj) if field == 'v4_prompt' else obj[field]
                if not isinstance(text, str):
                    raise _Invalid
                if text.strip():
                    return text, 'Comment.' + field
            except _Invalid:
                ctx.issue()
    for field in ('Description', 'parameters'):
        for keyword, text in texts:
            if keyword != field:
                continue
            if field == 'parameters':
                boundary = re.search(r'(?m)^[ \t]*(?:Negative prompt:|Steps: \d+\s*,)', text)
                if boundary:
                    text = text[:boundary.start()]
            if text.strip():
                return text, field
    return '', None


def _stealth(image, ctx):
    if 'A' not in image.getbands() or image.width * image.height < 152:
        return '', None
    # Transposition gives x-outer/y-inner order without creating an RGBA copy.
    with image.getchannel('A') as alpha:
        with alpha.transpose(Image.Transpose.TRANSPOSE) as columns:
            samples = columns.tobytes()

    def read(start, size):
        result = bytearray(size)
        for index in range(size):
            value = 0
            for sample in samples[start + index * 8:start + index * 8 + 8]:
                value = (value << 1) | (sample & 1)
            result[index] = value
        return bytes(result)

    marker = read(0, 15)
    if marker not in (b'stealth_pngcomp', b'stealth_pnginfo'):
        return '', None
    bits = int.from_bytes(read(120, 4), 'big')
    if bits % 8 or bits > len(samples) - 152:
        raise _Invalid
    if bits // 8 > ctx.limits.text_chunk_bytes:
        raise _Limit
    payload = read(152, bits // 8)
    if marker == b'stealth_pngcomp':
        payload = _inflate(payload, ctx.budget(), gzip=True)
    ctx.account(len(payload))
    obj = _json(payload.decode('utf-8'), ctx)
    if obj is None:
        return '', None
    objects = [obj]
    if isinstance(obj.get('Comment'), str):
        comment = _json(obj['Comment'], ctx)
        objects = [comment] if comment is not None else []
    texts = [(key, obj[key]) for key in ('Description', 'parameters') if isinstance(obj.get(key), str)]
    text, source = _positive(objects, texts, ctx)
    return text, marker.decode('ascii') if source else None


def extract_positive(data: bytes, *, limits: MetadataLimits = MetadataLimits()) -> ExtractedPrompt:
    """Extract positive text; blank/invalid input returns text='' and source=None.

    Sources: Comment.v4_prompt, Comment.prompt, Description, parameters,
    stealth_pngcomp, stealth_pnginfo. Recoverable candidate failures are warnings;
    invalid images and resource violations are errors (metadata:invalid or
    metadata:limit). At most 32 diagnostics are returned. No metadata is logged.
    Limits may be lowered, but cannot exceed the F31 per-image caps.
    """
    ctx = _Context(limits)
    try:
        texts, pixels = _container(data, ctx)
        # Pillow sees only pixel chunks: malformed optional text can fall back,
        # and Pillow's independent text decoding cannot bypass our budgets.
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(pixels)) as image:
                image.load()
                objects = []
                for keyword, text in texts:
                    if keyword == 'Comment':
                        obj = _json(text, ctx)
                        if obj is not None:
                            objects.append(obj)
                text, source = _positive(objects, texts, ctx)
                if source is None:
                    text, source = _stealth(image, ctx)
        return ExtractedPrompt(text, source, tuple(ctx.issues))
    except _Limit:
        ctx.issue('metadata:limit', 'error')
    except (_Invalid, UnicodeError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        ctx.issue(severity='error')
    return ExtractedPrompt('', None, tuple(ctx.issues))
