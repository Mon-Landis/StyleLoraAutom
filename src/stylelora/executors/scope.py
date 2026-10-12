from dataclasses import replace
from stylelora.project_db import read_snapshot
from stylelora.project_db.repositories import images
from stylelora.project_db.types import ImageFilter, PageRequest, Scope


def scope_from_json(value):
    query = value.get('filter')
    return Scope(tuple(value['selected_ids']) if value.get('selected_ids') is not None else None,
                 ImageFilter(**query) if query is not None else None)


def resolve_in_snapshot(db, scope, *, mode):
    if mode not in ('selected', 'all', 'uncovered'):
        raise ValueError('scope_invalid')
    if scope.selected_ids is not None:
        if any(type(i) is not int or i < 1 for i in scope.selected_ids):
            raise ValueError('scope_invalid')
        ids = tuple(sorted(set(scope.selected_ids)))
        for identity in ids:
            images.get_image(db, identity)
        return ids
    if mode == 'selected':
        raise ValueError('scope_invalid')
    query = scope.filter or ImageFilter()
    query = replace(query, review_statuses=query.review_statuses if query.review_statuses is not None
                    else ('unreviewed', 'keep'), include_missing=True)
    ids, after = [], 0
    while True:
        page = images.list_images(db, query, PageRequest(after_id=after, limit=1000))
        ids.extend(image.image_id for image in page.items)
        if page.next_after_id is None:
            return tuple(ids)
        after = page.next_after_id


def resolve_scope(project, scope, *, mode):
    with read_snapshot(project) as db:
        return resolve_in_snapshot(db, scope, mode=mode)
