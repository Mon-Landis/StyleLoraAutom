from pathlib import Path
from dataclasses import replace
from stylelora.tagging.catalog import load_catalog
from stylelora.project_db.types import AnnotationRow, Tag


def catalog():
    return load_catalog(Path(__file__).parent/'fixtures/tag_catalog.json',complete=True)


def row(identity,layer,tags=(),*,nl=None,meta=None,image_id=1):
    return AnnotationRow(identity,image_id,layer,'nl' if nl is not None else 'tags',
                         nl if nl is not None else tuple(t if isinstance(t,Tag) else Tag(t) for t in tags),
                         None,None,{'schema_version':1,**({'merge_meta':meta} if meta else {})},None,None,
                         '2026-01-01T00:00:00.000000Z',True)


def snapshot(origins,removals=()):
    return {'schema_version':1,'tag_origins':origins,
            'removals':[{'tag':tag,'cutoff_annotation_id':identity,'removed_at':'2026-01-01T00:00:00.000000Z'} for tag,identity in removals]}


def origin(layer,identity,conf=None):
    return {'layer':layer,'annotation_id':identity,'conf':conf}
