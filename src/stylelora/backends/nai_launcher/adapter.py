"""Launcher settings lease and tool mapping. Unverified remote evidence stays unknown."""
import os
import json
import time
import math
from dataclasses import fields
from pathlib import Path
from uuid import uuid4
from ..types import (BackendError, ProfileHandle, Preparation, GenerationResult, digest, plain)
from .client import connect, close_session
from .discovery import discover
from .compatibility import verify_compatibility, TRUSTED_MANIFEST
from stylelora.generation.profile import validate_profile, profile_values, validate_value
from stylelora.project_db.migrations import atomic_json
from stylelora.project_db.types import RecoveryEvidence

class LauncherBackend:
    def __init__(self, session, capabilities, *, journal_dir=None, manifest=TRUSTED_MANIFEST,
                 account_provider=None, evidence_decoder=None, lease_dir=None):
        report = verify_compatibility(session.server, session.tools, app_version=session.info.app_version,
                                      protocol_version=session.protocol_version, manifest=manifest)
        if not report.accepted:
            raise BackendError(report.reason, 'compatibility', retry='fatal')
        if not capabilities.supports_cost or not capabilities.session_settings:
            raise BackendError('backend:incompatible_schema', 'compatibility')
        self.session, self._caps = session, capabilities
        self.manifest = manifest
        self.instance = digest({'endpoint': session.info.endpoint, 'pid': session.info.pid,
                                'version': session.server.version, 'schema': report.schema_hash})
        self.lease_key = session.info.endpoint
        if journal_dir is None:
            base = os.environ.get('LOCALAPPDATA')
            if not base:
                raise BackendError('profile:restore_pending', 'journal')
            journal_dir = Path(base) / 'StyleLoraAutom/recovery/launcher'
        self.journal = Path(journal_dir) / 'launcher.json'
        if lease_dir is None and not os.environ.get('LOCALAPPDATA'):
            raise BackendError('profile:restore_pending', 'lease')
        base = lease_dir or Path(os.environ['LOCALAPPDATA']) / 'StyleLoraAutom/recovery/launcher/leases'
        self.lease_path = Path(base) / (self.instance + '.json')
        self.owner = uuid4().hex
        self.lease_id = None
        self.handle = None
        self.preparations = {}
        self.inflight = False
        self.account_provider, self.evidence_decoder = account_provider, evidence_decoder

    @classmethod
    async def open(cls, appdata, capabilities, *, transport=None, explicit_token=None,
                   environ=None, client_options=None, **options):
        environment = os.environ if environ is None else environ
        def reread():
            return discover(appdata, explicit_token=explicit_token, environ=environment)
        session = await connect(reread(), transport=transport, rediscover=reread, **(client_options or {}))
        try:
            return cls(session, capabilities, **options)
        except Exception:
            await close_session(session)
            raise

    async def capabilities(self):
        return self._caps

    async def account_status(self):
        return self.account_provider() if self.account_provider else None

    async def _call(self, name, arguments=None, kind='read'):
        self._check_connection_identity()
        if self.lease_id is not None:
            self._check_lease()
        result = await self.session.call(name, arguments or {}, kind=kind)
        self._check_connection_identity()
        if self.lease_id is not None:
            self._check_lease()
        return plain(result.payload)

    def _check_connection_identity(self):
        report = verify_compatibility(self.session.server, self.session.tools,
            app_version=self.session.info.app_version, protocol_version=self.session.protocol_version,
            manifest=self.manifest)
        identity = digest({'endpoint': self.session.info.endpoint, 'pid': self.session.info.pid,
                           'version': self.session.server.version, 'schema': report.schema_hash})
        if not report.accepted or identity != self.instance:
            raise BackendError('backend:incompatible_schema', 'compatibility')

    async def _settings(self):
        result = await self._call('get_generation_settings')
        values = result.get('settings')
        if not isinstance(values, dict):
            raise BackendError('profile:drift', 'settings')
        return values

    def _save_handle(self, handle):
        self._check_lease()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        data = {'schema_version': 1, 'owner': self.owner, **plain(handle)}
        atomic_json(self.journal, {**data, 'digest': digest(data)})

    def _read_lease(self):
        try:
            data = json.loads(self.lease_path.read_text(encoding='utf-8'))
            if set(data) != {'owner', 'lease_id', 'expires_at', 'backend_instance', 'journal'}:
                raise ValueError()
            if data['backend_instance'] != self.instance:
                raise ValueError()
            if (not all(isinstance(data[k], str) and data[k] for k in ('owner', 'lease_id', 'journal'))
                    or type(data['expires_at']) not in (int, float) or not math.isfinite(data['expires_at'])):
                raise ValueError()
            return data
        except (OSError, ValueError, TypeError):
            raise BackendError('profile:restore_conflict', 'lease') from None

    def _check_lease(self):
        data = self._read_lease()
        if (data['owner'], data['lease_id']) != (self.owner, self.lease_id) or data['expires_at'] <= time.time():
            raise BackendError('profile:restore_conflict', 'lease')

    def _acquire_lease(self):
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        identity = uuid4().hex
        data = {'owner': self.owner, 'lease_id': identity, 'expires_at': time.time() + 86400,
                'backend_instance': self.instance, 'journal': str(self.journal.resolve())}
        try:
            fd = os.open(self.lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise BackendError('profile:restore_pending', 'apply') from None
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self.lease_id = identity

    def _release_lease(self):
        self._check_lease()
        self.lease_path.unlink()
        self.lease_id = None

    async def recover_settings(self):
        """Explicit read-first recovery; never adopts another Launcher identity."""
        if not self.journal.exists():
            return
        try:
            data = json.loads(self.journal.read_text(encoding='utf-8'))
            fingerprint = data.pop('digest')
            if digest(data) != fingerprint or set(data) != {'schema_version', 'owner'} | {f.name for f in fields(ProfileHandle)}:
                raise ValueError()
            if data.pop('schema_version') != 1:
                raise ValueError()
            owner = data.pop('owner')
            handle = ProfileHandle(**data)
            if (handle.backend_instance != self.instance or not isinstance(owner, str) or not owner
                    or not isinstance(handle.lease_id, str) or not handle.lease_id):
                raise ValueError()
            specs = {s.key: s for s in self._caps.parameters if not s.read_only and s.key not in self._caps.per_request_parameters}
            keys = set(handle.writable_keys)
            if len(keys) != len(handle.writable_keys) or keys != set(handle.original) or keys != set(handle.applied) or not keys <= set(specs):
                raise ValueError()
            for key in keys:
                validate_value(handle.original[key], specs[key].value_schema)
                validate_value(handle.applied[key], specs[key].value_schema)
        except (OSError, ValueError, TypeError, KeyError, BackendError):
            self.session.unknown_write = True
            raise BackendError('profile:restore_conflict', 'restore') from None
        lock = self.lease_path.with_suffix('.recover')
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise BackendError('profile:restore_pending', 'restore') from None
        try:
            os.close(fd)
            lease = self._read_lease()
            if (lease['owner'], lease['lease_id'], lease['journal']) != (owner, handle.lease_id, str(self.journal.resolve())):
                raise BackendError('profile:restore_conflict', 'restore')
            if lease['owner'] != self.owner and lease['expires_at'] > time.time():
                raise BackendError('profile:restore_pending', 'restore')
            lease.update(owner=self.owner, expires_at=time.time() + 86400)
            atomic_json(self.lease_path, lease)
            self.lease_id = handle.lease_id
            self._save_handle(handle)
        finally:
            lock.unlink(missing_ok=True)
        self.handle = handle
        await self.restore(handle)

    async def apply_profile(self, profile):
        validate_profile(profile, self._caps)
        if self.handle is not None or self.journal.exists():
            raise BackendError('profile:restore_pending', 'apply')
        self._acquire_lease()
        try:
            current = await self._settings()
            applied = profile_values(profile)
            if not set(applied) <= set(current):
                raise BackendError('profile:drift', 'apply')
            # Only keys actually changed are restored; read-only/one-shot fields never enter the log.
            changed = {k: v for k, v in applied.items() if current[k] != v}
            handle = ProfileHandle(self.lease_id, {k: current[k] for k in changed}, changed,
                                   tuple(changed), self.instance)
            self._save_handle(handle)
            self.handle = handle
            self.profile = profile
            if changed:
                await self._call('update_generation_settings', changed, 'write')
            actual = await self._settings()
            if any(actual.get(k) != v for k, v in applied.items()):
                raise BackendError('profile:drift', 'apply')
            return handle
        except BaseException:
            if self.handle is None and not self.journal.exists():
                self._release_lease()
            raise

    async def restore(self, handle):
        self._check_lease()
        if self.handle != handle or handle.backend_instance != self.instance:
            raise BackendError('profile:restore_conflict', 'restore')
        if self.inflight or self.preparations:
            raise BackendError('profile:restore_pending', 'restore')
        current = await self._settings()
        values = {k: current.get(k) for k in handle.writable_keys}
        if values == plain(handle.original):
            self.session.resolve_write_barrier()
        elif values == plain(handle.applied):
            self.session.resolve_write_barrier()
            try:
                if handle.writable_keys:
                    await self._call('update_generation_settings', plain(handle.original), 'write')
            except BackendError as exc:
                if exc.outcome != 'unknown':
                    raise
                # Read back once; never repeat an unknown restore write.
                current = await self._settings()
                if any(current.get(k) != v for k, v in handle.original.items()):
                    raise BackendError('profile:restore_pending', 'restore') from None
                self.session.resolve_write_barrier()
        else:
            raise BackendError('profile:restore_conflict', 'restore')
        current = await self._settings()
        if any(current.get(k) != v for k, v in handle.original.items()):
            raise BackendError('profile:restore_conflict', 'restore')
        self._check_lease()
        self.journal.unlink(missing_ok=True)
        self.handle = None
        self._release_lease()

    async def prepare(self, request):
        if self.handle is None or self.inflight or self.preparations:
            raise BackendError('profile:restore_pending', 'prepare')
        if self.profile.effort == 'medium' and request.negative.strip():
            raise BackendError('profile:effort_conflict', 'prepare')
        if request.save_path.exists():
            raise BackendError('artifact:save_path_exists', 'prepare')
        args = {'operation': 'generate', 'count': 1, 'characters': [], 'prompt': request.positive,
                'negative_prompt': request.negative, 'width': request.width, 'height': request.height,
                'save_path': str(request.save_path)}
        if request.seed is not None:
            args['seed'] = request.seed
        schema = next(t.input_schema for t in self.session.tools if t.name == 'prepare_generation')
        for key in ('width', 'height', 'seed'):
            if key in args:
                validate_value(args[key], schema.get('properties', {}).get(key, {}))
        try:
            reply = await self._call('prepare_generation', args, 'write')
        except BackendError as exc:
            if exc.outcome == 'unknown':
                self.inflight = True  # unknown preparation side effect also blocks settings restore
            raise
        try:
            identity, effective = reply['preparation_id'], reply['effective']
            if not isinstance(identity, str) or not identity or not isinstance(effective, dict):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            self.inflight = True
            self.session.unknown_write = True
            raise BackendError('mcp:write_outcome_unknown', 'prepare', 'unknown') from None
        preparation = Preparation(identity, digest(request), reply.get('estimated_anlas'), effective, reply.get('expires_at'))
        self.preparations[identity] = (preparation, request, self.session.generation, self.handle.lease_id)
        expected = {**profile_values(self.profile), 'prompt': request.positive, 'negative_prompt': request.negative,
                    'width': request.width, 'height': request.height, 'characters': []}
        if any(effective.get(k) != v for k, v in expected.items()):
            await self.cancel_preparation(preparation)
            raise BackendError('profile:drift', 'prepare')
        return preparation

    def _prepared(self, preparation):
        entry = self.preparations.get(preparation.handle)
        if entry is None or entry[0] != preparation or entry[2] != self.session.generation or self.handle is None or entry[3] != self.handle.lease_id:
            raise BackendError('generation:stale_preparation', 'submit')
        return entry

    async def submit(self, preparation):
        _, request, _, _ = self._prepared(preparation)
        del self.preparations[preparation.handle]  # consume before await, including rejection/unknown
        self.inflight = True
        try:
            result = await self._call('submit_generation', {'preparation_id': preparation.handle}, 'write')
            path = result.get('saved_path')
            resource = result.get('resource_ref', {}).get('resourceId')
            if not isinstance(resource, str) or not resource or len(resource) > 512:
                raise BackendError('artifact:ownership_conflict', 'submit', 'unknown')
            if path is not None and (not isinstance(path, str) or len(path) > 4096 or Path(path).resolve() != request.save_path.resolve()):
                raise BackendError('artifact:ownership_conflict', 'submit', 'unknown')
            stripped = result.get('metadata_stripped')
            stripped = stripped if type(stripped) is bool else None
            actual = {}
            for key in ('seed', 'steps', 'scale', 'sampler', 'width', 'height'):
                value = result.get(key)
                if (key != 'sampler' and type(value) in (int, float) and abs(value) < 1e20) or (key == 'sampler' and isinstance(value, str) and len(value) <= 128):
                    actual[key] = value
            # Managed requests have an intent receipt. Persist before any further tool call.
            from stylelora.generation.artifacts import record_submission
            if (request.save_path.parent / 'attempt.json').exists():
                record_submission(request.save_path.parents[4], request,
                    GenerationResult(resource, Path(path) if path else None, metadata_stripped=stripped))
            if path is None and isinstance(resource, str) and resource:
                # A fresh controlled export; any unknown result remains behind the barrier.
                result = await self._call('save_generated_image', {'resource_id': resource, 'save_path': str(request.save_path)}, 'write')
                path = result.get('saved_path')
            if not isinstance(path, str) or Path(path).resolve() != request.save_path.resolve():
                raise BackendError('artifact:ownership_conflict', 'submit', 'unknown')
            self.inflight = False
            return GenerationResult(resource, request.save_path, metadata_stripped=stripped, actual=actual)
        except BackendError:
            raise
        except Exception:
            raise BackendError('mcp:write_outcome_unknown', 'submit', 'unknown') from None

    async def cancel_preparation(self, preparation):
        self._prepared(preparation)
        await self._call('cancel_generation_preparation', {'preparation_id': preparation.handle}, 'write')
        self.preparations.pop(preparation.handle)

    async def recover_artifact(self, request, receipt):
        """Export only the durably bound resource; never submit a generation."""
        if receipt.phase != 'submitted' or not receipt.resource_id or request.save_path.exists():
            return
        if receipt.backend_instance != self.instance:
            raise BackendError('artifact:ownership_conflict', 'recovery')
        result = await self._call('save_generated_image',
            {'resource_id': receipt.resource_id, 'save_path': str(request.save_path)}, 'write')
        if result.get('saved_path') != str(request.save_path):
            raise BackendError('artifact:ownership_conflict', 'recovery', 'unknown')

    async def reconcile(self, item, receipt=None):
        status = await self._call('get_generation_status')
        recent = await self._call('get_recent_images')
        if self.evidence_decoder is not None:
            # Decoder comes only from an audited version contract, never from a server response.
            return self.evidence_decoder(item, receipt, status, recent)
        return RecoveryEvidence(item.item_id, item.attempts, 'unknown', reason='recovery:unknown')

    async def close(self):
        if self.handle is not None:
            await self.restore(self.handle)
        await close_session(self.session)
