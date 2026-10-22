"""Pure NAI dialect conversion; independent of catalog-based prompt parsing."""

from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
from enum import Enum
import re
from typing import Literal


class WarningCode(str, Enum):
    W_UNCLOSED_BRACKET = 'W_UNCLOSED_BRACKET'
    W_UNMATCHED_CLOSE = 'W_UNMATCHED_CLOSE'
    W_UNCLOSED_NUMERIC = 'W_UNCLOSED_NUMERIC'
    W_INVALID_NUMBER = 'W_INVALID_NUMBER'
    W_UNKNOWN_DCOLON = 'W_UNKNOWN_DCOLON'
    W_UNSUPPORTED_ALTERNATION = 'W_UNSUPPORTED_ALTERNATION'
    W_EMPTY_BLOCK = 'W_EMPTY_BLOCK'
    W_EMPTY_ARTIST = 'W_EMPTY_ARTIST'
    W_DANGLING_ESCAPE = 'W_DANGLING_ESCAPE'
    W_DEPTH_LIMIT = 'W_DEPTH_LIMIT'
    W_INPUT_LIMIT = 'W_INPUT_LIMIT'
    W_WEIGHT_CLAMPED = 'W_WEIGHT_CLAMPED'
    W_NORMALIZATION_ROUNDED = 'normalization:rounded'
    W_NUMERIC_LIMIT = 'W_NUMERIC_LIMIT'


class LexKind(str, Enum):
    TEXT = 'TEXT'
    ESCAPED = 'ESCAPED'
    OPEN_UP = 'OPEN_UP'
    CLOSE_UP = 'CLOSE_UP'
    OPEN_DOWN = 'OPEN_DOWN'
    CLOSE_DOWN = 'CLOSE_DOWN'
    NUM_OPEN = 'NUM_OPEN'
    DCOLON = 'DCOLON'
    INVALID_NUM = 'INVALID_NUM'
    SEP = 'SEP'
    PIPE = 'PIPE'
    EOF = 'EOF'


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int


@dataclass(frozen=True)
class ConversionPolicy:
    bracket_mode: Literal['multiplicative'] = 'multiplicative'
    ordinary_underscores: Literal['preserve', 'space'] = 'space'
    content_weight_ratio: Decimal = Decimal('0.6')
    max_weight: Decimal = Decimal('3.2')
    decimal_places: int = 2
    max_depth: int = 128
    max_chars: int = 1000000
    max_numeric_chars: int = 64

    def __post_init__(self):
        if not isinstance(self.content_weight_ratio, Decimal):
            raise TypeError('content_weight_ratio must be Decimal')
        if not self.content_weight_ratio.is_finite() or not 0 < self.content_weight_ratio <= 1:
            raise ValueError('content_weight_ratio must be in (0, 1]')
        for name in ('bracket_mode', 'ordinary_underscores'):
            if not isinstance(getattr(self, name), str):
                raise TypeError(name)
        if self.bracket_mode != 'multiplicative':
            raise ValueError('bracket_mode must be multiplicative')
        if self.ordinary_underscores not in ('preserve', 'space'):
            raise ValueError('ordinary_underscores must be preserve or space')
        if not isinstance(self.max_weight, Decimal):
            raise TypeError('max_weight must be Decimal')
        w = self.max_weight
        if not w.is_finite() or not Decimal(1) < w <= Decimal('3.2'):
            raise ValueError('max_weight must be finite and in (1, 3.2]')
        with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
            if w != w.quantize(Decimal('.01')):
                raise ValueError('max_weight must have at most two decimal places')
        for name in ('decimal_places', 'max_depth', 'max_chars', 'max_numeric_chars'):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(name)
            if value <= 0 or (name == 'decimal_places' and value != 2):
                raise ValueError(name)


@dataclass(frozen=True)
class ConversionWarning:
    code: WarningCode
    span: SourceSpan
    detail: str


@dataclass(frozen=True)
class LexToken:
    kind: LexKind
    raw: str
    span: SourceSpan
    value: str | Decimal | None


@dataclass(frozen=True)
class LexResult:
    items: tuple[LexToken, ...]
    warnings: tuple[ConversionWarning, ...]


@dataclass(frozen=True)
class WeightedTag:
    value: str
    raw_weight: Decimal
    span: SourceSpan


@dataclass(frozen=True)
class ParseResult:
    items: tuple[WeightedTag, ...]
    warnings: tuple[ConversionWarning, ...]


@dataclass(frozen=True)
class ScaledTag:
    value: str
    raw_weight: Decimal
    weight: Decimal
    span: SourceSpan


@dataclass(frozen=True)
class CompressionResult:
    items: tuple[ScaledTag, ...]
    warnings: tuple[ConversionWarning, ...]


@dataclass(frozen=True)
class ConvertedToken:
    value: str
    raw_weight: Decimal
    weight: Decimal
    span: SourceSpan
    rendered: str


@dataclass(frozen=True)
class ConversionResult:
    text: str
    tokens: tuple[ConvertedToken, ...]
    warnings: tuple[ConversionWarning, ...]


def _policy(policy):
    if not isinstance(policy, ConversionPolicy):
        raise TypeError('policy must be ConversionPolicy')
    return policy


def _warning(code, span):
    return ConversionWarning(code, span, code.value)


def _sorted(warnings):
    return tuple(sorted(warnings, key=lambda w: (w.span.start, w.span.end, w.code.value)))


_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z')
_CANDIDATE = re.compile(r'[^\s\\{}\[\],，|:]+::')
_CANDIDATE_BODY = re.compile(r'[^\s\\{}\[\],，|:]+')
# `_` is a NAI word separator: before a numeric weight it behaves like
# whitespace, e.g. `_1.8::x::`, `._3::y::`, `,_1.2::z::`. Keep it out of the
# body so it is retained as plain text, but allow it as a leading boundary.
_STORAGE_CANDIDATE = re.compile(r'_*[^\s\\{}\[\],，|:_]+::')
_STORAGE_BODY = re.compile(r'[^\s\\{}\[\],，|:_]+')


def _leading_separator_len(candidate: str) -> int:
    """Count leading `_` separators, plus a lone `.` glued to them.

    `_1.8::` -> 1, `._1.6::` -> 2, `.__3::` -> 3. A `.` only counts when
    followed by `_`, so `.5::` stays a plain number.
    """
    if not candidate.startswith(('.', '_')):
        return 0
    if candidate.startswith('.'):
        n = 1
        while n < len(candidate) and candidate[n] == '_':
            n += 1
        return n if n > 1 else 0
    return len(candidate) - len(candidate.lstrip('_'))


_BRACKETS = {'{': LexKind.OPEN_UP, '}': LexKind.CLOSE_UP,
             '[': LexKind.OPEN_DOWN, ']': LexKind.CLOSE_DOWN}
_BRACKET_OPENERS = (LexKind.OPEN_UP, LexKind.OPEN_DOWN)
_BRACKET_CLOSERS = (LexKind.CLOSE_UP, LexKind.CLOSE_DOWN)


def scan_nai(text: str, *, policy: ConversionPolicy, storage: bool = False) -> LexResult:
    _policy(policy)
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if len(text) > policy.max_chars:
        return LexResult((), (_warning(WarningCode.W_INPUT_LIMIT,
                                      SourceSpan(policy.max_chars, len(text))),))
    items, warnings = [], []
    i = 1 if not storage and text.startswith('\ufeff') else 0
    pending = i
    active, eligible = False, True
    storage_skip = 0
    storage_depth = 0

    def flush(end):
        if pending < end:
            raw = text[pending:end]
            items.append(LexToken(LexKind.TEXT, raw, SourceSpan(pending, end), raw))

    def _storage_opens_numeric(pos):
        """Storage mode: does a numeric `weight::` block start at ``pos``?

        A leading run of underscores is a word separator (`_1.8::`,
        `._3::`, `,_1.2::`) and is not part of the number.
        """
        c = text[pos]
        if c in '+-.0123456789' and (pos == 0 or text[pos - 1] not in '+-.0123456789'):
            return True
        if text[pos:pos + 3].lower() == 'nan' or text[pos:pos + 8].lower() == 'infinity':
            return True
        if c == '_':
            match = _STORAGE_CANDIDATE.match(text, pos)
            if match is not None:
                candidate = match[0][:-2].lstrip('_')
                return bool(candidate) and (candidate[0] in '+-.0123456789'
                                            or candidate.lower() in ('nan', 'infinity'))
        return False

    while i < len(text):
        c = text[i]
        kind, value, end = None, None, i + 1
        if c == '\\':
            kind = LexKind.ESCAPED
            if end < len(text):
                value, end = text[end], end + 1
            else:
                value = '\\'
                warnings.append(_warning(WarningCode.W_DANGLING_ESCAPE, SourceSpan(i, end)))
            eligible = False
        elif c in ',，\r\n':
            kind = LexKind.SEP
            if c == '\r' and text[i:i + 2] == '\r\n':
                end += 1
            eligible = True
        elif text.startswith('::', i):
            kind, value, end = LexKind.DCOLON, '::', i + 2
            if storage:
                storage_depth = max(0, storage_depth - 1)
            if active:
                active, eligible = False, True
            elif not storage:
                warnings.append(_warning(WarningCode.W_UNKNOWN_DCOLON, SourceSpan(i, end)))
                eligible = False
        elif ((storage and i >= storage_skip and _storage_opens_numeric(i))
              or (not storage and not active and eligible)) and not c.isspace():
            pattern = _STORAGE_CANDIDATE if storage else _CANDIDATE
            match = pattern.match(text, i)
            if storage and match is None:
                body = _STORAGE_BODY.match(text, i)
                storage_skip = body.end() if body else i + 1
            candidate = match[0][:-2] if match else ''
            # Leading underscores are separators, not part of the number; keep
            # them as text before the numeric block. This applies to both the
            # converter and storage scans: `_1.8::`, `._3::`, `,_1.2::`.
            lead = _leading_separator_len(candidate) if candidate else 0
            if lead and candidate[lead:]:
                flush(i + lead)
                i += lead
                pending = i
                candidate = candidate[lead:]
            if not storage and '_' in candidate and not _NUMBER.fullmatch(candidate):
                # `1_2::` is `1` then a new `2::` block, like `1 2::`.
                head = candidate.split('_', 1)[0]
                if head and _NUMBER.fullmatch(head):
                    end = i + len(head)
                    kind, value = LexKind.TEXT, head
                else:
                    eligible = False
            if kind is None and candidate and (candidate[0] in '+-.0123456789'
                                               or candidate.lower() in ('nan', 'infinity')):
                end = i + len(candidate) + 2
                if len(candidate) <= policy.max_numeric_chars and _NUMBER.fullmatch(candidate):
                    kind, value = LexKind.NUM_OPEN, Decimal(candidate)
                    active, eligible = True, True
                    if storage:
                        storage_depth += 1
                elif storage and storage_depth and not _NUMBER.fullmatch(candidate):
                    # A digit-leading tag before a closing :: is body text,
                    # e.g. 3::3d::. Leave the delimiter for the next iteration.
                    storage_skip = end - 2
                    end = i + 1
                else:
                    kind, value = LexKind.INVALID_NUM, text[i:end]
                    code = WarningCode.W_NUMERIC_LIMIT if storage and len(candidate) > policy.max_numeric_chars else WarningCode.W_INVALID_NUMBER
                    warnings.append(_warning(code, SourceSpan(i, end)))
                    eligible = False
            else:
                eligible = False
        elif storage and storage_depth and i >= storage_skip and c.isalpha():
            # Do not reinterpret a tag's numeric suffix (year_2025, model2)
            # as a nested opener. Numeric blocks at word starts still nest.
            body = _CANDIDATE_BODY.match(text, i)
            if body:
                storage_skip = body.end()
        if kind is None:
            if c in _BRACKETS:
                kind = _BRACKETS[c]
                eligible = c in '{['
            elif c == '|':
                kind, value = LexKind.PIPE, c
                warnings.append(_warning(WarningCode.W_UNSUPPORTED_ALTERNATION, SourceSpan(i, end)))
                eligible = False
            elif c.isspace():
                # Whitespace is a word boundary: `s 1.5::x::` starts a numeric block too.
                eligible = not active
            elif c == '_' and not storage:
                # NAI writes spaces as underscores: `_1.8::`, `._3::`, `,_1.2::`
                # must behave like their space-separated counterparts.
                eligible = not active
            else:
                eligible = False
        if kind is not None:
            flush(i)
            items.append(LexToken(kind, text[i:end], SourceSpan(i, end), value))
            pending = end
        i = end
    flush(i)
    items.append(LexToken(LexKind.EOF, '', SourceSpan(i, i), None))
    return LexResult(tuple(items), _sorted(warnings))


def parse_nai(items: tuple[LexToken, ...], *, policy: ConversionPolicy, storage: bool = False) -> ParseResult:
    _policy(policy)
    result, warnings, stack, buffer = [], [], [], []
    start = end = 0
    balance, numeric = 0, None
    powers = {}
    numerics = []

    def emit():
        if buffer:
            value = ''.join(buffer)
            if value.strip():
                if balance not in powers:
                    powers[balance] = Decimal('1.05') ** balance
                weight = powers[balance] * (numeric[0] if numeric else Decimal(1))
                result.append(WeightedTag(value, weight, SourceSpan(start, end)))
            buffer.clear()

    def empty(span, count):
        if len(result) == count:
            warnings.append(_warning(WarningCode.W_EMPTY_BLOCK, span))

    with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
        for item in items:
            kind = item.kind
            if kind in (LexKind.OPEN_UP, LexKind.OPEN_DOWN):
                if len(stack) >= policy.max_depth:
                    return ParseResult((), (_warning(WarningCode.W_DEPTH_LIMIT, item.span),))
                emit()
                stack.append((kind, item.span, len(result)))
                balance += 1 if kind == LexKind.OPEN_UP else -1
            elif kind in (LexKind.CLOSE_UP, LexKind.CLOSE_DOWN):
                expected = LexKind.OPEN_UP if kind == LexKind.CLOSE_UP else LexKind.OPEN_DOWN
                if not stack or stack[-1][0] != expected:
                    warnings.append(_warning(WarningCode.W_UNMATCHED_CLOSE, item.span))
                    continue
                emit()
                _, span, count = stack.pop()
                empty(span, count)
                balance -= 1 if kind == LexKind.CLOSE_UP else -1
            elif kind == LexKind.NUM_OPEN:
                emit()
                if storage:
                    numerics.append(numeric)
                    factor = numeric[0] if numeric else Decimal(1)
                else:
                    factor = Decimal(1)
                numeric = (item.value * factor, item.span, len(result))
            elif kind == LexKind.DCOLON and numeric:
                emit()
                empty(numeric[1], numeric[2])
                numeric = numerics.pop() if storage else None
            elif kind == LexKind.SEP:
                emit()
            elif kind == LexKind.EOF:
                emit()
            else:
                if not buffer:
                    start = item.span.start
                end = item.span.end
                buffer.append(item.value)
        emit()
    for _, span, count in stack:
        warnings.append(_warning(WarningCode.W_UNCLOSED_BRACKET, span))
        empty(span, count)
    if numeric:
        warnings.append(_warning(WarningCode.W_UNCLOSED_NUMERIC, numeric[1]))
        empty(numeric[1], numeric[2])
    return ParseResult(tuple(result), _sorted(warnings))


def normalize_nai_tags(tags: tuple[WeightedTag, ...], *, policy: ConversionPolicy) -> ParseResult:
    _policy(policy)
    items, warnings = [], []
    for tag in tags:
        value = tag.value.strip()
        artist = value.startswith('artist:')
        if artist:
            value = value[len('artist:'):].lstrip().replace('_', ' ')
        elif policy.ordinary_underscores == 'space':
            value = value.replace('_', ' ')
        value = ' '.join(value.split())
        if artist and not value:
            warnings.append(_warning(WarningCode.W_EMPTY_ARTIST, tag.span))
        elif value:
            items.append(WeightedTag('@' + value if artist else value, tag.raw_weight, tag.span))
    return ParseResult(tuple(items), _sorted(warnings))


def compress_weights(tags: tuple[WeightedTag, ...], *, policy: ConversionPolicy) -> CompressionResult:
    _policy(policy)
    items, warnings = [], []
    with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
        for tag in tags:
            if tag.raw_weight <= 0:
                continue
            # U25: only the boost part (> 1) is compressed; weights <= 1 map 1:1.
            if tag.value.startswith('@') or tag.raw_weight <= 1:
                weight = tag.raw_weight
            else:
                weight = 1 + (tag.raw_weight - 1) * policy.content_weight_ratio
            if weight > policy.max_weight:
                warnings.append(_warning(WarningCode.W_WEIGHT_CLAMPED, tag.span))
            weight = min(weight, policy.max_weight).quantize(Decimal('.01'))
            if weight > 0:
                items.append(ScaledTag(tag.value, tag.raw_weight, weight, tag.span))
    return CompressionResult(tuple(items), _sorted(warnings))


def render_anima(tags: tuple[ScaledTag, ...]) -> tuple[ConvertedToken, ...]:
    items = []
    for tag in tags:
        value = ''.join('\\' + c if c in '\\(){}[],' else ' ' if c in '\r\n' else c
                        for c in tag.value)
        if tag.weight != 1:
            weight = format(tag.weight, 'f').rstrip('0').rstrip('.')
            value = f'({value}:{weight})'
        items.append(ConvertedToken(tag.value, tag.raw_weight, tag.weight, tag.span, value))
    return tuple(items)


def convert_nai_to_anima(text: str, *, policy: ConversionPolicy | None = None) -> ConversionResult:
    policy = ConversionPolicy() if policy is None else _policy(policy)
    lexed = scan_nai(text, policy=policy)
    if lexed.warnings and lexed.warnings[0].code == WarningCode.W_INPUT_LIMIT:
        return ConversionResult('', (), lexed.warnings)
    parsed = parse_nai(lexed.items, policy=policy)
    if parsed.warnings and parsed.warnings[0].code == WarningCode.W_DEPTH_LIMIT:
        return ConversionResult('', (), parsed.warnings)
    normalized = normalize_nai_tags(parsed.items, policy=policy)
    scaled = compress_weights(normalized.items, policy=policy)
    tokens = render_anima(scaled.items)
    warnings = _sorted(lexed.warnings + parsed.warnings + normalized.warnings + scaled.warnings)
    return ConversionResult('\n'.join(_lines_by_source(text, tokens)), tokens, warnings)


def _line_starts(text):
    """Offsets where a new source line begins; escaped line breaks do not split lines."""
    starts, i = [0], 0
    while i < len(text):
        c = text[i]
        if c == '\\':
            i += 2
            continue
        i += 1
        if c == '\r' and i < len(text) and text[i] == '\n':
            i += 1
        if c in '\r\n':
            starts.append(i)
    return starts


def _lines_by_source(text, tokens, offset=0):
    """Keep the source line layout: each tag goes to the line where it starts."""
    starts = [s + offset for s in _line_starts(text)]
    lines = [[] for _ in starts]
    row = 0
    for token in sorted(tokens, key=lambda t: t.span.start):
        while row + 1 < len(starts) and starts[row + 1] <= token.span.start:
            row += 1
        lines[row].append(token.rendered)
    return [', '.join(items) for items in lines]


def _render_storage(text, lexed, tags, weight_for, *, anima=False):
    """Replace semantic spans, retaining separators and whitespace from the lexer."""
    output, tokens, warnings = [], [], []
    dropped_empty = False
    cursor = 0

    def top_level(start):
        """True when ``start`` is not inside a bracket or numeric block."""
        depth = 0
        for item in lexed.items:
            if item.span.start >= start:
                break
            if item.kind in _BRACKET_OPENERS or item.kind == LexKind.NUM_OPEN:
                depth += 1
            elif item.kind in _BRACKET_CLOSERS or item.kind == LexKind.DCOLON:
                if depth:
                    depth -= 1
        return depth == 0

    for tag in tags:
        while cursor < len(lexed.items) and lexed.items[cursor].span.end <= tag.span.start:
            item = lexed.items[cursor]
            if item.kind in (LexKind.TEXT, LexKind.SEP, LexKind.ESCAPED):
                output.append(str(item.value or item.raw).replace('_', ' '))
            cursor += 1
        value = tag.value.replace('_', ' ')
        leading = value[:len(value) - len(value.lstrip())]
        trailing = value[len(value.rstrip()):]
        body = value.strip()
        if not body:
            if top_level(tag.span.start):
                # A lone `_` separator outside a block is ordinary text, not
                # an empty tag: `_1.8::x::` keeps the leading space.
                output.append(text[tag.span.start:tag.span.end].replace('_', ' '))
            else:
                dropped_empty = True
            while cursor < len(lexed.items) and lexed.items[cursor].span.start < tag.span.end:
                cursor += 1
            continue
        artist = (body.startswith('artist:') and
                  text[tag.span.start:tag.span.end].lstrip().startswith('artist:')) if not anima else (
            body.startswith('@') and text[tag.span.start:tag.span.end].lstrip().startswith('@'))
        if anima and artist:
            body = 'artist:' + body[1:]
        weight = weight_for(tag.raw_weight, artist)
        if weight != tag.raw_weight and not anima:
            warnings.append(_warning(WarningCode.W_NORMALIZATION_ROUNDED, tag.span))
        if artist and not body[len('artist:'):].strip():
            warnings.append(_warning(WarningCode.W_EMPTY_ARTIST, tag.span))
        rendered = ''.join('\\' + c if c in '\\{}[],:|，\n' else c for c in body)
        if artist:
            rendered = rendered.replace('artist\\:', 'artist:', 1)
        if weight != 1:
            # A final numeric word would otherwise become an opener before
            # the closing :: on the next scan (e.g. weighted year_2025).
            rendered = re.sub(r'(?<!\S)([+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))$',
                              r'\\\1', rendered)
            number = format(weight, 'f')
            if '.' in number:
                number = number.rstrip('0').rstrip('.')
            rendered = f'{number}::{rendered}::'
        rendered = leading + rendered + trailing
        output.append(rendered)
        tokens.append(ConvertedToken(value, tag.raw_weight, weight, tag.span, rendered))
        while cursor < len(lexed.items) and lexed.items[cursor].span.start < tag.span.end:
            cursor += 1
    for item in lexed.items[cursor:]:
        if item.kind in (LexKind.TEXT, LexKind.SEP, LexKind.ESCAPED):
            output.append(str(item.value or item.raw).replace('_', ' '))
    rendered_text = ''.join(output)
    if dropped_empty:
        # Use lexer spans so escaped commas inside a tag remain intact.
        parts, start = [], 0
        for item in scan_nai(rendered_text, policy=ConversionPolicy(), storage=True).items:
            if item.kind == LexKind.SEP and item.raw in (',', '，'):
                parts.append(rendered_text[start:item.span.start].strip())
                start = item.span.end
        parts.append(rendered_text[start:].strip())
        rendered_text = ', '.join(part for part in parts if part)
    return ConversionResult(rendered_text, tuple(tokens), _sorted(warnings))
