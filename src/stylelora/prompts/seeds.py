"""Only frozen ItemSeed projections cross the generation-source boundary."""
from .types import *
from .validation import normalize_tag, tag_key, validate_content, require
from .dictionaries import digest


def serialize_content(entry):
    validate_content(entry)
    return ', '.join(normalize_tag(t) for t in entry.tags)+(', '+entry.natural_language.strip() if entry.natural_language else '')


def to_item_seeds(entries):
    entries=tuple(entries); seen=set(); result=[]
    for entry in entries:
        key=tag_key(entry)
        require(key not in seen,'sampling:duplicate'); seen.add(key)
        result.append(ItemSeed(len(result)+1,serialize_content(entry),entry.aspect))
    return tuple(result)


def freeze_entries(entries, target_count, *, report=None):
    require(len(entries)==target_count and (report is None or report.status=='satisfied'),'seeds:not_ready')
    items=to_item_seeds(entries)
    return FrozenSeeds(digest(items),items,len(items))


async def deliver(frozen, project, source_id, *, start):
    """Source runner owns idempotent start/query/resume. No project data is stored here."""
    require(isinstance(frozen,FrozenSeeds) and frozen.count>0 and digest(frozen.items)==frozen.digest,'seeds:not_ready')
    return await start(project,source_id,frozen.items)
