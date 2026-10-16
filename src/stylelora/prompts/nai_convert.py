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
    ordinary_underscores: Literal['preserve', 'space'] = 'preserve'
    max_weight: Decimal = Decimal('3.2')
    decimal_places: int = 2
    max_depth: int = 128
    max_chars: int = 1000000
    max_numeric_chars: int = 64

    def __post_init__(self):
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
_BRACKETS = {'{': LexKind.OPEN_UP, '}': LexKind.CLOSE_UP,
             '[': LexKind.OPEN_DOWN, ']': LexKind.CLOSE_DOWN}


def scan_nai(text: str, *, policy: ConversionPolicy) -> LexResult:
    _policy(policy)
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if len(text) > policy.max_chars:
        return LexResult((), (_warning(WarningCode.W_INPUT_LIMIT,
                                      SourceSpan(policy.max_chars, len(text))),))
    items, warnings = [], []
    i = 1 if text.startswith('\ufeff') else 0
    pending = i
    active, eligible = False, True

    def flush(end):
        if pending < end:
            raw = text[pending:end]
            items.append(LexToken(LexKind.TEXT, raw, SourceSpan(pending, end), raw))

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
            if active:
                active, eligible = False, True
            else:
                warnings.append(_warning(WarningCode.W_UNKNOWN_DCOLON, SourceSpan(i, end)))
                eligible = False
        elif not active and eligible and not c.isspace():
            match = _CANDIDATE.match(text, i)
            candidate = match[0][:-2] if match else ''
            if candidate and (candidate[0] in '+-.0123456789'
                              or candidate.lower() in ('nan', 'infinity')):
                end = match.end()
                if len(candidate) <= policy.max_numeric_chars and _NUMBER.fullmatch(candidate):
                    kind, value = LexKind.NUM_OPEN, Decimal(candidate)
                    active, eligible = True, True
                else:
                    kind, value = LexKind.INVALID_NUM, match[0]
                    warnings.append(_warning(WarningCode.W_INVALID_NUMBER, SourceSpan(i, end)))
                    eligible = False
            else:
                eligible = False
        if kind is None:
            if c in _BRACKETS:
                kind = _BRACKETS[c]
                eligible = c in '{['
            elif c == '|':
                kind, value = LexKind.PIPE, c
                warnings.append(_warning(WarningCode.W_UNSUPPORTED_ALTERNATION, SourceSpan(i, end)))
                eligible = False
            elif not c.isspace():
                eligible = False
        if kind is not None:
            flush(i)
            items.append(LexToken(kind, text[i:end], SourceSpan(i, end), value))
            pending = end
        i = end
    flush(i)
    items.append(LexToken(LexKind.EOF, '', SourceSpan(i, i), None))
    return LexResult(tuple(items), _sorted(warnings))


def parse_nai(items: tuple[LexToken, ...], *, policy: ConversionPolicy) -> ParseResult:
    _policy(policy)
    result, warnings, stack, buffer = [], [], [], []
    start = end = 0
    balance, numeric = 0, None
    powers = {}

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
                numeric = (item.value, item.span, len(result))
            elif kind == LexKind.DCOLON and numeric:
                emit()
                empty(numeric[1], numeric[2])
                numeric = None
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
    items = []
    with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
        for tag in tags:
            if tag.raw_weight <= 0:
                continue
            weight = min(tag.raw_weight, policy.max_weight).quantize(Decimal('.01'))
            if weight > 0:
                items.append(ScaledTag(tag.value, tag.raw_weight, weight, tag.span))
    return CompressionResult(tuple(items), ())


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
    return ConversionResult(', '.join(t.rendered for t in tokens), tokens, warnings)
