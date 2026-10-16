"""F27 immutable content contract, migration and rendering (no display filtering)."""
from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from uuid import UUID, uuid4, uuid5
import random
from .types import Issue, PromptError, immutable, plain, FrozenSeeds
from .validation import require, identifier, normalize_tag
from .dictionaries import digest
from stylelora.project_db.types import ItemSeed

CONTENT = {'tag', 'expand_nl', 'nl', 'disable', 'aspect'}


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
    tag: tuple = ()
    expand_nl: str = ''
    nl: str = ''
    disable: bool = False
    aspect: str | None = None
    preview: PreviewRef | None = None
    completion: CompletionState = field(default_factory=CompletionState)


EntryDraft = Entry


def legacy_dictionary(group):
    from .types import PromptDictionary, PromptEntry
    # rating remains unknown; group NSFW is never forged into a trusted rating.
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
    # The sole deterministic inference prescribed by §6.
    if not normalized and values['expand_nl'] and not values['nl']:
        values['nl'] = values['expand_nl']
    missing = tuple(k for k in ('tag', 'expand_nl', 'nl') if not (normalized if k == 'tag' else values[k]))
    return Entry(identity, tuple(dict.fromkeys(normalized)), **values,
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
    schema_version: int = 3
    kind: str = 'prompts'
    category: str = 'full_content'
    _operations: Mapping = field(default_factory=dict, repr=False)

    def __post_init__(self):
        identifier(self.dictionary_id)
        require(isinstance(self.name, str) and 1 <= len(self.name.strip()) <= 128)
        require(self.scope in ('global', 'project') and type(self.nsfw) is bool)
        require(type(self.revision) is int and self.revision >= 1)
        require(self.schema_version == 3 and self.kind == 'prompts' and self.category == 'full_content')
        require(all(isinstance(e, Entry) for e in self.entries))
        require(len({e.entry_id for e in self.entries}) == len(self.entries), 'entry:id_conflict')
        object.__setattr__(self, 'name', self.name.strip())
        object.__setattr__(self, 'entries', tuple(self.entries))
        object.__setattr__(self, '_operations', immutable(self._operations))


def decode_group(raw):
    require(isinstance(raw, dict))
    require(set(raw) <= set(Group.__dataclass_fields__))
    require(raw.get('schema_version') == 3, 'newer_version')
    entries = []
    for row in raw.get('entries', []):
        require(isinstance(row, dict) and set(row) <= set(Entry.__dataclass_fields__))
        entry = validate_entry({k: v for k, v in row.items() if k in CONTENT | {'entry_id'}})
        preview = PreviewRef(**row['preview']) if row.get('preview') else None
        state = row.get('completion')
        completion = CompletionState(**(state | {'missing': tuple(state['missing'])})) if state else entry.completion
        require(completion.state in ('pending', 'queued', 'running', 'complete', 'failed', 'paused', 'canceled'))
        require(set(completion.missing) <= {'tag', 'expand_nl', 'nl'})
        entries.append(replace(entry, preview=preview, completion=completion))
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
                 entries=tuple(entries), revision=old.revision + 1)


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
