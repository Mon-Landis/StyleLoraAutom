"""Pure v1–v4 adapters. Persistence, backups and receipts belong to T5."""
from collections.abc import Mapping
from uuid import UUID, uuid5

from .prompt_normalize import normalize_prompt
from .types import Issue, PromptError
from .validation import require, identifier

OLD_FIELDS = ('tag', 'expand_nl', 'nl')


def legacy_version(raw: dict) -> int:
    require(isinstance(raw, dict))
    version = raw.get('schema_version')
    require(type(version) is int and version in (1, 2, 3, 4, 5), 'newer_version')
    return version


def _check(condition, path):
    if not condition:
        raise PromptError(issues=(Issue('schema:invalid', field=path),))


def _normalize(text, identity, field):
    try:
        value = normalize_prompt(text, 'anima').text if text.strip() else text.replace('\r\n', '\n').replace('\r', '\n')
    except PromptError as exc:
        raise PromptError(exc.code, issues=(Issue(exc.code, field=field,
            message=f'entry_id={identity}; source_length={len(text)}'),)) from None
    if len(value) > 16000:
        raise PromptError(issues=(Issue('schema:invalid', field=field,
            message=f'entry_id={identity}; length={len(value)}; limit=16000'),))
    return value


def merge_legacy_content(raw: Mapping) -> str:
    """Preserve source ordering, whitespace and duplicates before conversion."""
    tags = raw.get('tag', ())
    _check(isinstance(tags, (list, tuple)) and all(isinstance(t, str) for t in tags), 'tag')
    values = [', '.join(tags), raw.get('expand_nl', ''), raw.get('nl', '')]
    for key, value in zip(OLD_FIELDS, values):
        _check(isinstance(value, str), key)
    return '\n\n'.join(value for value in values if value.strip())


def legacy_content_issues(raw: Mapping) -> tuple[Issue, ...]:
    """A migration/preview report can warn without removing repeated segments."""
    merge_legacy_content(raw)
    parts = [', '.join(raw.get('tag', ())), raw.get('expand_nl', ''), raw.get('nl', '')]
    parts = [p for p in parts if p.strip()]
    return (Issue('migration:possible_duplicate', field=raw.get('entry_id'), severity='warning'),) if len(set(parts)) < len(parts) else ()


def adapt_legacy_entry(raw: Mapping) -> dict:
    _check(isinstance(raw, Mapping), 'entry')
    allowed = {'entry_id', *OLD_FIELDS, 'disable', 'aspect', 'preview', 'completion',
               'variants', 'generated', 'pending', 'edit_versions'}
    _check(set(raw) <= allowed, 'entry')
    identity = raw.get('entry_id')
    identifier(identity)
    merged = merge_legacy_content(raw)
    _check(bool(merged.strip()), 'nl')
    generated = raw.get('generated', {})
    versions = raw.get('edit_versions', {})
    for state, path, cls in ((generated, 'generated', bool), (versions, 'edit_versions', int)):
        _check(isinstance(state, Mapping) and set(state) <= {*OLD_FIELDS, 'variants'}, path)
        _check(isinstance(state.get('variants', {}), Mapping), path + '.variants')
        for key in OLD_FIELDS:
            value = state.get(key, False if cls is bool else 0)
            _check(type(value) is cls and (cls is bool or value >= 0), path + '.' + key)
    parts = dict(zip(OLD_FIELDS, (', '.join(raw.get('tag', ())), raw.get('expand_nl', ''), raw.get('nl', ''))))
    variants = raw.get('variants', {})
    _check(isinstance(variants, Mapping), 'variants')
    converted = {}
    for key, text in variants.items():
        _check(isinstance(text, str), f'variants[{key!r}]')
        converted[key] = _normalize(text, identity, f'variants[{key!r}]')
    return {k: raw[k] for k in ('entry_id', 'disable', 'aspect', 'preview') if k in raw} | {
        'nl': _normalize(merged, identity, 'nl'), 'variants': converted,
        'generated': {'nl': all(generated.get(k, False) for k, v in parts.items() if v.strip()),
                      'variants': dict(generated.get('variants', {}))},
        'edit_versions': {'nl': max(versions.get(k, 0) for k in OLD_FIELDS) + 1,
                          'variants': dict(versions.get('variants', {}))},
        'pending': {}, 'completion': {'state': 'complete', 'missing': ()}}


def adapt_legacy_group(raw: dict, *, auto_complete_default: bool = True) -> dict:
    version = legacy_version(raw)
    require(version < 5)
    _check(type(auto_complete_default) is bool, 'auto_complete')
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
                            'aspect': row.get('aspect')})
        nsfw = raw.get('nsfw', False) or any(r.get('rating') in ('questionable', 'explicit') for r in rows)
        rows = adapted
    else:
        nsfw = raw.get('nsfw', False)
    result = raw | {'schema_version': 5, 'revision': revision + 1,
                    'scope': raw.get('scope', 'global'), 'nsfw': nsfw,
                    'auto_complete': raw.get('auto_complete', auto_complete_default),
                    'prompt_format': 'nai', 'normalization_version': 'nai-storage-v1',
                    'entries': [adapt_legacy_entry(row) for row in rows]}
    return result
