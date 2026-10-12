"""Launcher settings lease and tool mapping. Unverified remote evidence stays unknown."""
import os
import json
import threading
from pathlib import Path
from uuid import uuid4
from ..types import (BackendError, ProfileHandle, Preparation, GenerationResult, digest, plain)
from .client import connect, close_session
from .discovery import discover
from .compatibility import verify_compatibility, TRUSTED_MANIFEST
from stylelora.generation.profile import validate_profile, profile_values, validate_value
from stylelora.project_db.migrations import atomic_json
from stylelora.project_db.types import RecoveryEvidence

_leases = {}
_guard = threading.Lock()


class LauncherBackend:
    def __init__(self, session, capabilities, *, journal_dir=None, manifest=TRUSTED_MANIFEST,
                 account_provider=None, evidence_decoder=None):
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
        result = await self.session.call(name, arguments or {}, kind=kind)
        self._check_connection_identity()
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
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.journal, {'schema_version': 1, **plain(handle)})

    async def recover_settings(self):
        """Explicit read-first recovery; never adopts another Launcher identity."""
        if not self.journal.exists():
            return
        try:
            data = json.loads(self.journal.read_text(encoding='utf-8'))
            if data.pop('schema_version') != 1:
                raise ValueError()
            handle = ProfileHandle(**data)
            if handle.backend_instance != self.instance:
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError):
            raise BackendError('profile:restore_conflict', 'restore') from None
        with _guard:
            owner = _leases.get(self.lease_key)
            if owner is not None and owner is not self:
                raise BackendError('profile:restore_pending', 'restore')
            _leases[self.lease_key] = self
        self.handle = handle
        await self.restore(handle)

    async def apply_profile(self, profile):
        validate_profile(profile, self._caps)
        if self.handle is not None or self.journal.exists():
            raise BackendError('profile:restore_pending', 'apply')
        with _guard:
            if self.lease_key in _leases:
                raise BackendError('profile:restore_pending', 'apply')
            _leases[self.lease_key] = self
        try:
            current = await self._settings()
            applied = profile_values(profile)
            if not set(applied) <= set(current):
                raise BackendError('profile:drift', 'apply')
            # Only keys actually changed are restored; read-only/one-shot fields never enter the log.
            changed = {k: v for k, v in applied.items() if current[k] != v}
            handle = ProfileHandle(uuid4().hex, {k: current[k] for k in changed}, changed,
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
            if self.handle is None:
                with _guard:
                    _leases.pop(self.lease_key, None)
            raise

    async def restore(self, handle):
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
        self.journal.unlink(missing_ok=True)
        self.handle = None
        with _guard:
            _leases.pop(self.lease_key, None)

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
            if path is None and isinstance(resource, str) and resource:
                # A fresh controlled export; any unknown result remains behind the barrier.
                result = await self._call('save_generated_image', {'resource_id': resource, 'save_path': str(request.save_path)}, 'write')
                path = result.get('saved_path')
            if not isinstance(path, str) or Path(path).resolve() != request.save_path.resolve():
                raise BackendError('artifact:ownership_conflict', 'submit', 'unknown')
            self.inflight = False
            return GenerationResult(resource, request.save_path, metadata_stripped=result.get('metadata_stripped'), actual=result)
        except BackendError:
            raise
        except Exception:
            raise BackendError('mcp:write_outcome_unknown', 'submit', 'unknown') from None

    async def cancel_preparation(self, preparation):
        self._prepared(preparation)
        await self._call('cancel_generation_preparation', {'preparation_id': preparation.handle}, 'write')
        self.preparations.pop(preparation.handle)

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
