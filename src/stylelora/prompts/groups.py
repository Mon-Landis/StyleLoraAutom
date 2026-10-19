"""F31 v5 immutable content contract and pure migration/rendering."""
from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from uuid import uuid4
from typing import Literal
import random
import unicodedata
from .types import Issue, PromptError, immutable, plain, FrozenSeeds
from .validation import require, identifier
from .prompt_normalize import normalize_prompt
from .dictionaries import digest
from stylelora.project_db.types import ItemSeed

CONTENT = {'nl', 'disable', 'aspect', 'variants'}
BASE_FIELDS = {'nl'}
BaseField = Literal['nl']


def _check(condition, path):
    if not condition:
        raise PromptError(issues=(Issue('schema:invalid', field=path),))


def _variant_key(key, path):
    _check(isinstance(key, str), path)
    value = key.strip()
    _check(1 <= len(value) <= 128 and not any(unicodedata.category(c) == 'Cc' for c in key), path)
    return value


def _variants(raw, path='variants', value_type=str, *, limit=64):
    _check(isinstance(raw, Mapping) and (limit is None or len(raw) <= limit), path)
    result = {}
    for key, value in raw.items():
        location = f'{path}[{key!r}]'
        key = _variant_key(key, location)
        _check(key not in result, location)
        _check(type(value) is value_type, location)
        if value_type is str:
            value = value.replace('\r\n', '\n').replace('\r', '\n')
            _check(len(value) <= 16000, location)
        elif value_type is int:
            _check(value >= 0, location)
        result[key] = value
    return immutable(result)


@dataclass(frozen=True)
class GeneratedFields:
    nl: bool = False
    variants: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        for key in BASE_FIELDS:
            _check(type(getattr(self, key)) is bool, 'generated.' + key)
        object.__setattr__(self, 'variants', _variants(self.variants, 'generated.variants', bool))


@dataclass(frozen=True)
class FieldVersions:
    nl: int = 0
    variants: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        for key in BASE_FIELDS:
            value = getattr(self, key)
            _check(type(value) is int and value >= 0, 'edit_versions.' + key)
        # Deleted keys retain counters; only live variants have the 64-key limit.
        object.__setattr__(self, 'variants', _variants(self.variants, 'edit_versions.variants', int, limit=None))


@dataclass(frozen=True)
class PendingField:
    state: str
    task_id: str
    attempt: int
    edit_version: int
    input_digest: str

    def __post_init__(self):
        _check(self.state == 'pending', 'pending.state')
        for key in ('task_id', 'input_digest'):
            _check(isinstance(getattr(self, key), str) and bool(getattr(self, key).strip()), 'pending.' + key)
        for key in ('attempt', 'edit_version'):
            value = getattr(self, key)
            _check(type(value) is int and value >= 0, 'pending.' + key)


def _state(cls, value, path):
    if isinstance(value, cls):
        return value
    _check(isinstance(value, Mapping) and set(value) <= set(cls.__dataclass_fields__), path)
    try:
        return cls(**value)
    except TypeError:
        _check(False, path)


@dataclass(frozen=True)
class GroupRef:
    scope: str
    project_token: str | None
    group_id: str

    def __post_init__(self):
        identifier(self.group_id)
        require(self.scope in ('global', 'project'))
        require(self.project_token is None if self.scope == 'global' else isinstance(self.project_token, str))

    @classmethod
    def parse(cls, value):
        require(isinstance(value, Mapping) and set(value) == {'scope', 'project_token', 'group_id'})
        return cls(**value)


@dataclass(frozen=True)
class CompletionState:
    state: str = 'complete'
    missing: tuple = ()
    task_id: str | None = None
    error_code: str | None = None

    def __post_init__(self):
        _check(self.state in ('pending', 'queued', 'running', 'complete', 'failed', 'paused', 'canceled'), 'completion.state')
        _check(isinstance(self.missing, (tuple, list)) and all(k == 'nl' for k in self.missing), 'completion.missing')
        object.__setattr__(self, 'missing', tuple(self.missing))


@dataclass(frozen=True)
class PreviewRef:
    asset_revision: str
    sha256: str
    width: int
    height: int
    mime: str = 'image/webp'

    def __post_init__(self):
        identifier(self.asset_revision)
        require(len(self.sha256) == 64 and all(c in '0123456789abcdef' for c in self.sha256))
        require(type(self.width) is int and type(self.height) is int and 0 < self.width <= 2048 and 0 < self.height <= 2048)
        require(self.mime == 'image/webp')


@dataclass(frozen=True)
class Entry:
    entry_id: str
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
        identifier(self.entry_id)
        _check(isinstance(self.nl, str) and bool(self.nl.strip()) and len(self.nl) <= 16000, 'nl')
        _check(type(self.disable) is bool, 'disable')
        _check(self.aspect in (None, 'landscape', 'portrait', 'square'), 'aspect')
        _check(self.preview is None or isinstance(self.preview, PreviewRef), 'preview')
        object.__setattr__(self, 'completion', _state(CompletionState, self.completion, 'completion'))
        object.__setattr__(self, 'variants', _variants(self.variants))
        object.__setattr__(self, 'generated', _state(GeneratedFields, self.generated, 'generated'))
        object.__setattr__(self, 'edit_versions', _state(FieldVersions, self.edit_versions, 'edit_versions'))
        _check(isinstance(self.pending, Mapping) and set(self.pending) <= BASE_FIELDS, 'pending')
        object.__setattr__(self, 'pending', immutable({k: _state(PendingField, v, 'pending.' + k) for k, v in self.pending.items()}))


EntryDraft = Entry


def legacy_dictionary(group):
    from .types import PromptDictionary, PromptEntry
    # rating remains unknown; group NSFW is never forged into a trusted rating.
    entries = tuple(PromptEntry((), e.nl, e.aspect, None)
                    for e in group.entries if not e.disable)
    return PromptDictionary(group.dictionary_id, group.name, 2, entries, group.revision, group.category)


def content(entry):
    return {key: plain(getattr(entry, key)) for key in sorted(CONTENT)}


def content_digest(entry):
    return digest(content(entry))


def validate_entry(raw: dict, *, allow_id: bool = True) -> Entry:
    """Validate content only; ingress callers normalize their submitted fields."""
    issues = []
    def issue(key):
        issues.append(Issue('schema:invalid', field=key))
    if not isinstance(raw, Mapping):
        raise PromptError(issues=(Issue('schema:invalid', field='entry'),))
    for key in set(raw) - CONTENT - ({'entry_id'} if allow_id else set()):
        issue(key)
    value = raw.get('nl')
    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
        issue('nl')
    if type(raw.get('disable', False)) is not bool:
        issue('disable')
    if raw.get('aspect') not in (None, 'landscape', 'portrait', 'square'):
        issue('aspect')
    identity = raw.get('entry_id', str(uuid4()))
    try:
        identifier(identity)
    except PromptError:
        issue('entry_id')
    if issues:
        raise PromptError(issues=issues)
    return Entry(identity, nl=value,
                 variants=raw.get('variants', {}),
                 disable=raw.get('disable', False), aspect=raw.get('aspect'))


@dataclass(frozen=True)
class Group:
    dictionary_id: str
    name: str
    scope: str = 'global'
    nsfw: bool = False
    entries: tuple = ()
    revision: int = 1
    schema_version: int = 5
    kind: str = 'prompts'
    category: str = 'full_content'
    _operations: Mapping = field(default_factory=dict, repr=False)
    auto_complete: bool = True
    prompt_format: Literal['nai'] = 'nai'
    normalization_version: Literal['nai-storage-v1'] = 'nai-storage-v1'

    def __post_init__(self):
        identifier(self.dictionary_id)
        require(isinstance(self.name, str) and 1 <= len(self.name.strip()) <= 128)
        require(self.scope in ('global', 'project') and type(self.nsfw) is bool)
        require(type(self.revision) is int and self.revision >= 1)
        require(type(self.schema_version) is int and self.schema_version == 5 and self.kind == 'prompts' and self.category == 'full_content')
        _check(self.prompt_format == 'nai', 'prompt_format')
        _check(self.normalization_version == 'nai-storage-v1', 'normalization_version')
        _check(type(self.auto_complete) is bool, 'auto_complete')
        _check(isinstance(self.entries, (tuple, list)), 'entries')
        _check(isinstance(self._operations, Mapping), '_operations')
        require(all(isinstance(e, Entry) for e in self.entries))
        require(len({e.entry_id for e in self.entries}) == len(self.entries), 'entry:id_conflict')
        object.__setattr__(self, 'name', self.name.strip())
        object.__setattr__(self, 'entries', tuple(self.entries))
        object.__setattr__(self, '_operations', immutable(self._operations))


def decode_group(raw: dict) -> Group:
    require(isinstance(raw, dict))
    require(type(raw.get('schema_version')) is int and raw['schema_version'] == 5, 'newer_version')
    require(set(raw) <= set(Group.__dataclass_fields__))
    for key in ('dictionary_id', 'name'):
        _check(key in raw, key)
    _check(isinstance(raw.get('entries', []), (list, tuple)), 'entries')
    entries = []
    for row in raw.get('entries', []):
        require(isinstance(row, dict) and set(row) <= set(Entry.__dataclass_fields__))
        _check('entry_id' in row, 'entry_id')
        entry = validate_entry({k: v for k, v in row.items() if k in CONTENT | {'entry_id'}})
        preview = _state(PreviewRef, row['preview'], 'preview') if row.get('preview') is not None else None
        completion = _state(CompletionState, row.get('completion', {}), 'completion')
        entries.append(replace(entry, preview=preview, completion=completion,
                               **{k: row[k] for k in ('generated', 'pending', 'edit_versions') if k in row}))
    return Group(**(raw | {'entries': tuple(entries)}))


def migrate_v2(raw):
    from .group_legacy import legacy_version
    require(legacy_version(raw) in (1, 2))
    return migrate_group(raw)


def migrate_v3(raw, *, auto_complete_default=True):
    from .group_legacy import legacy_version
    require(legacy_version(raw) == 3)
    return migrate_group(raw, auto_complete_default=auto_complete_default)


def migrate_v4(raw: dict) -> Group:
    from .group_legacy import legacy_version
    require(legacy_version(raw) == 4)
    return migrate_group(raw)


def migrate_group(raw: dict, *, auto_complete_default: bool = True) -> Group:
    from .group_legacy import legacy_version, adapt_legacy_group
    _check(type(auto_complete_default) is bool, 'auto_complete')
    if legacy_version(raw) == 5:
        return decode_group(raw)
    return decode_group(adapt_legacy_group(raw, auto_complete_default=auto_complete_default))


@dataclass(frozen=True)
class RenderResult:
    text: str
    requested_mode: str
    effective_mode: str
    warnings: tuple = ()


def render(entry, mode='nl'):
    require(mode in ('nl', 'tags_plus_nl', 'full_nl'))
    return RenderResult(entry.nl, mode, 'nl', () if mode == 'nl' else (
        Issue('render:deprecated_mode', field='mode', severity='warning'),))


def freeze_groups(groups, mode, seed, count, default_aspect=None):
    require(type(count) is int and count >= 0 and type(seed) is int)
    require(mode in ('nl', 'tags_plus_nl', 'full_nl'))
    candidates, seen, warnings = [], set(), []
    for group in groups:
        for entry in group.entries:
            if entry.disable:
                continue
            result = render(entry, mode)
            if not result.text:
                continue
            key = normalize_prompt(result.text, 'nai').text
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
