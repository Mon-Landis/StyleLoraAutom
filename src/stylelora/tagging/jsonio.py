"""Strict deterministic JSON and atomic product file publication."""
import json
import os
import tempfile
from pathlib import Path
from stylelora.project_db.errors import ValidationError
from stylelora.project_db.validation import dumps as canonical_json


def loads(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError('schema:duplicate_key')
            result[key] = value
        return result
    def invalid(value):
        raise ValidationError('schema:nonfinite')
    try:
        if len(data) > 10 * 1024 * 1024:
            raise ValidationError('schema:too_large')
        if isinstance(data, bytes):
            data = data.decode('utf-8-sig')
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError('schema:invalid') from exc


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.partial-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
