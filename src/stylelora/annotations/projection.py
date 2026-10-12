from dataclasses import dataclass
from collections.abc import Mapping
from stylelora.project_db import read_snapshot
from stylelora.project_db.repositories import annotations, images
from stylelora.project_db.types import AnnotationRow, CurrentRef, Tag
from stylelora.caption.types import CaptionConfig
from stylelora.tagging.normalization import normalize_tag
from .merge import merge_annotations

LAYERS = CaptionConfig().layer_priority


def read_rows(db, image_id, *, nl=True):
    images.get_image(db, image_id)
    rows, refs = [], []
    for layer in LAYERS:
        for kind in (('tags','nl') if nl and layer in ('manual','llm','original') else ('tags',)):
            row = annotations.get_current(db,image_id,layer,kind)
            refs.append(CurrentRef(image_id,layer,kind,row.annotation_id if row else None))
            if row:
                rows.append(row)
    return tuple(rows), tuple(refs)


@dataclass(frozen=True)
class AnnotationView:
    image_id: int
    tags: tuple[Tag, ...]
    natural_language: str | None
    layers: Mapping[str, AnnotationRow | None]
    refs: tuple[CurrentRef, ...]
    missing_original: bool
    wd_overlap: Mapping[str, bool]


def get_annotation_view(project, image_id, config=CaptionConfig()):
    with read_snapshot(project) as db:
        rows, refs = read_rows(db,image_id)
    view = merge_annotations(rows,config,editing=True)
    layers = {k:next((r for r in rows if r.source == k and r.type == 'tags'),None) for k in LAYERS}
    original, wd = layers['original'], layers['wd']
    keys = {normalize_tag(t.tag) for t in (original or wd).content} if original or wd else set()
    overlap = {normalize_tag(t.tag):normalize_tag(t.tag) in keys for t in wd.content} if wd else {}
    return AnnotationView(image_id,view.tags,view.nl,layers,refs,original is None,overlap)
