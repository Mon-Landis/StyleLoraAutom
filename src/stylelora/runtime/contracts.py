"""Transport independent runtime failures and opaque versions."""
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import fields as dataclass_fields, is_dataclass


def plain(value):
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in dataclass_fields(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def canonical(value):
    return json.dumps(plain(value), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class Failure(Exception):
    def __init__(self, status, code, *, details=None):
        self.status, self.code, self.details = status, code, details or {}
        super().__init__(code)


def version(secret, *values):
    return hmac.new(secret, canonical(values), hashlib.sha256).hexdigest()


def check(expected, actual):
    if expected is None:
        raise Failure(428, 'concurrency:precondition_required')
    if expected != actual:
        raise Failure(409, 'concurrency:version_conflict')


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or value.keys() - set(allowed) or set(required) - value.keys():
        raise Failure(422, 'input:invalid')
    return value


def positive(value):
    if type(value) is not int or value <= 0:
        raise Failure(422, 'input:invalid')
    return value
