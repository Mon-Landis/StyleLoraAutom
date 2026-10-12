from dataclasses import asdict
from ..connection import ReadSession, WriteSession
from ..types import Page

from ..errors import StateConflict, UnsupportedScheme, ValidationError
from ..types import ExportSchemeInput, ImportSchemeInput, ExportSchemeRow, ImportSchemeRow, PageRequest, WriteResult
from ..validation import dumps, now, safe_snapshot, text, confidence, choice
from .common import get, page as paginate, cas


def validate(value):
    text(value.name,nonempty=True)
    data = asdict(value)
    if isinstance(value,ExportSchemeInput):
        if value.exporter != 'image_txt':
            raise UnsupportedScheme('Unsupported exporter')
        for key in ('filter_json','caption_config_json','output_config_json'):
            safe_snapshot(data[key])
        config = value.caption_config_json
        if 'wd_min_confidence' in config:
            confidence(config['wd_min_confidence'])
        if 'tag_sort' in config:
            choice(config['tag_sort'],('layer_first','prompt_pyramid'))
        if not isinstance(value.pipeline_json,tuple):
            raise ValidationError('Pipeline tuple required')
        for processor in value.pipeline_json:
            safe_snapshot(processor)
    else:
        if value.reader != 'txt_sidecar':
            raise UnsupportedScheme('Unsupported reader')
        safe_snapshot(value.params_json)
        if type(value.is_default) is not bool:
            raise ValidationError('Boolean required')
    return {key: dumps(val) if key.endswith('_json') else val for key,val in data.items()}


def _create(db, table, dto, value):
    data = validate(value)
    data.update(created_at=now(),updated_at=now())
    cursor = db.execute(f"INSERT INTO {table}({','.join(data)}) VALUES({','.join('?' for _ in data)})",tuple(data.values()))
    return get(db,table,dto,cursor.lastrowid)


def _update(db, table, dto, identity, value, expected):
    old = get(db,table,dto,identity)
    data = validate(value)
    if all(getattr(old,key) == getattr(value,key) for key in asdict(value)):
        return WriteResult(old,False)
    if old.updated_at != expected:
        raise StateConflict('Scheme edited concurrently')
    if table == 'import_schemes' and old.is_default != value.is_default:
        raise StateConflict('Use atomic default switch')
    data['updated_at'] = now(old.updated_at)
    cas(db,table,identity,data,{'updated_at':expected})
    return WriteResult(get(db,table,dto,identity),True)


def get_export_scheme(db: ReadSession, scheme_id: int) -> ExportSchemeRow:
    return get(db,'export_schemes',ExportSchemeRow,scheme_id)


def list_export_schemes(db: ReadSession, page: PageRequest) -> Page[ExportSchemeRow]:
    return paginate(db,'export_schemes',ExportSchemeRow,page)


def create_export_scheme(db: WriteSession, value: ExportSchemeInput) -> ExportSchemeRow:
    return _create(db,'export_schemes',ExportSchemeRow,value)


def update_export_scheme(db: WriteSession, scheme_id: int, value: ExportSchemeInput, *, expected_updated_at: str) -> WriteResult[ExportSchemeRow]:
    return _update(db,'export_schemes',ExportSchemeRow,scheme_id,value,expected_updated_at)


def delete_export_scheme(db: WriteSession, scheme_id: int) -> None:
    get_export_scheme(db,scheme_id)
    if db.execute('SELECT count(*) FROM export_schemes').fetchone()[0] <= 1:
        raise StateConflict('Cannot delete final export scheme')
    db.execute('DELETE FROM export_schemes WHERE scheme_id=?',(scheme_id,))


def get_import_scheme(db: ReadSession, scheme_id: int) -> ImportSchemeRow:
    return get(db,'import_schemes',ImportSchemeRow,scheme_id)


def get_default_import_scheme(db: ReadSession) -> ImportSchemeRow:
    identity = db.execute('SELECT scheme_id FROM import_schemes WHERE is_default=1').fetchone()
    if not identity:
        raise StateConflict('Missing default scheme')
    return get_import_scheme(db,identity[0])


def list_import_schemes(db: ReadSession, page: PageRequest) -> Page[ImportSchemeRow]:
    return paginate(db,'import_schemes',ImportSchemeRow,page)


def create_import_scheme(db: WriteSession, value: ImportSchemeInput) -> ImportSchemeRow:
    return _create(db,'import_schemes',ImportSchemeRow,value)


def update_import_scheme(db: WriteSession, scheme_id: int, value: ImportSchemeInput, *, expected_updated_at: str) -> WriteResult[ImportSchemeRow]:
    return _update(db,'import_schemes',ImportSchemeRow,scheme_id,value,expected_updated_at)


def set_default_import_scheme(db: WriteSession, scheme_id: int) -> WriteResult[ImportSchemeRow]:
    target = get_import_scheme(db,scheme_id)
    if target.is_default:
        return WriteResult(target,False)
    old = get_default_import_scheme(db)
    db.execute('UPDATE import_schemes SET is_default=0,updated_at=? WHERE scheme_id=?',(now(old.updated_at),old.scheme_id))
    db.execute('UPDATE import_schemes SET is_default=1,updated_at=? WHERE scheme_id=?',(now(target.updated_at),scheme_id))
    return WriteResult(get_import_scheme(db,scheme_id),True)


def delete_import_scheme(db: WriteSession, scheme_id: int) -> None:
    if get_import_scheme(db,scheme_id).is_default:
        raise StateConflict('Switch default before deleting')
    db.execute('DELETE FROM import_schemes WHERE scheme_id=?',(scheme_id,))


def require_executable(scheme: ExportSchemeRow) -> None:
    if scheme.pipeline_json:
        raise UnsupportedScheme('Unsupported processor chain')


def initialize_defaults(db) -> None:
    create_import_scheme(db,ImportSchemeInput('Default',params_json={'schema_version':1,'encoding':'utf-8-sig','tags_separator':',','paragraphs':'blank_lines','errors':'strict'},is_default=True))
    create_export_scheme(db,ExportSchemeInput('Default',filter_json={'schema_version':1,'review_statuses':['keep'],'include_missing':False},
        caption_config_json={'schema_version':1,'tag_layers':['original','wd','llm','manual','rule'],'layer_priority':['manual','rule','llm','wd','original'],
                             'wd_min_confidence':0.0,'wd_null_confidence':'exclude','tag_sort':'layer_first','include_nl':False,
                             'nl_priority':['manual','llm','original'],'trigger_first':True,'separator':', '},
        output_config_json={'schema_version':1,'directory':'{scheme_id}/{utc}-{random}','new_directory':True}))
