from dataclasses import asdict
import hashlib
import json
from stylelora.project_db.repositories import annotations


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


_TRANSIENT = frozenset(('retries', 'attempts', 'timeout', 'concurrency', 'batch_size',
                       'actual_batch', 'job_id', 'rated_at', 'binding_id', 'revision_binding',
                       'free_bytes', 'device_index', 'requested_tier', 'tag_mapping', 'tiers'))


def semantic(value):
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k not in _TRANSIENT}
    if isinstance(value, (tuple, list)):
        return [semantic(v) for v in value]
    return value


def cache_keys(context, *, actual_model, actual_params):
    snapshot = context.snapshot
    wd = snapshot['role'] == 'wd'
    source = 'wd' if wd else 'llm'
    config = {'params': semantic(snapshot['params']), 'actual': semantic(actual_params),
              'protocol': snapshot['protocol'], 'routing': semantic(snapshot['routing'])}
    if not wd:
        config['evidence'] = context.evidence.digest
    return {(source, kind): 'exec-v1:' + digest({
        'config': dict(config, source=source, type=kind), 'model': actual_model,
        'skill_hash': 'none:wd-v1' if wd else snapshot['skill_hash'],
        'image_sha256': context.image.sha256}) for kind in snapshot['target_types']}


def covered_group(db, image_id, keys):
    if not annotations.is_covered(db, image_id, keys):
        return ()
    rows = [annotations.get_current(db, image_id, *layer) for layer in keys]
    if len({row.job_id for row in rows}) != 1 or any(
            row.model_name != rows[0].model_name or row.params_json != rows[0].params_json for row in rows):
        return ()
    return tuple(row.annotation_id for row in rows)
