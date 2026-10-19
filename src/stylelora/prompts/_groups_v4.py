"""Temporary F30 contract for consumers awaiting F31 T5/T6/T7/T11.

This preserves the pre-T4 behavior; new code must use groups (strict v5).
Remove this module when the storage/API/exchange/task cutover is complete.
"""
from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from uuid import UUID, uuid4, uuid5
from typing import Literal
import random
from .types import Issue, PromptError, immutable, plain, FrozenSeeds
from .validation import require, identifier, normalize_tag
from .dictionaries import digest
from .groups import GroupRef, PreviewRef, PendingField, _check, _variant_key, _variants, _state
from stylelora.project_db.types import ItemSeed

CONTENT = {'tag', 'expand_nl', 'nl', 'disable', 'aspect', 'variants'}
BASE_FIELDS = {'tag', 'expand_nl', 'nl'}
BaseField = Literal['tag', 'expand_nl', 'nl']


@dataclass(frozen=True)
class GeneratedFields:
    tag: bool = False
    expand_nl: bool = False
    nl: bool = False
    variants: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        for key in BASE_FIELDS:
            _check(type(getattr(self, key)) is bool, 'generated.' + key)
        object.__setattr__(self, 'variants', _variants(self.variants, 'generated.variants', bool))


@dataclass(frozen=True)
class FieldVersions:
    tag: int = 0
    expand_nl: int = 0
    nl: int = 0
    variants: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        for key in BASE_FIELDS:
            value = getattr(self, key)
            _check(type(value) is int and value >= 0, 'edit_versions.' + key)
        object.__setattr__(self, 'variants', _variants(self.variants, 'edit_versions.variants', int, limit=None))


@dataclass(frozen=True)
class CompletionState:
    state: str = 'complete'
    missing: tuple = ()
    task_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class Entry:
    entry_id: str
    tag: tuple = ()
    expand_nl: str = ''
    nl: str = ''
    disable: bool = False
    aspect: str | None = None
    preview: PreviewRef | None = None
    completion: CompletionState = field(default_factory=CompletionState)
    variants: Mapping[str, str] = field(default_factory=dict)
    generated: GeneratedFields = field(default_factory=GeneratedFields)
    pending: Mapping[BaseField, PendingField] = field(default_factory=dict)
    edit_versions: FieldVersions = field(default_factory=FieldVersions)

    def __post_init__(self):
        object.__setattr__(self, 'variants', _variants(self.variants))
        object.__setattr__(self, 'generated', _state(GeneratedFields, self.generated, 'generated'))
        object.__setattr__(self, 'edit_versions', _state(FieldVersions, self.edit_versions, 'edit_versions'))
        _check(isinstance(self.pending, Mapping) and set(self.pending) <= BASE_FIELDS, 'pending')
        object.__setattr__(self, 'pending', immutable({k: _state(PendingField, v, 'pending.' + k) for k, v in self.pending.items()}))


EntryDraft = Entry


def legacy_dictionary(group):
    from .types import PromptDictionary, PromptEntry
    entries = tuple(PromptEntry(e.tag, e.expand_nl or (e.nl if not e.tag else None), e.aspect, None)
                    for e in group.entries if not e.disable)
    return PromptDictionary(group.dictionary_id, group.name, 2, entries, group.revision, group.category)


def content(entry):
    return {key: plain(getattr(entry, key)) for key in sorted(CONTENT)}


def content_digest(entry):
    return digest(content(entry))


def validate_entry(raw, *, catalog=None, allow_id=True):
    issues = []
    def issue(key):
        issues.append(Issue('schema:invalid', field=key))
    if not isinstance(raw, Mapping):
        raise PromptError(issues=(Issue('schema:invalid', field='entry'),))
    for key in set(raw) - CONTENT - ({'entry_id'} if allow_id else set()):
        issue(key)
    tags = raw.get('tag', [])
    if not isinstance(tags, (list, tuple)) or len(tags) > 256:
        issue('tag'); tags = []
    if catalog is None:
        from .validation import LocalCatalog
        catalog = LocalCatalog()
    normalized = []
    for tag in tags:
        if not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 256:
            issue('tag'); continue
        normalized.append(normalize_tag(tag.strip(), catalog))
    values = {}
    for key in ('expand_nl', 'nl'):
        value = raw.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > 16000:
            issue(key); value = ''
        values[key] = value.strip()
    if type(raw.get('disable', False)) is not bool:
        issue('disable')
    if raw.get('aspect') not in (None, 'landscape', 'portrait', 'square'):
        issue('aspect')
    if not normalized and not any(values.values()):
        issue('content')
    identity = raw.get('entry_id', str(uuid4()))
    try:
        identifier(identity)
    except PromptError:
        issue('entry_id')
    if issues:
        raise PromptError(issues=issues)
    if not normalized and values['expand_nl'] and not values['nl']:
        values['nl'] = values['expand_nl']
    missing = tuple(k for k in ('tag', 'expand_nl', 'nl') if not (normalized if k == 'tag' else values[k]))
    return Entry(identity, tuple(dict.fromkeys(normalized)), **values,
                 variants=raw.get('variants', {}),
                 disable=raw.get('disable', False), aspect=raw.get('aspect'),
                 completion=CompletionState('pending' if missing else 'complete', missing))


@dataclass(frozen=True)
class Group:
    dictionary_id: str
    name: str
    scope: str = 'global'
    nsfw: bool = False
    entries: tuple = ()
    revision: int = 1
    schema_version: int = 4
    kind: str = 'prompts'
    category: str = 'full_content'
    _operations: Mapping = field(default_factory=dict, repr=False)
    auto_complete: bool = True

    def __post_init__(self):
        identifier(self.dictionary_id)
        require(isinstance(self.name, str) and 1 <= len(self.name.strip()) <= 128)
        require(self.scope in ('global', 'project') and type(self.nsfw) is bool)
        require(type(self.revision) is int and self.revision >= 1)
        require(type(self.schema_version) is int and self.schema_version == 4 and self.kind == 'prompts' and self.category == 'full_content')
        _check(type(self.auto_complete) is bool, 'auto_complete')
        require(all(isinstance(e, Entry) for e in self.entries))
        require(len({e.entry_id for e in self.entries}) == len(self.entries), 'entry:id_conflict')
        object.__setattr__(self, 'name', self.name.strip())
        object.__setattr__(self, 'entries', tuple(self.entries))
        object.__setattr__(self, '_operations', immutable(self._operations))


def decode_group(raw):
    require(isinstance(raw, dict))
    require(set(raw) <= set(Group.__dataclass_fields__))
    require(type(raw.get('schema_version')) is int and raw['schema_version'] == 4, 'newer_version')
    entries = []
    for row in raw.get('entries', []):
        require(isinstance(row, dict) and set(row) <= set(Entry.__dataclass_fields__))
        entry = validate_entry({k: v for k, v in row.items() if k in CONTENT | {'entry_id'}})
        preview = PreviewRef(**row['preview']) if row.get('preview') else None
        state = row.get('completion')
        completion = CompletionState(**(state | {'missing': tuple(state['missing'])})) if state else entry.completion
        require(completion.state in ('pending', 'queued', 'running', 'complete', 'failed', 'paused', 'canceled'))
        require(set(completion.missing) <= BASE_FIELDS)
        entries.append(replace(entry, preview=preview, completion=completion,
                               **{k: row[k] for k in ('generated', 'pending', 'edit_versions') if k in row}))
    return Group(**(raw | {'entries': tuple(entries)}))


def migrate_v2(raw):
    from .dictionaries import _decode
    old = _decode(raw)
    require(raw['kind'] == 'prompts')
    entries = []
    for index, row in enumerate(old.entries):
        entry = validate_entry(dict(entry_id=str(uuid5(UUID(old.dictionary_id), str(index))),
                                    tag=list(row.tags), expand_nl=row.natural_language or '', aspect=row.aspect))
        entries.append(entry)
    return Group(old.dictionary_id, old.name, nsfw=any(e.rating in ('questionable', 'explicit') for e in old.entries),
                 entries=tuple(entries), revision=old.revision + 1, _operations=raw.get('_operations', {}))


def migrate_v3(raw, *, auto_complete_default=True):
    require(raw.get('schema_version') == 3)
    _check(type(auto_complete_default) is bool, 'auto_complete')
    return decode_group(raw | {'schema_version': 4, 'revision': raw['revision'] + 1,
                               'auto_complete': auto_complete_default})


def migrate_group(raw, *, auto_complete_default=True):
    require(isinstance(raw, dict))
    _check(type(auto_complete_default) is bool, 'auto_complete')
    version = raw.get('schema_version')
    require(type(version) is int and version in (1, 2, 3, 4), 'newer_version')
    if version == 4:
        return decode_group(raw)
    if version == 3:
        return migrate_v3(raw, auto_complete_default=auto_complete_default)
    return replace(migrate_v2(raw), auto_complete=auto_complete_default)


@dataclass(frozen=True)
class RenderResult:
    text: str
    requested_mode: str
    effective_mode: str
    warnings: tuple = ()


def render(entry, mode):
    require(mode in ('tags_plus_nl', 'full_nl'))
    tags = ', '.join(entry.tag)
    combined = ' '.join(x for x in (tags, entry.expand_nl) if x)
    primary, fallback = (combined, entry.nl) if mode == 'tags_plus_nl' else (entry.nl, combined)
    effective = mode if primary else ('full_nl' if mode == 'tags_plus_nl' else 'tags_plus_nl')
    return RenderResult(primary or fallback, mode, effective,
                        () if primary else (Issue('render:fallback', severity='warning'),))


def freeze_groups(groups, mode, seed, count, default_aspect=None):
    require(type(count) is int and count >= 0 and type(seed) is int)
    candidates, seen, warnings = [], set(), []
    for group in groups:
        for entry in group.entries:
            if entry.disable:
                continue
            result = render(entry, mode)
            if not result.text:
                continue
            key = ('tag', tuple(sorted(entry.tag))) if entry.tag else ('nl', ' '.join(result.text.split()))
            if key in seen:
                continue
            seen.add(key); candidates.append((entry, result))
    if len(candidates) < count:
        raise PromptError('source:insufficient', issues=(Issue('source:insufficient', message=f'available={len(candidates)}; deficit={count-len(candidates)}'),))
    random.Random(seed).shuffle(candidates)
    items = []
    for entry, result in candidates[:count]:
        items.append(ItemSeed(seq=len(items)+1, prompt=result.text, aspect=entry.aspect or default_aspect))
        warnings.extend(result.warnings)
    return FrozenSeeds(digest(items), tuple(items), len(items)), tuple(warnings)
