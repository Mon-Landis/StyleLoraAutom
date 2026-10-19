"""F31 pure storage boundary. Callers map PromptError codes to HTTP/row errors.

Empty variants are handled by callers; every value passed here must contain a
prompt. Source selection is explicit and never inferred from the text.
"""
from decimal import Context, Decimal, DecimalException, ROUND_HALF_UP, localcontext
from typing import Literal

from .anima_convert import convert_anima_to_nai, scan_anima
from .nai_convert import (
    ConversionPolicy, ConversionResult, LexKind, WarningCode,
    _render_storage, _sorted, convert_nai_to_anima, parse_nai, scan_nai,
)
from .types import PromptError

PromptFormat = Literal['nai', 'anima']
NORMALIZATION_VERSION = 'nai-storage-v1'
_POLICY = ConversionPolicy()
_LIMITS = {WarningCode.W_INPUT_LIMIT, WarningCode.W_DEPTH_LIMIT,
           WarningCode.W_NUMERIC_LIMIT}
_LOSSES = {WarningCode.W_NORMALIZATION_ROUNDED, WarningCode.W_WEIGHT_CLAMPED}


def _check_warnings(warnings):
    if any(w.code in _LIMITS for w in warnings):
        raise PromptError('import:limit')
    if any(w.code not in _LOSSES for w in warnings):
        raise PromptError('normalization:syntax')


def _structure(items):
    """Reject crossing/unclosed blocks before the recovery-oriented F28 parser."""
    stack = []
    pairs = {LexKind.CLOSE_UP: LexKind.OPEN_UP,
             LexKind.CLOSE_DOWN: LexKind.OPEN_DOWN,
             LexKind.DCOLON: LexKind.NUM_OPEN}
    for item in items:
        if item.kind in (LexKind.OPEN_UP, LexKind.OPEN_DOWN, LexKind.NUM_OPEN):
            if len(stack) >= _POLICY.max_depth:
                raise PromptError('import:limit')
            stack.append(item.kind)
        elif item.kind in pairs:
            if not stack or stack.pop() != pairs[item.kind]:
                raise PromptError('normalization:syntax')
    if stack:
        raise PromptError('normalization:syntax')


def _validate(text, dialect):
    if dialect not in ('nai', 'anima') or not isinstance(text, str):
        raise PromptError('schema:invalid')
    if len(text) > _POLICY.max_chars:
        raise PromptError('import:limit')
    if not text.strip():
        raise PromptError('normalization:empty')


def normalize_prompt(text: str, source: PromptFormat) -> ConversionResult:
    """Return canonical NAI; raise PromptError for invalid/empty/limited input.

    Precision loss is returned as ``normalization:rounded``. Limits use the
    existing ConversionPolicy defaults (characters, nesting, numeric length).
    """
    _validate(text, source)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    try:
        with localcontext(Context(prec=128, rounding=ROUND_HALF_UP)):
            scanner = scan_nai if source == 'nai' else scan_anima
            lexed = scanner(text, policy=_POLICY, storage=True)
            _check_warnings(lexed.warnings)
            _structure(lexed.items)
            if source == 'anima':
                result = convert_anima_to_nai(text, body_spaces=True)
            else:
                parsed = parse_nai(lexed.items, policy=_POLICY, storage=True)
                _check_warnings(parsed.warnings)
                result = _render_storage(
                    text, lexed, parsed.items,
                    lambda weight, artist: weight.quantize(Decimal('.00000001')))
            _check_warnings(result.warnings)
            _validate(result.text, 'nai')
            if not result.tokens:
                raise PromptError('normalization:empty')
            # Multiplication may exceed the numeric limit even with valid factors.
            check = scan_nai(result.text, policy=_POLICY, storage=True)
            _check_warnings(check.warnings)
            _structure(check.items)
            reparsed = parse_nai(check.items, policy=_POLICY, storage=True)
            _check_warnings(reparsed.warnings)
            if not reparsed.items:
                raise PromptError('normalization:syntax')
            return result
    except DecimalException as exc:
        raise PromptError('import:limit') from exc
    except PromptError:
        raise
    except Exception as exc:
        raise PromptError('import:conversion_failed') from exc


def render_for_endpoint(nl: str, target: PromptFormat) -> ConversionResult:
    """Render stored NAI without mutating it; Anima applies the F28 output rules."""
    _validate(nl, target)
    canonical = normalize_prompt(nl, 'nai')
    if target == 'nai':
        return canonical
    try:
        result = convert_nai_to_anima(canonical.text)
        _check_warnings(result.warnings)
        if not result.text.strip():
            raise PromptError('normalization:empty')
        return ConversionResult(result.text, result.tokens,
                                _sorted(canonical.warnings + result.warnings))
    except PromptError:
        raise
    except Exception as exc:
        raise PromptError('import:conversion_failed') from exc
