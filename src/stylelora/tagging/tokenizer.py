import re
from .normalization import normalize_tag
from .types import Token

VERSION = 'comma-v1'


def syntax_of(value, *, known=False):
    if '\\' in value:
        return 'escaped'
    if re.search(r'\b(?:BREAK|AND)\b|\||::', value) or value.lower() in ('break','and'):
        return 'separator' if not re.match(r'-?\d+(?:\.\d+)?::', value) else 'weight'
    if re.search(r':\s*-?\d+(?:\.\d+)?\s*\)?$', value) or value.startswith(('(', '[', '{')):
        return 'weight'
    if any(c in value for c in '[]{}') or ('(' in value or ')' in value) and not known:
        return 'unknown'
    if any(ord(c) < 32 for c in value):
        return 'unknown'
    return 'plain'


def tokenize_prompt(text, *, catalog):
    result, offset = [], 0
    parts = text.split(',')
    for i, raw in enumerate(parts):
        value = raw.strip().lstrip('\ufeff') if i == 0 else raw.strip()
        hit = catalog.lookup(value)
        syntax = syntax_of(value, known=hit.match in ('known', 'alias'))
        if i and parts[i-1].endswith('\\'):
            syntax = 'escaped'
        weight = re.search(r'(?::|^)(-?\d+(?:\.\d+)?)(?:::|\)?$)', value)
        result.append(Token(raw, normalize_tag(value), syntax, syntax == 'escaped',
                            float(weight[1]) if weight else None,
                            hit.match if syntax == 'plain' else 'unverifiable', offset))
        offset += len(raw) + 1
    return tuple(result)
