from ..errors import CurrentConflict, StateConflict, ValidationError
from collections.abc import Mapping
from typing import Literal
from ..connection import ReadSession, WriteSession
from ..types import Page, Layer, AnnotationType
from ..types import AnnotationRow, AnnotationInput, CurrentRef, PageRequest, WriteResult
from ..validation import choice, confidence, dumps, now, safe_snapshot, tags, text, normalize, timestamp
from .common import get, row, page as paginate


def get_current(db: ReadSession, image_id: int, source: Layer, type: AnnotationType) -> AnnotationRow | None:
    value = db.execute('SELECT * FROM annotations WHERE image_id=? AND source=? AND type=? AND is_current=1', (image_id, source, type)).fetchone()
    return row(AnnotationRow, value) if value else None


def list_history(db: ReadSession, image_id: int, source: Layer, type: AnnotationType, page: PageRequest) -> Page[AnnotationRow]:
    return paginate(db, 'annotations', AnnotationRow, page, lambda r: (r.image_id,r.source,r.type) == (image_id,source,type))


def is_covered(db: ReadSession, image_id: int, keys: Mapping[tuple[Layer, AnnotationType], str]) -> bool:
    return bool(keys) and all((current := get_current(db, image_id, *layer)) is not None and current.cache_key == key for layer,key in keys.items())


def validate_merge_meta(db, image_id, meta, new_id):
    safe_snapshot(meta)
    if set(meta) != {'schema_version', 'tag_origins', 'removals'} or not isinstance(meta['tag_origins'], dict) or not isinstance(meta['removals'], list):
        raise ValidationError('Invalid merge metadata')
    for key, origins in meta['tag_origins'].items():
        if normalize(key) != key or not isinstance(origins, list) or not origins:
            raise ValidationError('Invalid tag origins')
        for origin in origins:
            if set(origin) != {'layer','annotation_id','conf'}:
                raise ValidationError('Invalid origin evidence')
            choice(origin['layer'], ('original','wd','llm','manual','rule'))
            confidence(origin['conf'])
            identity = origin['annotation_id']
            if type(identity) is not int or identity > new_id or identity < 1:
                raise ValidationError('Future or invalid origin')
            if identity != new_id:
                previous = get(db, 'annotations', AnnotationRow, identity)
                if previous.image_id != image_id or previous.source != origin['layer'] or previous.type != 'tags' or not any(normalize(t.tag)==key and t.conf==origin['conf'] for t in previous.content):
                    raise ValidationError('Origin does not match annotation')
    for removal in meta['removals']:
        if set(removal) != {'tag','cutoff_annotation_id','removed_at'}:
            raise ValidationError('Invalid removal evidence')
        normalize(removal['tag'])
        timestamp(removal['removed_at'])
        cutoff = removal['cutoff_annotation_id']
        if type(cutoff) is not int or not 1 <= cutoff <= new_id:
            raise ValidationError('Invalid removal boundary')
        if cutoff != new_id and get(db, 'annotations', AnnotationRow, cutoff).image_id != image_id:
            raise ValidationError('Removal belongs to another image')


def append_version(db: WriteSession, value: AnnotationInput, *, expected: CurrentRef, policy: Literal['cache','content','force_new_job']) -> WriteResult[AnnotationRow]:
    choice(policy, ('cache', 'content', 'force_new_job'))
    choice(value.source, ('original','wd','llm','manual','rule'))
    choice(value.type, ('tags','nl'))
    confidence(value.confidence)
    safe_snapshot(value.params)
    if value.type == 'tags':
        tags(value.content)
        content = dumps(value.content)
    else:
        text(value.content)
        content = value.content
    if value.source in ('original','rule','manual') and db.authority != value.source:
        raise StateConflict('Layer requires its atomic command')
    if value.source in ('wd','llm'):
        if db.authority != 'job' or not value.job_id or not value.model_name or not value.cache_key or not value.params.get('actual'):
            raise ValidationError('Model results require job and actual parameters')
    if value.source in ('original','manual','rule') and (value.model_name is not None or value.job_id is not None):
        raise ValidationError('Invalid layer provenance')
    if value.source in ('manual','rule') and value.cache_key is not None:
        raise ValidationError('Manual/rule snapshots do not have cache keys')
    if value.source in ('wd','rule') and value.type != 'tags':
        raise ValidationError('Layer supports tags only')
    if (expected.image_id, expected.source, expected.type) != (value.image_id,value.source,value.type):
        raise CurrentConflict('Reference tuple differs')
    current = get_current(db, value.image_id, value.source, value.type)
    if current:
        same = current.content == value.content and current.params_json == value.params
        if (policy == 'cache' and value.cache_key and current.cache_key == value.cache_key) or (policy == 'content' and same):
            return WriteResult(current, False)
    if (current.annotation_id if current else None) != expected.annotation_id:
        raise CurrentConflict('Current annotation changed')
    new_id = db.execute('SELECT coalesce(max(annotation_id),0)+1 FROM annotations').fetchone()[0]
    if value.source in ('manual','rule') and value.type == 'tags':
        validate_merge_meta(db, value.image_id, value.params.get('merge_meta', {}), new_id)
    if current:
        db.execute('UPDATE annotations SET is_current=0 WHERE annotation_id=? AND is_current=1', (current.annotation_id,))
    db.execute('INSERT INTO annotations(annotation_id,image_id,source,type,content,confidence,model_name,params_json,job_id,cache_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (new_id,value.image_id,value.source,value.type,content,value.confidence,value.model_name,dumps(value.params),value.job_id,value.cache_key,now()))
    return WriteResult(get(db, 'annotations', AnnotationRow, new_id), True)
