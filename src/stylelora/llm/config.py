"""Product configuration, atomic writes and revision compare-and-swap."""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

from .types import EndpointConfig, LlmFailure, ModelConfig, ROLES, RoleBinding

_LOCK = RLock()


def product_path(variable: str, filename: str) -> Path:
    root = os.environ.get(variable)
    if not root:
        raise LlmFailure('llm:config_path', outcome='not_sent')
    return Path(root) / 'StyleLoraAutom' / filename


def atomic_json(path: Path, value, *, secure=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            if secure:
                secure(Path(name))
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def config_lock(path: Path):
    """Serialize threads and processes, including distinct store instances."""
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(path.suffix + '.lock').open('a+b') as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        raise LlmFailure('llm:config_invalid', outcome='not_sent') from None


def validate_endpoint(value: EndpointConfig) -> None:
    try:
        url = urlsplit(value.base_url)
        valid = (url.scheme in ('http', 'https') and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment)
        if (not valid or not re.fullmatch(r'[A-Za-z0-9_.-]+', value.endpoint_id)
                or value.revision < 0 or not value.protocol
                or len({m.name for m in value.models}) != len(value.models)
                or any(not m.name or m.capability_source not in ('manual', 'models', 'tested')
                       for m in value.models)
                or any(not re.fullmatch(r'[A-Za-z0-9_]+', ref) for ref in value.token_refs)):
            raise ValueError
    except (TypeError, ValueError):
        raise LlmFailure('llm:config_invalid', outcome='not_sent') from None


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else product_path('APPDATA', 'llm.json')

    def _read(self):
        value = read_json(self.path, {'endpoints': [], 'roles': {}, 'revision': 0})
        if not isinstance(value, dict) or not isinstance(value.get('roles'), dict):
            raise LlmFailure('llm:config_invalid', outcome='not_sent')
        return value

    def load_endpoints(self) -> tuple[EndpointConfig, ...]:
        try:
            endpoints = []
            for item in self._read()['endpoints']:
                raw = dict(item)
                raw['models'] = tuple(ModelConfig(**dict(m, response_aliases=tuple(m.get('response_aliases', ()))))
                                      for m in raw['models'])
                raw['token_refs'] = tuple(raw['token_refs'])
                endpoint = EndpointConfig(**raw)
                validate_endpoint(endpoint)
                endpoints.append(endpoint)
            return tuple(endpoints)
        except (KeyError, TypeError, ValueError):
            raise LlmFailure('llm:config_invalid', outcome='not_sent') from None

    def endpoint(self, endpoint_id: str) -> EndpointConfig:
        for endpoint in self.load_endpoints():
            if endpoint.endpoint_id == endpoint_id:
                return endpoint
        raise LlmFailure('llm:binding_missing', outcome='not_sent')

    def save_endpoint(self, value: EndpointConfig, *, expected_revision: int) -> EndpointConfig:
        validate_endpoint(value)
        with config_lock(self.path):
            document = self._read()
            endpoints = self.load_endpoints()
            old = next((e for e in endpoints if e.endpoint_id == value.endpoint_id), None)
            if (old.revision if old else 0) != expected_revision:
                raise LlmFailure('llm:revision_conflict', outcome='not_sent')
            if old and replace(value, revision=old.revision) == old:
                return replace(old, changed=False)
            saved = replace(value, revision=expected_revision + 1, changed=True)
            rows = [e for e in endpoints if e.endpoint_id != value.endpoint_id] + [saved]
            document['endpoints'] = []
            for endpoint in rows:
                raw = asdict(endpoint)
                raw.pop('changed')
                document['endpoints'].append(raw)
            document['revision'] += 1
            atomic_json(self.path, document)
            return saved

    def delete_endpoint(self, endpoint_id: str, *, expected_revision: int) -> None:
        with config_lock(self.path):
            document = self._read()
            endpoints = self.load_endpoints()
            old = next((e for e in endpoints if e.endpoint_id == endpoint_id), None)
            if old is None:
                raise LlmFailure('llm:binding_missing', outcome='not_sent')
            if old.revision != expected_revision:
                raise LlmFailure('llm:revision_conflict', outcome='not_sent')
            if any(v and v.get('endpoint_id') == endpoint_id for v in document['roles'].values()):
                raise LlmFailure('endpoint:in_use', outcome='not_sent')
            document['endpoints'] = [asdict(e) | {'models': [asdict(m) for m in e.models]}
                                    for e in endpoints if e.endpoint_id != endpoint_id]
            document['revision'] += 1
            atomic_json(self.path, document)

    def bind_role(self, role: str, endpoint_id: str | None, model: str | None = None,
                  *, expected_revision: int | None = None) -> RoleBinding | None:
        if role not in ROLES:
            raise LlmFailure('llm:role_invalid', outcome='not_sent')
        with config_lock(self.path):
            document = self._read()
            old = document['roles'].get(role)
            revision = old['revision'] if old else 0
            if expected_revision is not None and revision != expected_revision:
                raise LlmFailure('llm:revision_conflict', outcome='not_sent')
            if endpoint_id is not None:
                endpoint = self.endpoint(endpoint_id)
                if not any(m.name == model for m in endpoint.models):
                    raise LlmFailure('llm:model_missing', outcome='not_sent')
            target = {'endpoint_id': endpoint_id, 'model': model, 'revision': revision + 1}
            if old and old['endpoint_id'] == endpoint_id and old['model'] == model:
                return self.resolve_role(role)
            document['roles'][role] = target
            document['revision'] += 1
            atomic_json(self.path, document)
            return self.resolve_role(role)

    def resolve_role(self, role: str) -> RoleBinding | None:
        if role not in ROLES:
            raise LlmFailure('llm:role_invalid', outcome='not_sent')
        row = self._read()['roles'].get(role)
        if not row or row['endpoint_id'] is None:
            return None
        endpoint = self.endpoint(row['endpoint_id'])
        if not any(m.name == row['model'] for m in endpoint.models):
            raise LlmFailure('llm:model_missing', outcome='not_sent')
        return RoleBinding(endpoint.endpoint_id, role, row['model'], endpoint.protocol,
                           row['revision'])

    def role_map(self) -> tuple[int, dict]:
        """Read all bindings under one product lock for an atomic API snapshot."""
        with config_lock(self.path):
            document = self._read()
            return document['revision'], {
                role: ({'endpoint_id': row['endpoint_id'], 'model': row['model']}
                       if (row := document['roles'].get(role)) and row['endpoint_id'] else None)
                for role in ROLES}

    def save_role_map(self, roles: dict, *, expected_revision: int) -> tuple[int, dict]:
        """Validate and replace the complete map in a single atomic publication."""
        if not isinstance(roles, dict) or set(roles) != set(ROLES):
            raise LlmFailure('llm:role_invalid', outcome='not_sent')
        with config_lock(self.path):
            document = self._read()
            if document['revision'] != expected_revision:
                raise LlmFailure('llm:revision_conflict', outcome='not_sent')
            updated = {}
            changed = False
            for role, binding in roles.items():
                if role in ('clean', 'clean_nsfw') and binding is not None:
                    raise LlmFailure('llm:role_invalid', outcome='not_sent')
                if binding is not None:
                    if not isinstance(binding, dict) or set(binding) != {'endpoint_id', 'model'}:
                        raise LlmFailure('llm:role_invalid', outcome='not_sent')
                    endpoint = self.endpoint(binding['endpoint_id'])
                    model = next((m for m in endpoint.models if m.name == binding['model']), None)
                    if model is None or (role in ('vision', 'vision_nsfw', 'rating') and not model.supports_images):
                        raise LlmFailure('llm:image_unsupported', outcome='not_sent')
                target = binding or {'endpoint_id': None, 'model': None}
                old = document['roles'].get(role)
                different = (old is None and binding is not None) or (old is not None and any(old[k] != target[k] for k in target))
                changed |= different
                updated[role] = {**target, 'revision': (old['revision'] if old else 0) + int(different)}
            if changed:
                document['roles'] = updated
                document['revision'] += 1
                atomic_json(self.path, document)
            return document['revision'], {role: dict(binding) if binding else None for role, binding in roles.items()}


def load_endpoints(path: Path | None = None) -> tuple[EndpointConfig, ...]:
    return ConfigStore(path).load_endpoints()


def save_endpoint(value: EndpointConfig, *, expected_revision: int,
                  path: Path | None = None) -> EndpointConfig:
    return ConfigStore(path).save_endpoint(value, expected_revision=expected_revision)
