"""Persistence-side provenance for full manual/rule snapshots (not caption output).

The effective view here has no export threshold or sorter. F09 owns caption
composition; F01 only computes the difference needed to persist removals safely.
"""
from .errors import PreviewStale
from .types import AnnotationInput, CurrentRef, Tag
from .validation import normalize, now, tags
from .repositories.annotations import get_current

LAYERS = ('manual','rule','llm','wd','original')


def effective_evidence(db, image_id, read_set):
    rows = []
    for layer in LAYERS:
        current = get_current(db,image_id,layer,'tags')
        ref = CurrentRef(image_id,layer,'tags',current.annotation_id if current else None)
        if ref not in read_set:
            raise PreviewStale('Full snapshot requires evidence for all five tag layers')
        if current:
            rows.append(current)
    cutoffs = {}
    removals = {}
    for row in rows:
        for entry in row.params_json.get('merge_meta',{}).get('removals',[]):
            key = normalize(entry['tag'])
            if entry['cutoff_annotation_id'] > cutoffs.get(key,0):
                cutoffs[key] = entry['cutoff_annotation_id']
                removals[key] = entry.copy()
    evidence = {}
    for row in rows:
        for tag in row.content:
            key = normalize(tag.tag)
            origins = row.params_json.get('merge_meta',{}).get('tag_origins',{}).get(key,[{'layer':row.source,'annotation_id':row.annotation_id,'conf':tag.conf}])
            valid = [dict(o) for o in origins if o['annotation_id'] > cutoffs.get(key,0)]
            if valid:
                output = evidence.setdefault(key,[])
                for origin in valid:
                    if origin not in output:
                        output.append(origin)
    return evidence,removals


def make_snapshot(db, image_id, layer, values, read_set):
    tags(values)
    evidence, removals = effective_evidence(db,image_id,read_set)
    new_id = db.execute('SELECT coalesce(max(annotation_id),0)+1 FROM annotations').fetchone()[0]
    origins = {}
    for tag in values:
        key = normalize(tag.tag)
        origins[key] = evidence.get(key,[{'layer':layer,'annotation_id':new_id,'conf':tag.conf}])
    for removed in evidence.keys() - origins.keys():
        removals[removed] = {'tag':removed,'cutoff_annotation_id':new_id,'removed_at':now()}
    # Retain old removal boundaries, including explicitly re-added tags; their
    # new evidence is later than the cutoff. Never refresh an untouched removal.
    meta = {'schema_version':1,'tag_origins':origins,'removals':[removals[k] for k in sorted(removals)]}
    return AnnotationInput(image_id,layer,'tags',tuple(values),params={'schema_version':1,'merge_meta':meta})
