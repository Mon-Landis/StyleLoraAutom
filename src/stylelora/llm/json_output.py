"""Strict JSON contract checking with explicit, optional local format recovery."""
import json
import math
import re

from .types import JsonObject, LlmFailure


def _invalid():
    return LlmFailure('llm:invalid_json', safe_message='JSON contract validation failed')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _constant(value):
    raise _invalid()


DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


def repair_json_text(text: str) -> str:
    """Remove fences/prose and retain the first complete JSON object, never eval."""
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, count=1, flags=re.IGNORECASE)
        text = re.sub(r'\s*```\s*$', '', text)
    start = text.find('{')
    if start < 0:
        raise _invalid()
    try:
        value, end = DECODER.raw_decode(text, start)
        if not isinstance(value, dict):
            raise _invalid()
        return text[start:end]
    except (ValueError, RecursionError):
        raise _invalid() from None


def _check(value, schema):
    if schema is True:
        return
    if schema is False:
        raise _invalid()
    # Fail closed for unsupported schema keywords instead of claiming validation.
    allowed = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum',
               'const', 'anyOf', 'oneOf', 'allOf', 'minItems', 'maxItems', 'uniqueItems',
               'minLength', 'maxLength', 'pattern', 'minimum', 'maximum', 'description',
               'title', '$schema', 'default', 'examples'}
    if set(schema) - allowed:
        raise LlmFailure('llm:schema_unsupported', outcome='not_sent')
    types = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
             'string': lambda v: isinstance(v, str), 'boolean': lambda v: type(v) is bool,
             'integer': lambda v: type(v) is int,
             'number': lambda v: type(v) in (int, float) and math.isfinite(v),
             'null': lambda v: v is None}
    wanted = schema.get('type')
    if wanted is not None:
        names = wanted if isinstance(wanted, list) else [wanted]
        if not any(types.get(name, lambda v: False)(value) for name in names):
            raise _invalid()
    for key in ('anyOf', 'oneOf'):
        if key in schema:
            matched = 0
            for branch in schema[key]:
                try:
                    _check(value, branch)
                    matched += 1
                except LlmFailure as exc:
                    if exc.code != 'llm:invalid_json':
                        raise
            if not matched or (key == 'oneOf' and matched != 1):
                raise _invalid()
    for branch in schema.get('allOf', []):
        _check(value, branch)
    if 'enum' in schema and not any(type(value) is type(v) and value == v for v in schema['enum']):
        raise _invalid()
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise _invalid()
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if set(schema.get('required', [])) - value.keys():
            raise _invalid()
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key])
            elif not any(k in schema for k in ('oneOf', 'anyOf', 'allOf')):
                _check(item, schema.get('additionalProperties', False))
    if isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', math.inf):
            raise _invalid()
        if schema.get('uniqueItems') and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            raise _invalid()
        for item in value:
            _check(item, schema.get('items', True))
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', math.inf):
            raise _invalid()
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            raise _invalid()
    if type(value) in (int, float):
        if not math.isfinite(value) or not schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf):
            raise _invalid()


def validate_json_reply(text: str, schema: JsonObject, *, validator=None) -> JsonObject:
    try:
        value = DECODER.decode(text)
        if not isinstance(value, dict):
            raise _invalid()
        _check(value, schema)
        if validator is not None:
            validator(value, schema)
        return value
    except LlmFailure:
        raise
    except Exception:
        raise _invalid() from None
