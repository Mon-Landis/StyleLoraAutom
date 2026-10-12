from .tokenizer import tokenize_prompt, VERSION, syntax_of
from .types import ParsedPrompt, ParsedSegment
from .normalization import normalize_tag
from stylelora.project_db.types import Tag
from stylelora.project_db.errors import ValidationError
from stylelora.project_db.validation import tags as validate_tags


def parse_original_prompt(text, *, catalog):
    if not catalog.available:
        raise ValidationError('catalog:unavailable')
    tags, nl, segments, unverifiable = [], [], [], []
    seen = set()
    for token in tokenize_prompt(text, catalog=catalog):
        if not token.normalized:
            segments.append(ParsedSegment(token.raw, token, 'empty'))
        elif token.lookup in ('known', 'alias'):
            key = catalog.lookup(token.normalized).canonical
            if key not in seen:
                tags.append(Tag(key))
                seen.add(key)
            segments.append(ParsedSegment(token.raw, token, 'tag'))
        else:
            nl.append(token.raw.strip().lstrip('\ufeff'))
            reason = 'tokenizer:unsupported_syntax' if token.lookup == 'unverifiable' else 'tag:unknown'
            segments.append(ParsedSegment(token.raw, token, 'nl', reason))
            if token.lookup == 'unverifiable':
                unverifiable.append(token.raw)
    return ParsedPrompt(tuple(tags), ', '.join(nl) or None, tuple(segments), VERSION, tuple(unverifiable))


def normalize_structured(values, *, catalog=None):
    validate_tags(values)
    result, seen = [], set()
    for tag in values:
        key = normalize_tag(tag.tag)
        known = catalog is not None and catalog.lookup(key).match in ('known','alias')
        # Verified disambiguation is allowed; structured unknown plain tags survive.
        if ',' in key or syntax_of(key, known=known) != 'plain':
            raise ValidationError('tag:unverifiable')
        if key not in seen:
            seen.add(key)
            result.append(Tag(key, tag.conf))
    return tuple(result)
