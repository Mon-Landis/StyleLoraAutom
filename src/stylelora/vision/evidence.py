from dataclasses import asdict
from stylelora.project_db import read_snapshot
from stylelora.project_db.repositories.annotations import get_current
from stylelora.project_db.types import CurrentRef
from stylelora.executors.cache import digest
from stylelora.executors.types import Evidence


def evidence_in_snapshot(db, image_id):
    layers = (('original', 'tags'), ('original', 'nl'), ('wd', 'tags'))
    rows = [get_current(db, image_id, *layer) for layer in layers]
    refs = tuple(CurrentRef(image_id, *layer, row.annotation_id if row else None)
                 for layer, row in zip(layers, rows))
    contents = [row.content if row else ('' if layer[1] == 'nl' else ())
                for layer, row in zip(layers, rows)]
    payload = [{'present': row is not None, 'content':
                [asdict(tag) for tag in value] if isinstance(value, tuple) else value}
               for row, value in zip(rows, contents)]
    return Evidence(contents[0], contents[1], contents[2], refs, digest(payload))


def read_evidence(project, image_id):
    with read_snapshot(project) as db:
        return evidence_in_snapshot(db, image_id)
