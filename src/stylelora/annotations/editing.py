from dataclasses import dataclass
from collections.abc import Mapping
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db import commands
from stylelora.project_db.repositories import annotations
from stylelora.project_db.types import Tag, CurrentRef, AnnotationInput
from stylelora.project_db.errors import ValidationError, BatchTooLarge, CurrentConflict
from stylelora.project_db.validation import dumps
from stylelora.tagging.parser import normalize_structured
from .projection import read_rows
from .merge import merge_annotations


@dataclass(frozen=True)
class ManualEdit:
    image_id: int
    tags: tuple[Tag, ...]
    natural_language: str | None
    read_set: tuple[CurrentRef, ...]
    write_nl: bool = False


@dataclass(frozen=True)
class ManualResult:
    annotation_ids: tuple[int, ...]
    changed: bool


@dataclass(frozen=True)
class BulkEditResult:
    results: Mapping[int, ManualResult]
    changed_count: int


def _commit(db, edits):
    for edit in edits:
        _, current_refs = read_rows(db,edit.image_id)
        required = set(current_refs if edit.write_nl else (r for r in current_refs if r.type == 'tags'))
        if not required.issubset(set(edit.read_set)):
            raise CurrentConflict('manual:current_conflict')
        commands.verify_read_set(db,edit.read_set,CurrentConflict)
    results = {}
    for edit in edits:
        result = commands.edit_manual_tags(db,edit.image_id,tags=edit.tags,read_set=edit.read_set)
        ids, changed = [result.value.annotation_id], result.changed
        if edit.write_nl:
            ref = next(r for r in edit.read_set if r.source == 'manual' and r.type == 'nl')
            with commands.authority(db,'manual'):
                result = annotations.append_version(db,AnnotationInput(edit.image_id,'manual','nl',edit.natural_language or '',
                                                      params={'schema_version':1}),expected=ref,policy='content')
            ids.append(result.value.annotation_id)
            changed |= result.changed
        results[edit.image_id] = ManualResult(tuple(ids),changed)
    return BulkEditResult(results,sum(r.changed for r in results.values()))


def _check_size(edits):
    if len(edits)>5000 or len(dumps(edits).encode('utf-8'))>16*1024*1024:
        raise BatchTooLarge('manual:batch_too_large')


def edit_manual(project, edit, *, catalog=None):
    edit = ManualEdit(edit.image_id,normalize_structured(edit.tags,catalog=catalog),edit.natural_language,edit.read_set,edit.write_nl)
    _check_size((edit,))
    return submit_write(project,lambda db:_commit(db,(edit,))).result().results[edit.image_id]


def bulk_edit_manual(project, image_ids, *, add, remove, expected, catalog=None):
    add, remove = normalize_structured(add,catalog=catalog), normalize_structured(remove,catalog=catalog)
    add_keys, remove_keys = {t.tag for t in add}, {t.tag for t in remove}
    if add_keys & remove_keys or not image_ids or len(set(image_ids)) != len(image_ids):
        raise ValidationError('manual:invalid_selection')
    if len(image_ids)>5000:
        raise BatchTooLarge('manual:batch_too_large')
    edits = []
    with read_snapshot(project) as db:
        for identity in image_ids:
            rows, refs = read_rows(db,identity)
            view = merge_annotations(rows,editing=True)
            values = tuple(t for t in view.tags if t.tag not in remove_keys)
            keys = {t.tag for t in values}
            values += tuple(t for t in add if t.tag not in keys)
            edits.append(ManualEdit(identity,values,None,expected.get(identity,())))
    _check_size(edits)
    return submit_write(project,lambda db:_commit(db,edits)).result()
