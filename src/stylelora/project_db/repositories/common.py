from dataclasses import fields
import json

from ..errors import NotFound, StateConflict
from ..types import Page, PageRequest, Tag
from ..validation import integer

TABLES = {'sources': 'source_id', 'source_items': 'item_id', 'images': 'image_id',
          'annotations': 'annotation_id', 'jobs': 'job_id', 'operation_log': 'op_id',
          'export_schemes': 'scheme_id', 'import_schemes': 'scheme_id'}


def row(dto, value):
    if value is None:
        raise NotFound('Record not found')
    values = dict(value)
    for key in values:
        if key.endswith('_json') and values[key] is not None:
            values[key] = json.loads(values[key])
            if key in ('changes_json', 'pipeline_json'):
                values[key] = tuple(values[key])
        if key in ('locked', 'file_missing', 'rating_verified', 'is_default', 'is_current'):
            values[key] = bool(values[key])
    if 'annotation_id' in values and values['type'] == 'tags':
        values['content'] = tuple(Tag(**tag) for tag in json.loads(values['content']))
    return dto(**values)


def get(db, table, dto, identity):
    integer(identity, 1)
    return row(dto, db.execute(f'SELECT * FROM {table} WHERE {TABLES[table]}=?', (identity,)).fetchone())


def page(db, table, dto, request: PageRequest, predicate=lambda value: True):
    integer(request.after_id)
    integer(request.limit, 1)
    if request.limit > 1000:
        from ..errors import ValidationError
        raise ValidationError('Maximum page size is 1000')
    key = TABLES[table]
    upper = request.upper_id
    if upper is None:
        upper = db.execute(f'SELECT coalesce(max({key}),0) FROM {table}').fetchone()[0]
    integer(upper)
    # Scan bounded keysets rather than unbounded SQL IN parameters. Filter members
    # may change between sessions as explicitly permitted by the paging contract.
    result = []
    after = request.after_id
    while len(result) <= request.limit:
        rows = db.execute(f'SELECT * FROM {table} WHERE {key}>? AND {key}<=? ORDER BY {key} LIMIT 200', (after, upper)).fetchall()
        if not rows:
            break
        for value in rows:
            after = value[key]
            item = row(dto, value)
            if predicate(item):
                result.append(item)
                if len(result) > request.limit:
                    break
    more = len(result) > request.limit
    result = result[:request.limit]
    return Page(tuple(result), getattr(result[-1], key) if more else None, upper)


def cas(db, table, identity, changes, expected):
    key = TABLES[table]
    clauses = ','.join(f'{name}=?' for name in changes)
    checks = ' AND '.join(f'{name} IS ?' for name in expected)
    cursor = db.execute(f'UPDATE {table} SET {clauses} WHERE {key}=? AND {checks}',
                        (*changes.values(), identity, *expected.values()))
    if cursor.rowcount != 1:
        raise StateConflict('Concurrent state change')
