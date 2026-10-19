"""Anima attention syntax and line-aware conversion to NAI."""
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
import re

from .nai_convert import (
    ConversionPolicy, ConversionResult, ConvertedToken, LexKind, LexResult, LexToken,
    ParseResult, SourceSpan, WarningCode, WeightedTag, _NUMBER, _lines_by_source, _policy, _sorted, _warning, _render_storage,
)

_EXPLICIT = re.compile(r':([^():\[\],\s\\]*)\)')


def _line_blocks(text):
    """Yield each source line separately so line breaks and blank lines survive."""
    offset = 0
    lines = text.splitlines(keepends=True)
    if text.endswith(('\n', '\r')):
        lines.append('')
    for line in lines:
        body = line.rstrip('\r\n')
        natural = bool(re.search(r'[.!?](?:\s|$)', body)) or any(
            len(part.split()) >= 4 for part in re.split(r'(?<!\\),', body))
        yield body, natural or not body.strip(), offset
        offset += len(line)


def scan_anima(text: str, *, policy: ConversionPolicy, storage: bool = False) -> LexResult:
    _policy(policy)
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if len(text) > policy.max_chars:
        return LexResult((), (_warning(WarningCode.W_INPUT_LIMIT, SourceSpan(policy.max_chars, len(text))),))
    items, warnings = [], []
    i = 0
    while i < len(text):
        start, c = i, text[i]
        i += 1
        kind, value = LexKind.TEXT, c
        if c == '\\':
            kind = LexKind.ESCAPED
            if i < len(text):
                value = text[i]
                i += 1
            else:
                warnings.append(_warning(WarningCode.W_DANGLING_ESCAPE, SourceSpan(start, i)))
        elif c in '()[]':
            kind = {'(': LexKind.OPEN_UP, ')': LexKind.CLOSE_UP,
                    '[': LexKind.OPEN_DOWN, ']': LexKind.CLOSE_DOWN}[c]
        elif c in ',\r\n':
            kind = LexKind.SEP
        elif c == ':':
            match = _EXPLICIT.match(text, start)
            if match:
                number = match[1]
                if len(number) <= policy.max_numeric_chars and _NUMBER.fullmatch(number):
                    kind, value = LexKind.CLOSE_UP, Decimal(number)
                    i = start + len(match[0])
                else:
                    code = WarningCode.W_NUMERIC_LIMIT if storage and len(number) > policy.max_numeric_chars else WarningCode.W_INVALID_NUMBER
                    warnings.append(_warning(code, SourceSpan(start, start + len(match[0]) - 1)))
        items.append(LexToken(kind, text[start:i], SourceSpan(start, i), value))
    return LexResult(tuple(items), _sorted(warnings))


def parse_anima(items: tuple[LexToken, ...], *, policy: ConversionPolicy, storage: bool = False) -> ParseResult:
    _policy(policy)
    result, stack, buffer, warnings = [], [], [], []
    start = end = 0

    def emit():
        if buffer:
            value = ''.join(buffer) if storage else ''.join(buffer).strip()
            if value.strip():
                result.append(WeightedTag(value, Decimal(1), SourceSpan(start, end)))
            buffer.clear()

    def close(kind, span, index, factor):
        if index == len(result):
            warnings.append(_warning(WarningCode.W_EMPTY_BLOCK, span))
        for j in range(index, len(result)):
            tag = result[j]
            result[j] = WeightedTag(tag.value, tag.raw_weight * factor, tag.span)

    with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
        for item in items:
            kind = item.kind
            if kind in (LexKind.OPEN_UP, LexKind.OPEN_DOWN):
                if len(stack) >= policy.max_depth:
                    return ParseResult((), (_warning(WarningCode.W_DEPTH_LIMIT, item.span),))
                emit()
                stack.append((kind, item.span, len(result)))
            elif kind in (LexKind.CLOSE_UP, LexKind.CLOSE_DOWN):
                expected = LexKind.OPEN_UP if kind == LexKind.CLOSE_UP else LexKind.OPEN_DOWN
                if not stack or stack[-1][0] != expected:
                    warnings.append(_warning(WarningCode.W_UNMATCHED_CLOSE, item.span))
                    continue
                emit()
                factor = item.value if isinstance(item.value, Decimal) else (Decimal('1.1') if kind == LexKind.CLOSE_UP else 1 / Decimal('1.1'))
                close(*stack.pop(), factor)
            elif kind == LexKind.SEP:
                emit()
            else:
                if not buffer:
                    start = item.span.start
                end = item.span.end
                buffer.append(item.value)
        emit()
        while stack:
            frame = stack.pop()
            warnings.append(_warning(WarningCode.W_UNCLOSED_BRACKET, frame[1]))
            close(*frame, Decimal('1.1') if frame[0] == LexKind.OPEN_UP else 1 / Decimal('1.1'))
    return ParseResult(tuple(result), _sorted(warnings))


def _inverse_weight(weight, artist, policy):
    if not artist and weight > 1:
        weight = 1 + (weight - 1) / policy.content_weight_ratio
    return weight


def convert_anima_to_nai(text: str, *, policy: ConversionPolicy | None = None,
                         body_spaces: bool = False) -> ConversionResult:
    """Preserve natural-language lines verbatim; convert tag lines one by one."""
    policy = ConversionPolicy() if policy is None else _policy(policy)
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if len(text) > policy.max_chars:
        return ConversionResult('', (), (_warning(WarningCode.W_INPUT_LIMIT, SourceSpan(policy.max_chars, len(text))),))
    if body_spaces:
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        lexed = scan_anima(text, policy=policy, storage=True)
        parsed = parse_anima(lexed.items, policy=policy, storage=True)
        losses = []
        with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
            def weight_for(weight, artist):
                exact = _inverse_weight(weight, artist, policy)
                rounded = exact.quantize(Decimal('.01'))
                return rounded
            result = _render_storage(text, lexed, parsed.items, weight_for, anima=True)
            for tag, token in zip(parsed.items, result.tokens):
                artist = token.rendered.lstrip().startswith('artist:') or '::artist:' in token.rendered
                if _inverse_weight(tag.raw_weight, artist, policy) != token.weight:
                    losses.append(_warning(WarningCode.W_NORMALIZATION_ROUNDED, tag.span))
        return ConversionResult(result.text, result.tokens,
                                _sorted(lexed.warnings + parsed.warnings + result.warnings + tuple(losses)))
    output, tokens, warnings = [], [], []
    # Consecutive tag lines form one block so brackets may span lines;
    # the output still keeps every source line in place.
    blocks = []
    for body, natural, offset in _line_blocks(text):
        if natural:
            blocks.append((True, body, offset))
        elif blocks and not blocks[-1][0]:
            start = blocks[-1][2]
            blocks[-1] = (False, text[start:offset + len(body)], start)
        else:
            blocks.append((False, body, offset))

    with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
        for natural, body, offset in blocks:
            if natural:
                output.append(body)
                continue
            pending = []
            lexed = scan_anima(body, policy=policy)
            shifted = tuple(LexToken(t.kind, t.raw, SourceSpan(t.span.start + offset, t.span.end + offset), t.value) for t in lexed.items)
            parsed = parse_anima(shifted, policy=policy)
            if any(w.code == WarningCode.W_DEPTH_LIMIT for w in parsed.warnings):
                return ConversionResult('', (), parsed.warnings)
            warnings.extend(_warning(w.code, SourceSpan(w.span.start + offset, w.span.end + offset)) for w in lexed.warnings)
            warnings.extend(parsed.warnings)
            for tag in parsed.items:
                artist = tag.value.startswith('@')
                value = tag.value[1:].strip() if artist else tag.value
                if artist and not value:
                    warnings.append(_warning(WarningCode.W_EMPTY_ARTIST, tag.span))
                    continue
                if tag.raw_weight <= 0:
                    continue
                value = ('artist:' if artist else '') + value.replace(' ', '_')
                # U25: inverse of the forward rule; weights <= 1 map 1:1, so no floor is needed.
                weight = _inverse_weight(tag.raw_weight, artist, policy)
                weight = weight.quantize(Decimal('.01'))
                if weight <= 0:
                    continue
                rendered = ''.join('\\' + c if c in '\\{}[],:|，' else c for c in value)
                # The artist prefix colon is NAI syntax, not literal tag content.
                if artist:
                    rendered = rendered.replace('artist\\:', 'artist:', 1)
                if weight != 1:
                    rendered = f"{format(weight, 'f').rstrip('0').rstrip('.')}::{rendered}::"
                pending.append(ConvertedToken(value, tag.raw_weight, weight, tag.span, rendered))
            tokens.extend(pending)
            output.extend(_lines_by_source(body, pending, offset))
    return ConversionResult('\n'.join(output), tuple(tokens), _sorted(warnings))
