from dataclasses import dataclass
from collections.abc import Mapping
from stylelora.project_db.types import Tag
from stylelora.project_db.errors import ValidationError
from stylelora.tagging.normalization import normalize_tag
from stylelora.caption.types import CaptionConfig


@dataclass(frozen=True)
class Origin:
    layer: str
    annotation_id: int
    conf: float | None


@dataclass(frozen=True)
class Removal:
    tag: str
    cutoff_annotation_id: int
    removed_at: str


@dataclass(frozen=True)
class MergeView:
    tags: tuple[Tag, ...]
    origins: Mapping[str, tuple[Origin, ...]]
    removals: Mapping[str, tuple[Removal, ...]]
    nl: str | None
    image_id: int = 0


def merge_annotations(rows, config=CaptionConfig(), *, editing=False):
    rows = tuple(r for r in rows if r.is_current)
    if len({r.image_id for r in rows}) > 1 or len({(r.source,r.type) for r in rows}) != len(rows):
        raise ValidationError('merge:invalid_rows')
    layers = {r.source:r for r in rows if r.type == 'tags' and (editing or r.source in config.tag_layers)}
    cutoffs, removals, origins, output = {}, {}, {}, {}
    for layer, row in layers.items():
        entries = tuple(Removal(**v) for v in row.params_json.get('merge_meta',{}).get('removals',[]))
        removals[layer] = entries
        for entry in entries:
            key = normalize_tag(entry.tag)
            cutoffs[key] = max(cutoffs.get(key,0), entry.cutoff_annotation_id)
    for layer in (CaptionConfig().layer_priority if editing else config.layer_priority):
        row = layers.get(layer)
        if row is None:
            continue
        for tag in row.content:
            key = normalize_tag(tag.tag)
            evidence = row.params_json.get('merge_meta',{}).get('tag_origins',{}).get(key)
            candidates = tuple(Origin(**o) for o in evidence) if evidence else (Origin(layer,row.annotation_id,tag.conf),)
            for origin in candidates:
                if origin.annotation_id > row.annotation_id or origin.annotation_id < 1:
                    raise ValidationError('merge:invalid_origin')
                if origin.annotation_id <= cutoffs.get(key,0):
                    continue
                if not editing and origin.layer == 'wd':
                    if origin.conf is None and config.wd_null_confidence == 'exclude':
                        continue
                    if origin.conf is not None and origin.conf < config.wd_min_confidence:
                        continue
                output.setdefault(key, Tag(key, tag.conf))
                bucket = origins.setdefault(key, [])
                if origin not in bucket:
                    bucket.append(origin)
    nl = None
    if editing or config.include_nl:
        by_layer = {r.source:r.content for r in rows if r.type == 'nl'}
        nl = next((by_layer[k] for k in config.nl_priority if by_layer.get(k, '').strip()), None)
    return MergeView(tuple(output.values()), {k:tuple(v) for k,v in origins.items()}, removals, nl,
                     rows[0].image_id if rows else 0)
