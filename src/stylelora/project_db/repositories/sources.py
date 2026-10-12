from dataclasses import asdict, replace
from collections.abc import Sequence
from ..connection import ReadSession, WriteSession
from ..types import Page

from ..errors import LockedSource, SeedConflict, StateConflict, ValidationError
from ..types import SourceRow, SourceItemRow, SourceDraft, SourcePatch, SourceFilter, ItemFilter, ItemSeed, PageRequest, WriteResult, UNSET
from ..validation import choice, integer, now, safe_snapshot, text
from .common import get, page as paginate, cas


def get_source(db: ReadSession, source_id: int) -> SourceRow:
    return get(db, 'sources', SourceRow, source_id)


def list_sources(db: ReadSession, query: SourceFilter, page: PageRequest) -> Page[SourceRow]:
    return paginate(db, 'sources', SourceRow, page, lambda r: not query.statuses or r.status in query.statuses)


def validate_draft(draft: SourceDraft) -> None:
    choice(draft.type, ('import', 'nai'))
    choice(draft.cost_policy, ('deny', 'ask', 'allow'))
    text(draft.name, nonempty=True)
    text(draft.style_prompt)
    text(draft.negative_prompt)
    safe_snapshot(draft.config, versioned=bool(draft.config))
    if draft.target_count is not None:
        integer(draft.target_count, 1)
    if draft.type == 'import' and (draft.style_prompt or draft.negative_prompt):
        raise ValidationError('Import prompts must be empty')


def create_source(db: WriteSession, draft: SourceDraft) -> SourceRow:
    validate_draft(draft)
    cursor = db.execute('INSERT INTO sources(type,name,config_json,cost_policy,style_prompt,negative_prompt,target_count,created_at) VALUES(?,?,?,?,?,?,?,?)',
                        (draft.type, draft.name, safe_snapshot(draft.config, versioned=bool(draft.config)), draft.cost_policy,
                         draft.style_prompt, draft.negative_prompt, draft.target_count, now()))
    return get_source(db, cursor.lastrowid)


def update_source(db: WriteSession, source_id: int, patch: SourcePatch) -> WriteResult[SourceRow]:
    old = get_source(db, source_id)
    data = {}
    for name in ('type', 'config', 'style_prompt', 'negative_prompt', 'target_count'):
        value = getattr(patch, name)
        if value is not UNSET:
            column = 'config_json' if name == 'config' else name
            if value != getattr(old, column):
                if old.locked or old.status != 'draft':
                    raise LockedSource('Source configuration is frozen')
                data[column] = value
    for name in ('name', 'cost_policy'):
        if getattr(patch, name) is not None and getattr(patch, name) != getattr(old, name):
            data[name] = getattr(patch, name)
    if not data:
        return WriteResult(old, False)
    candidate = replace(old, **data)
    validate_draft(SourceDraft(candidate.type, candidate.name, candidate.config_json, candidate.cost_policy,
                               candidate.style_prompt, candidate.negative_prompt, candidate.target_count))
    if 'config_json' in data:
        data['config_json'] = safe_snapshot(data['config_json'], versioned=bool(data['config_json']))
    cas(db, 'sources', source_id, data, {'status': old.status, 'locked': int(old.locked)})
    if 'cost_policy' in data and db.writer:
        db.after_commit.append(lambda: db.writer.cycles.pop(source_id, None))
    return WriteResult(get_source(db, source_id), True)


def delete_empty_draft(db: WriteSession, source_id: int) -> None:
    source = get_source(db, source_id)
    if source.status != 'draft' or db.execute('SELECT 1 FROM source_items WHERE source_id=?', (source_id,)).fetchone() or db.execute('SELECT 1 FROM images WHERE source_id=?', (source_id,)).fetchone():
        raise StateConflict('Only empty drafts may be removed')
    db.execute('DELETE FROM sources WHERE source_id=? AND status=?', (source_id, 'draft'))


def seed_items(db: WriteSession, source_id: int, items: Sequence[ItemSeed]) -> tuple[SourceItemRow, ...]:
    source = get_source(db, source_id)
    result = []
    for item in items:
        integer(item.seq, 1)
        text(item.prompt)
        if item.aspect is not None:
            choice(item.aspect, ('landscape', 'portrait', 'square'))
        if source.type == 'import' and item.aspect is not None:
            raise ValidationError('Import aspect must be null')
        existing = db.execute('SELECT item_id,prompt,aspect FROM source_items WHERE source_id=? AND seq=?', (source_id, item.seq)).fetchone()
        if existing:
            if (existing['prompt'], existing['aspect']) != (item.prompt, item.aspect):
                raise SeedConflict('Natural key already has different seed')
            result.append(get_item(db, existing['item_id']))
        else:
            if source.locked or db.authority != 'start_source':
                raise LockedSource('Items may only be seeded in source start')
            cursor = db.execute('INSERT INTO source_items(source_id,seq,prompt,aspect,updated_at) VALUES(?,?,?,?,?)',
                                (source_id, item.seq, item.prompt, item.aspect, now()))
            result.append(get_item(db, cursor.lastrowid))
    return tuple(result)


def get_item(db: ReadSession, item_id: int) -> SourceItemRow:
    return get(db, 'source_items', SourceItemRow, item_id)


def list_items(db: ReadSession, query: ItemFilter, page: PageRequest) -> Page[SourceItemRow]:
    return paginate(db, 'source_items', SourceItemRow, page,
                lambda r: r.source_id == query.source_id and (not query.statuses or r.status in query.statuses))


def count_item_states(db: ReadSession, source_id: int) -> dict[str, int]:
    result = dict.fromkeys(('pending', 'sent', 'done', 'failed', 'skipped'), 0)
    result.update(dict(db.execute('SELECT status,count(*) FROM source_items WHERE source_id=? GROUP BY status', (source_id,))))
    return result
