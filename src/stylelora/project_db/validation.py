"""Strict DTO primitives and portable project-relative path checks."""
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path, PureWindowsPath
import re

from .errors import UnsafePath, ValidationError
from .types import JsonObject, Scope, Tag


def now(after: str | None = None) -> str:
    value = datetime.now(timezone.utc)
    if after:
        value = max(value, datetime.strptime(after, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) + timedelta(microseconds=1))
    return value.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def timestamp(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z', value):
        raise ValidationError('UTC timestamp required')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError as exc:
        raise ValidationError('Invalid timestamp') from exc


def integer(value: int, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValidationError('Integer outside permitted range')


def text(value: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValidationError('Invalid text')


def choice(value: str, values: tuple[str, ...]) -> None:
    if value not in values:
        raise ValidationError('Unsupported enum value')


def confidence(value: float | None) -> None:
    if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValidationError('Invalid confidence')


def normalize(value: str) -> str:
    text(value, nonempty=True)
    result = value.strip().lower().replace('_', ' ').strip()
    if not result:
        raise ValidationError('Empty normalized tag')
    return result


def tags(values: tuple[Tag, ...]) -> None:
    if not isinstance(values, (tuple, list)):
        raise ValidationError('Tags must be a sequence')
    for value in values:
        if not isinstance(value, Tag):
            raise ValidationError('Tag DTO required')
        normalize(value.tag)
        confidence(value.conf)


def dumps(value: object) -> str:
    def convert(item: object) -> object:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        raise TypeError('Unsupported JSON object')
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False, default=convert)
    except (TypeError, ValueError) as exc:
        raise ValidationError('Invalid JSON') from exc


def object_json(value: JsonObject, *, versioned: bool = False) -> str:
    if type(value) is not dict or any(type(k) is not str for k in value):
        raise ValidationError('JSON object required')
    if versioned and (type(value.get('schema_version')) is not int or value['schema_version'] != 1):
        raise ValidationError('Unsupported JSON schema version')
    return dumps(value)


def safe_snapshot(value: JsonObject, *, versioned: bool = True) -> str:
    result = object_json(value, versioned=versioned)
    def walk(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in {'url', 'endpoint', 'token', 'api_key', 'secret', 'import_root', 'save_path'}:
                    raise ValidationError('Product connection/path data cannot enter project')
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif isinstance(item, str) and (PureWindowsPath(item).drive or item.startswith(('/', '\\')) or '://' in item):
            raise UnsafePath('Absolute location in snapshot')
    walk(value)
    return result


def relative_path(root: Path, value: str, *, filename: bool = False) -> Path:
    text(value, nonempty=True)
    if '\\' in value or ':' in value or PureWindowsPath(value).drive or value.startswith('/'):
        raise UnsafePath('Project-relative forward-slash path required')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts) or (filename and len(parts) != 1):
        raise UnsafePath('Unsafe path component')
    result = root.joinpath(*parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise UnsafePath('Path escapes project root')
    return result


def sha256(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValidationError('Invalid SHA-256')


def digest(value: object) -> str:
    return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()


def scope_json(scope: Scope) -> str:
    if (scope.selected_ids is None) == (scope.filter is None):
        raise ValidationError('Exactly one scope selector required')
    if scope.selected_ids is not None:
        for image_id in scope.selected_ids:
            integer(image_id, 1)
        if len(set(scope.selected_ids)) != len(scope.selected_ids):
            raise ValidationError('Duplicate scope ID')
    return dumps({'schema_version': 1, **asdict(scope)})
