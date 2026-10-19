"""Scoped atomic snapshots, durable replay receipts and immutable image resources."""
import hashlib
import io
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4
from PIL import Image, ImageOps, UnidentifiedImageError
from .dictionaries import LOCK, _write, digest
from .groups import Group, GroupRef, PreviewRef, decode_group, migrate_group
from .group_legacy import legacy_content_issues
from .types import PromptError, Issue, plain
from .validation import require, strict_loads, identifier, PROMPT_GROUP_VERSIONS

JSON_LIMIT = 10 * 1024**2
IMAGE_LIMIT = 10 * 1024**2
PIXEL_LIMIT = 40_000_000
ZIP_LIMIT = 100 * 1024**2
EXPANDED_LIMIT = 200 * 1024**2
ENTRY_LIMIT = 10000


def _migration_backup(backup, original):
    """Publish only verified bytes, retaining both valid and damaged old backups."""
    with tempfile.NamedTemporaryFile(dir=backup.parent, prefix=backup.name + '.',
                                     suffix='.tmp', delete=False) as stream:
        staging = Path(stream.name)
        try:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            staging.unlink(missing_ok=True)
            raise
    try:
        require(staging.read_bytes() == original, 'storage:corrupt_snapshot')
        while True:
            if backup.exists():
                if backup.read_bytes() == original:
                    return
                try:
                    previous = strict_loads(backup.read_bytes())
                except PromptError:
                    previous = None
                require(not isinstance(previous, dict), 'storage:corrupt_snapshot')
                backup.rename(backup.with_name(backup.name + f'.corrupt-{time.time_ns()}-{uuid4().hex}'))
            try:
                # A hard link publishes atomically and fails if a backup already exists.
                os.link(staging, backup)
                return
            except FileExistsError:
                continue
    finally:
        staging.unlink(missing_ok=True)


@dataclass(frozen=True)
class PreparedAsset:
    full: bytes
    thumb: bytes
    width: int
    height: int


def prepare_preview(data, entry_id):
    identifier(entry_id)
    require(len(data) <= IMAGE_LIMIT, 'image:too_large')
    try:
        with Image.open(io.BytesIO(data)) as image:
            require(image.format in ('PNG', 'JPEG', 'WEBP') and not getattr(image, 'is_animated', False), 'image:invalid')
            require(image.width * image.height <= PIXEL_LIMIT, 'image:too_large')
            image.load()
            image = ImageOps.exif_transpose(image).convert('RGBA' if image.mode in ('RGBA', 'LA', 'P') else 'RGB')
            image.thumbnail((2048, 2048))
            # Construct fresh pixels; no EXIF/ICC/text metadata reaches the encoder.
            clean = Image.frombytes(image.mode, image.size, image.tobytes())
            full = io.BytesIO(); clean.save(full, 'WEBP')
            size = clean.size
            clean.thumbnail((320, 320))
            thumb = io.BytesIO(); clean.save(thumb, 'WEBP')
            return PreparedAsset(full.getvalue(), thumb.getvalue(), *size)
    except PromptError:
        raise
    except Image.DecompressionBombError:
        raise PromptError('image:too_large') from None
    except (OSError, ValueError, UnidentifiedImageError):
        raise PromptError('image:invalid') from None


class GroupStore:
    def __init__(self, product_root, project=lambda: None, *, auto_complete_default=lambda: True):
        self.product_root, self.project = Path(product_root), project
        self.auto_complete_default = auto_complete_default
        self.leases = set()

    def root(self, ref):
        if ref.scope == 'global':
            return self.product_root / 'dictionaries' / 'prompts'
        current = self.project()
        require(current is not None and current[0] == ref.project_token, 'project:closed')
        return Path(current[1]) / 'dictionaries' / 'prompts'

    def path(self, ref):
        return self.root(ref) / (ref.group_id + '.json')

    def _raw(self, ref, absent=False):
        path = self.path(ref)
        try:
            if not path.exists():
                if absent:
                    return None
                raise PromptError('dictionary:not_found')
            original = path.read_bytes()
            raw = strict_loads(original)
            require(isinstance(raw, dict))
            version = raw.get('schema_version')
            require(type(version) is int and version in PROMPT_GROUP_VERSIONS, 'newer_version')
            require(raw.get('dictionary_id') == ref.group_id and raw.get('scope', 'global') == ref.scope)
            if version in (1, 2, 3, 4):
                default = self.auto_complete_default()
                require(type(default) is bool)
                if raw.get('_deleted'):
                    migrated = raw | {'schema_version': 5, 'revision': raw['revision'] + 1,
                                      'scope': ref.scope, 'auto_complete': raw.get('auto_complete', default),
                                      'prompt_format': 'nai', 'normalization_version': 'nai-storage-v1'}
                else:
                    migrated = plain(migrate_group(raw, auto_complete_default=default))
                operations = dict(raw.get('_operations', {}))
                operations['migration:v5'] = {'digest': digest(raw), 'result': {
                    'revision': migrated['revision'], 'source_version': version,
                    'warnings': plain(tuple(issue for row in raw.get('entries', [])
                        for issue in legacy_content_issues(row))) if version in (3, 4) and not raw.get('_deleted') else [],
                    'nsfw_promoted': version in (1, 2) and migrated.get('nsfw', False)}}
                migrated['_operations'] = operations
                backup = path.with_suffix(f'.v{version}.bak')
                _migration_backup(backup, original)
                _write(path, migrated)
                raw = migrated
            require(raw.get('dictionary_id') == ref.group_id and raw.get('scope') == ref.scope)
            return raw
        except OSError:
            raise PromptError('storage:io_error') from None
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, PromptError) and exc.code in ('dictionary:not_found', 'newer_version', 'storage:io_error', 'import:limit', 'normalization:syntax', 'normalization:empty', 'import:conversion_failed'):
                raise
            raise PromptError('storage:corrupt_snapshot', issues=exc.issues if isinstance(exc, PromptError) else ()) from None

    def load(self, ref):
        with LOCK:
            raw = self._raw(ref)
            require(not raw.get('_deleted'), 'dictionary:not_found')
            try:
                return decode_group(raw)
            except (ValueError, TypeError, KeyError):
                raise PromptError('storage:corrupt_snapshot') from None

    def list(self):
        groups, issues = [], []
        current = self.project()
        roots = [('global', None, self.product_root)]
        if current:
            roots.insert(0, ('project', current[0], Path(current[1])))
        for scope, token, root in roots:
            try:
                paths = sorted((root / 'dictionaries/prompts').glob('*.json'))
                for path in paths:
                    try:
                        group = self.load(GroupRef(scope, token, path.stem))
                        groups.append(group)
                        if any(group._operations.get(key, {}).get('result', {}).get('nsfw_promoted') for key in ('migration:v3', 'migration:v4', 'migration:v5')):
                            issues.append(Issue('migration:nsfw_promoted', field=group.dictionary_id, severity='warning'))
                        report = group._operations.get('migration:v5', {}).get('result', {})
                        issues.extend(Issue(**warning) for warning in report.get('warnings', ()))
                    except PromptError as exc:
                        if exc.code != 'dictionary:not_found':
                            issues.append(Issue(exc.code if exc.code == 'newer_version' else 'storage:corrupt_snapshot', field=path.stem))
                            issues.extend(exc.issues)
            except OSError:
                issues.append(Issue('storage:io_error', field=scope))
        return tuple(groups), tuple(issues)

    def mutate(self, ref, expected_revision, request_key, operation, payload, change):
        """Replay is checked before CAS and before invoking a mutating callback."""
        require(isinstance(request_key, str) and 0 < len(request_key) <= 256)
        require(type(expected_revision) is int and expected_revision >= 0)
        signature = digest(dict(operation=operation, payload=payload, expected_revision=expected_revision))
        with LOCK:
            raw = self._raw(ref, absent=True)
            operations = dict(raw.get('_operations', {})) if raw else {}
            if request_key in operations:
                receipt = operations[request_key]
                require(receipt['digest'] == signature, 'dictionary:request_key_conflict')
                return receipt['result']
            if raw is None and expected_revision:
                raise PromptError('dictionary:not_found')
            require((raw['revision'] if raw else 0) == expected_revision,
                    'group:id_conflict' if expected_revision == 0 and operation in ('create', 'import') else 'dictionary:revision_conflict')
            require(not raw or not raw.get('_deleted'), 'dictionary:not_found')
            old = decode_group(raw) if raw else None
            value, result = change(old)
            revision = expected_revision + 1
            result = plain(result) | {'revision': revision}
            operations[request_key] = {'digest': signature, 'result': result}
            if value is None:
                require(old is not None, 'dictionary:not_found')
                obj = dict(schema_version=5, dictionary_id=ref.group_id, scope=ref.scope,
                           revision=revision, _deleted=True, _operations=operations)
            else:
                require(isinstance(value, Group), 'newer_version')
                require(value.dictionary_id == ref.group_id and value.scope == ref.scope)
                obj = plain(replace(value, revision=revision, _operations=operations))
                decode_group(obj)
            try:
                _write(self.path(ref), obj)
            except OSError:
                raise PromptError('storage:io_error') from None
            return result

    def save(self, ref, value, expected_revision, request_key):
        return self.mutate(ref, expected_revision, request_key, 'save', plain(value), lambda _: (value, {}))['revision']

    def delete(self, ref, expected_revision, request_key):
        return self.mutate(ref, expected_revision, request_key, 'delete', {}, lambda old: (None, {'deleted': ref.group_id}))

    def publish(self, asset, ref, entry_id):
        identifier(entry_id)
        revision = str(uuid4())
        root = self.root(ref) / 'previews' / ref.group_id
        try:
            root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
            try:
                for name, data in ((entry_id+'.webp', asset.full), (entry_id+'.thumb.webp', asset.thumb)):
                    with (staging/name).open('xb') as stream:
                        stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.replace(staging, root/revision)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        except OSError:
            raise PromptError('storage:io_error') from None
        return PreviewRef(revision, hashlib.sha256(asset.full).hexdigest(), asset.width, asset.height)

    def image(self, ref, entry, size='full', asset_revision=None):
        require(size in ('full', 'thumb'))
        require(entry.preview is not None, 'image:not_found')
        revision = asset_revision or entry.preview.asset_revision
        identifier(revision)
        path = self.root(ref)/'previews'/ref.group_id/revision/(entry.entry_id+('.thumb' if size == 'thumb' else '')+'.webp')
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise PromptError('image:not_found') from None
        except OSError:
            raise PromptError('storage:io_error') from None
        return data, hashlib.sha256(data).hexdigest()

    @contextmanager
    def lease(self, ref):
        with LOCK:
            group = self.load(ref)
            key = str(uuid4())
            paths = frozenset(str(self.root(ref)/'previews'/ref.group_id/e.preview.asset_revision) for e in group.entries if e.preview)
            self.leases.add((key, paths))
        try:
            yield group
        finally:
            with LOCK:
                self.leases.discard((key, paths))

    def collect_orphans(self, ref, retention=86400):
        with LOCK:
            raw = self._raw(ref, absent=True) or {}
            active = {e['preview']['asset_revision'] for e in raw.get('entries', []) if e.get('preview')}
            leased = {p for _, paths in self.leases for p in paths}
            root = self.root(ref)/'previews'/ref.group_id
            for path in root.glob('*'):
                if not path.name.startswith('.') and path.name not in active and str(path) not in leased and time.time()-path.stat().st_mtime >= retention:
                    shutil.rmtree(path)

    def collect_all(self):
        current = self.project()
        scopes = [('global', None, self.product_root)]
        if current:
            scopes.append(('project', current[0], Path(current[1])))
        for scope, token, root in scopes:
            for path in (root/'dictionaries/prompts/previews').glob('*'):
                try:
                    if path.is_dir() and not path.is_symlink():
                        self.collect_orphans(GroupRef(scope, token, path.name))
                except (PromptError, OSError):
                    # A damaged group must never cause its resources to be collected.
                    continue
