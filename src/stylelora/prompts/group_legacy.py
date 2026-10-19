"""Pure v1–v4 adapters. Persistence, backups and receipts belong to T5."""
from collections.abc import Mapping
from uuid import UUID, uuid5

from .prompt_normalize import normalize_prompt
from .types import Issue, PromptError
from .validation import require, identifier

OLD_FIELDS = ('tag', 'expand_nl', 'nl')
# tag/expand_nl become variants; only the old nl becomes the v5 base prompt.
VARIANT_SOURCES = ('tag', 'expand_nl')
# Fallback order for a missing legacy nl: expand_nl first, then tag.
NL_FALLBACK_ORDER = ('expand_nl', 'tag')
VARIANT_LIMIT = 64


def nl_fallback(texts: Mapping) -> str | None:
    """Source key that fills an empty nl (``expand_nl`` before ``tag``), else None."""
    if texts.get('nl', '').strip():
        return None
    return next((source for source in NL_FALLBACK_ORDER if texts.get(source, '').strip()), None)


def _diagnostic(code, identity, message):
    return Issue(code, field=identity, severity='warning', message=message)


def _check(condition, path):
    if not condition:
        raise PromptError(issues=(Issue('schema:invalid', field=path),))


def legacy_version(raw: dict) -> int:
    require(isinstance(raw, dict))
    version = raw.get('schema_version')
    require(type(version) is int and version in (1, 2, 3, 4, 5), 'newer_version')
    return version


def legacy_texts(raw: Mapping) -> dict:
    """Validated legacy tag/expand_nl/nl strings under their original keys."""
    tags = raw.get('tag', ())
    _check(isinstance(tags, (list, tuple)) and all(isinstance(t, str) for t in tags), 'tag')
    values = dict(zip(OLD_FIELDS, (', '.join(tags), raw.get('expand_nl', ''), raw.get('nl', ''))))
    for key, value in values.items():
        _check(isinstance(value, str), key)
    return values


def _variant_sources(raw: Mapping) -> dict:
    """Non-empty tag/expand_nl keyed by their original names."""
    return {source: value for source, value in legacy_texts(raw).items()
            if source in VARIANT_SOURCES and value.strip()}


def _repeat_issue(raw: Mapping) -> tuple[Issue, ...]:
    values = [value for value in legacy_texts(raw).values() if value.strip()]
    return (Issue('migration:possible_duplicate', field=raw.get('entry_id'), severity='warning'),) if len(set(values)) < len(values) else ()


def merge_legacy_content(raw: Mapping) -> str:
    """v5 base text: only the legacy nl; tag/expand_nl never merge into it."""
    return legacy_texts(raw)['nl']


def legacy_content_issues(raw: Mapping) -> tuple[Issue, ...]:
    """A migration/preview report can warn without removing repeated segments."""
    return _repeat_issue(raw)


def variant_issues(raw: Mapping) -> tuple[Issue, ...]:
    """Report legacy variant sources that cannot be added (conflict or 64-key limit)."""
    identity = raw.get('entry_id')
    existing = raw.get('variants', {})
    _check(isinstance(existing, Mapping), 'variants')
    issues, count = [], len(existing)
    for source in _variant_sources(raw):
        if source in existing:
            issues.append(_diagnostic('migration:variant_conflict', identity,
                f'entry_id={identity}; source={source}; existing variant kept'))
        elif count >= VARIANT_LIMIT:
            issues.append(_diagnostic('migration:variant_limit', identity,
                f'entry_id={identity}; source={source}; limit={VARIANT_LIMIT}'))
        else:
            count += 1
    return tuple(issues)


def _normalize(text, identity, field):
    if not text.strip():
        return text.replace('\r\n', '\n').replace('\r', '\n')
    try:
        value = normalize_prompt(text, 'anima').text
    except PromptError as exc:
        raise PromptError(exc.code, issues=(Issue(exc.code, field=field,
            message=f'entry_id={identity}; source_length={len(text)}'),)) from None
    if len(value) > 16000:
        raise PromptError(issues=(Issue('schema:invalid', field=field,
            message=f'entry_id={identity}; length={len(value)}; limit=16000'),))
    return value


def adapt_legacy_entry(raw: Mapping, *, issues: list | None = None) -> dict:
    """Convert one legacy row; when ``issues`` is given, append diagnostics."""
    _check(isinstance(raw, Mapping), 'entry')
    allowed = {'entry_id', *OLD_FIELDS, 'disable', 'aspect', 'preview', 'completion',
               'variants', 'generated', 'pending', 'edit_versions'}
    # completion is accepted on input but dropped: v5 no longer stores it.
    _check(set(raw) <= allowed, 'entry')
    identity = raw.get('entry_id')
    identifier(identity)
    texts = legacy_texts(raw)
    generated = raw.get('generated', {})
    versions = raw.get('edit_versions', {})
    for state, path, cls in ((generated, 'generated', bool), (versions, 'edit_versions', int)):
        _check(isinstance(state, Mapping) and set(state) <= {*OLD_FIELDS, 'variants'}, path)
        _check(isinstance(state.get('variants', {}), Mapping), path + '.variants')
        for key in OLD_FIELDS:
            value = state.get(key, False if cls is bool else 0)
            _check(type(value) is cls and (cls is bool or value >= 0), path + '.' + key)
    variants = raw.get('variants', {})
    _check(isinstance(variants, Mapping), 'variants')
    converted = {}
    for key, text in variants.items():
        _check(isinstance(text, str), f'variants[{key!r}]')
        converted[key] = _normalize(text, identity, f'variants[{key!r}]')

    # v5 requires a non-empty nl: fall back to expand_nl, then tag.
    # ``source`` keeps the field name; ``base`` keeps that field's text.
    source = nl_fallback(texts)
    base = texts[source] if source is not None else texts['nl']
    _check(bool(base.strip()), 'nl')

    notes = list(variant_issues(raw))
    mapped = set()
    for variant_source, text in _variant_sources(raw).items():
        if variant_source in converted:
            continue
        if len(converted) >= VARIANT_LIMIT:
            break
        converted[variant_source] = _normalize(text, identity, f'variants[{variant_source!r}]')
        mapped.add(variant_source)
    if source is not None:
        notes.append(_diagnostic('migration:nl_fallback', identity,
            f'entry_id={identity}; source={source}; nl filled from {source}'))

    # tag/expand_nl generation marks follow the variant they produced; nl only from old nl.
    generated_variants = dict(generated.get('variants', {}))
    for variant_source in mapped:
        generated_variants[variant_source] = generated.get(variant_source, False)
    if issues is not None:
        issues.extend(notes)
    return {k: raw[k] for k in ('entry_id', 'disable', 'aspect', 'preview') if k in raw} | {
        'nl': _normalize(base, identity, 'nl'), 'variants': converted,
        'generated': {'nl': bool(generated.get(source or 'nl', False)),
                      'variants': generated_variants},
        'edit_versions': {'nl': max(versions.get(k, 0) for k in OLD_FIELDS) + 1,
                          'variants': dict(versions.get('variants', {}))},
        'pending': {}}


def adapt_legacy_group(raw: dict, *, issues: list | None = None) -> dict:
    version = legacy_version(raw)
    require(version < 5)
    require(raw.get('kind', 'prompts') == 'prompts')
    rows = raw.get('entries')
    _check(isinstance(rows, (list, tuple)), 'entries')
    identity = raw.get('dictionary_id')
    identifier(identity)
    revision = raw.get('revision')
    _check(type(revision) is int and revision >= 1, 'revision')
    if version in (1, 2):
        adapted = []
        for index, row in enumerate(rows):
            _check(isinstance(row, Mapping) and set(row) <= {
                'entry_id', 'tags', 'natural_language', 'aspect', 'rating'}, 'entry')
            _check(row.get('rating') in (None, 'general', 'sensitive', 'questionable', 'explicit'), 'rating')
            natural_language = row.get('natural_language')
            _check(natural_language is None or isinstance(natural_language, str), 'natural_language')
            adapted.append({'entry_id': row.get('entry_id', str(uuid5(UUID(identity), str(index)))),
                            'tag': row.get('tags', []), 'expand_nl': natural_language if natural_language is not None else '',
                            # v1/v2 natural_language is the only base text: it is the old nl.
                            'nl': natural_language if natural_language is not None else '',
                            'aspect': row.get('aspect')})
        nsfw = raw.get('nsfw', False) or any(r.get('rating') in ('questionable', 'explicit') for r in rows)
        rows = adapted
    else:
        nsfw = raw.get('nsfw', False)
    result = raw | {'schema_version': 5, 'revision': revision + 1,
                    'scope': raw.get('scope', 'global'), 'nsfw': nsfw,
                    'prompt_format': 'nai', 'normalization_version': 'nai-storage-v1',
                    'entries': [adapt_legacy_entry(row, issues=issues) for row in rows]}
    if issues is not None:
        # Keeping the migration receipt's duplicate report independent of variant mapping.
        issues.extend(issue for row in rows for issue in _repeat_issue(row))
    # Keep only keys the current v5 envelope still declares.
    from .groups import Group
    return {key: value for key, value in result.items()
            if key in Group.__dataclass_fields__}
