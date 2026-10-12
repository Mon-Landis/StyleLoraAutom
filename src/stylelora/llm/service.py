"""Global async service: one shared gate, frozen connections and bounded retries."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import io
import json
from pathlib import Path
import time
from uuid import uuid4

from PIL import Image

from .adapters import JsonAdapter, TransportFailure, adapter_for
from .config import ConfigStore
from .images import encode_image
from .json_output import repair_json_text, validate_json_reply
from .secrets import SecretStore
from .types import (CancelToken, DEFAULT_TIERS, ImageInput, JsonReply, LlmFailure,
                    LlmReply, LlmRequest, ModelConfig, RepairBudget, RoleBinding)


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _retry_after(value) -> float:
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0


def capability_fixture() -> bytes:
    """Fixed red fixture; capability tests require an exact semantic answer."""
    output = io.BytesIO()
    Image.new('RGB', (32, 32), (255, 0, 0)).save(output, 'PNG')
    return output.getvalue()


class LlmService:
    def __init__(self, config: ConfigStore | None = None, secrets: SecretStore | None = None,
                 *, concurrency: int = 2, transport=None, validator=None,
                 audit_path: Path | None = None, backoff_s: float = 1.0):
        if concurrency < 1 or backoff_s < 0:
            raise ValueError('Invalid service limits')
        self.config = config or ConfigStore()
        self.secrets = secrets or SecretStore()
        self._gate = asyncio.Semaphore(concurrency)
        self._transport = transport
        self._validator = validator
        self._audit_path = Path(audit_path) if audit_path is not None else self.config.path.with_suffix('.audit.jsonl')
        self.audit_warnings = 0
        self._backoff = backoff_s
        self._active = {}
        self._frozen = {}
        self._cursor = {}
        self._cooldowns = {}

    def _adapter(self, protocol):
        return JsonAdapter(protocol, self._transport) if self._transport and protocol in (
            'openai_chat', 'openai_responses', 'anthropic_messages') else adapter_for(protocol)

    async def resolve_role(self, role) -> RoleBinding | None:
        binding = self.config.resolve_role(role)
        if binding is not None:
            endpoint = self.config.endpoint(binding.binding_id)
            # Each returned binding owns its connection snapshot even if config changes.
            self._frozen[id(binding)] = (binding, endpoint)
        return binding

    def _endpoint(self, binding):
        frozen = self._frozen.get(id(binding))
        endpoint = frozen[1] if frozen and frozen[0] is binding else self.config.endpoint(binding.binding_id)
        if endpoint.protocol != binding.protocol or not any(m.name == binding.model for m in endpoint.models):
            raise LlmFailure('llm:model_mismatch', outcome='not_sent')
        return endpoint

    async def cancel(self, request_id: str) -> None:
        state = self._active.get(request_id)
        if state:
            state.cancel()

    @staticmethod
    def _check_cancel(internal, external, sent=False):
        if internal.canceled or (external is not None and external.canceled):
            raise LlmFailure('llm:canceled', outcome='unknown' if sent else 'not_sent')

    async def _wait(self, awaitable, internal, external, *, timeout=None, sent=False):
        task = asyncio.ensure_future(awaitable)
        start = time.monotonic()
        try:
            while True:
                self._check_cancel(internal, external, sent)
                remaining = None if timeout is None else timeout - (time.monotonic() - start)
                if remaining is not None and remaining <= 0:
                    raise TransportFailure('llm:timeout')
                done, _ = await asyncio.wait((task,), timeout=min(0.05, remaining) if remaining is not None else 0.05)
                if done:
                    self._check_cancel(internal, external, sent)
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _pause(self, seconds, deadline, internal, external, sent):
        remaining = deadline - time.monotonic()
        if seconds >= remaining:
            raise LlmFailure('llm:retry_deadline', outcome='unknown' if sent else 'not_sent')
        await self._wait(asyncio.sleep(seconds), internal, external, sent=sent)

    def _select_token(self, endpoint):
        refs = endpoint.token_refs
        if not refs:
            raise LlmFailure('llm:secret_missing', outcome='not_sent')
        cursor = self._cursor.get(endpoint.endpoint_id, 0) % len(refs)
        now = time.monotonic()
        for offset in range(len(refs)):
            index = (cursor + offset) % len(refs)
            if self._cooldowns.get((endpoint.endpoint_id, refs[index]), 0) <= now:
                self._cursor[endpoint.endpoint_id] = (index + 1) % len(refs)
                return index, 0
        earliest = min(self._cooldowns[(endpoint.endpoint_id, ref)] for ref in refs)
        return None, max(0, earliest - now)

    def _audit(self, request, endpoint, index, attempt, tier, *, code, outcome, duration=0,
               attempt_id=None, credentials=()):
        # Strict allowlist; never serialize DTOs, errors, bodies, URLs or token references.
        row = {'utc': _utc(), 'request_id': request.request_id, 'attempt_id': attempt_id or uuid4().hex,
               'role': request.binding.role, 'endpoint_id': endpoint.endpoint_id,
               'protocol': endpoint.protocol, 'model': request.binding.model,
               'token_index': index, 'attempt': attempt, 'tier': tier.max_pixels if tier else None,
               'code': code, 'outcome': outcome, 'duration_ms': duration}
        try:
            text = json.dumps(row, ensure_ascii=False)
            for secret in credentials:
                if secret:
                    text = text.replace(secret, '********')
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open('a', encoding='utf-8') as stream:
                stream.write(text + '\n')
        except OSError:
            self.audit_warnings += 1
        return row['attempt_id']

    async def complete(self, request: LlmRequest) -> LlmReply:
        if request.request_id in self._active:
            raise LlmFailure('llm:duplicate_request', outcome='not_sent')
        if (len(request.images) > 1 or (request.images and request.image_input)
                or request.timeout_s <= 0 or request.transport_retry_limit < 0
                or request.timeout_policy not in ('fail_unknown', 'vision_downgrade')):
            raise LlmFailure('llm:request_invalid', outcome='not_sent')
        internal = CancelToken()
        self._active[request.request_id] = internal
        acquired = False
        sent = False
        async def acquire():
            nonlocal acquired
            await self._gate.acquire()
            acquired = True

        try:
            self._check_cancel(internal, request.cancel)
            endpoint = self._endpoint(request.binding)
            await self._wait(acquire(), internal, request.cancel)
            deadline = time.monotonic() + request.timeout_s * (request.transport_retry_limit + 1)
            tiers = request.tiers or DEFAULT_TIERS
            if (len(tiers) > 3 or not tiers or any(t.max_pixels > 2_000_000 for t in tiers)
                    or any(a.max_pixels <= b.max_pixels for a, b in zip(tiers, tiers[1:]))):
                raise LlmFailure('llm:image_tier_invalid', outcome='not_sent')
            tier_index = 0
            image = (encode_image(request.image_input, tiers[0]) if request.image_input else
                     request.images[0] if request.images else None)
            model = next(m for m in endpoint.models if m.name == request.binding.model)
            if image and not model.supports_images:
                raise LlmFailure('llm:image_unsupported', outcome='not_sent')
            adapter = self._adapter(endpoint.protocol)
            index = None
            credentials = []
            for ref in endpoint.token_refs:
                try:
                    credentials.append(self.secrets.resolve_secret(ref))
                except LlmFailure:
                    pass
            for attempt in range(request.transport_retry_limit + 1):
                self._check_cancel(internal, request.cancel, sent)
                while index is None:
                    index, wait = self._select_token(endpoint)
                    if index is None:
                        await self._pause(wait, deadline, internal, request.cancel, sent)
                token = self.secrets.resolve_secret(endpoint.token_refs[index])
                credentials.append(token)
                timeout = min(request.timeout_s, deadline - time.monotonic())
                if timeout <= 0:
                    raise LlmFailure('llm:retry_deadline', outcome='unknown' if sent else 'not_sent')
                call = replace(request, timeout_s=timeout)
                start = time.monotonic()
                attempt_id = self._audit(request, endpoint, index, attempt + 1,
                                         image.tier if image else None, code='start', outcome='pending',
                                         credentials=credentials)
                sent = True
                try:
                    reply = await self._wait(adapter.complete(endpoint, token, call, image), internal,
                                             request.cancel, timeout=timeout, sent=True)
                    if reply.actual_model != model.name:
                        raise LlmFailure('llm:model_mismatch')
                    text = reply.text
                    for secret in credentials:
                        text = text.replace(secret, '********')
                    self._audit(request, endpoint, index, attempt + 1, image.tier if image else None,
                                code='ok', outcome='completed', duration=int((time.monotonic() - start) * 1000),
                                attempt_id=attempt_id, credentials=credentials)
                    usage = {k: v for k, v in (reply.usage or {}).items()
                             if k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens')
                             and type(v) is int and v >= 0}
                    return replace(reply, text=text, usage=usage or None, request_id=request.request_id, attempts=attempt + 1,
                                   actual_tier=image.tier if image else None)
                except LlmFailure as exc:
                    self._audit(request, endpoint, index, attempt + 1, image.tier if image else None,
                                code=exc.code, outcome=exc.outcome,
                                duration=int((time.monotonic() - start) * 1000),
                                attempt_id=attempt_id, credentials=credentials)
                    if not isinstance(exc, TransportFailure):
                        raise
                    if exc.status == 429:
                        cooldown = max(_retry_after(exc.retry_after), self._backoff * 2 ** attempt)
                        self._cooldowns[(endpoint.endpoint_id, endpoint.token_refs[index])] = time.monotonic() + cooldown
                        index = None
                    if attempt >= request.transport_retry_limit:
                        raise
                    if exc.code == 'llm:timeout':
                        if (request.timeout_policy != 'vision_downgrade' or not request.image_input
                                or tier_index + 1 >= len(tiers)):
                            raise
                        tier_index += 1
                        image = encode_image(request.image_input, tiers[tier_index])
                    elif not exc.retryable:
                        raise
                    elif exc.status != 429:
                        await self._pause(self._backoff * 2 ** attempt, deadline, internal, request.cancel, sent)
            raise LlmFailure('llm:retry_exhausted', outcome='unknown')
        except asyncio.CancelledError:
            raise LlmFailure('llm:canceled', outcome='unknown' if sent else 'not_sent') from None
        except LlmFailure as exc:
            # Plugins cannot expose an HTTP body, URL or credential through messages.
            raise LlmFailure(exc.code, exc.retryable, exc.outcome) from None
        except Exception:
            raise LlmFailure('llm:internal_error', outcome='unknown' if sent else 'not_sent') from None
        finally:
            if acquired:
                self._gate.release()
            self._active.pop(request.request_id, None)

    async def complete_json(self, request: LlmRequest, *, repair_limit: int = 1) -> JsonReply:
        budget = request.repair_budget or RepairBudget(limit=min(1, max(0, repair_limit)))
        reply = await self.complete(request)
        total_attempts = reply.attempts
        repair_used = False
        while True:
            try:
                text = reply.text
                try:
                    value = validate_json_reply(text, request.response_schema, validator=self._validator)
                except LlmFailure as exc:
                    if exc.code != 'llm:invalid_json':
                        raise
                    recovered = repair_json_text(text)
                    value = validate_json_reply(recovered, request.response_schema, validator=self._validator)
                    repair_used = repair_used or recovered != text.strip()
                return JsonReply(value, replace(reply, attempts=total_attempts), repair_used)
            except LlmFailure as exc:
                if (exc.code != 'llm:invalid_json' or repair_limit <= 0
                        or not budget.claim()):
                    raise
                # Do not replay untrusted reply content. Keep the same image tier.
                image = (encode_image(request.image_input, reply.actual_tier)
                         if request.image_input and reply.actual_tier else None)
                fixed = replace(request,
                                system_text=request.system_text + '\nReturn only JSON matching the supplied schema.',
                                payload={'validation_error': 'JSON contract validation failed',
                                         'schema': dict(request.response_schema)},
                                image_input=None, images=(image,) if image else request.images,
                                timeout_policy='fail_unknown', repair_budget=budget)
                reply = await self.complete(fixed)
                total_attempts += reply.attempts
                repair_used = True

    async def probe_models(self, endpoint_id: str) -> tuple[ModelConfig, ...]:
        endpoint = self.config.endpoint(endpoint_id)
        try:
            index, wait = self._select_token(endpoint)
            if index is None:
                return endpoint.models
            async with self._gate:
                models = await asyncio.wait_for(self._adapter(endpoint.protocol).list_models(
                    endpoint, self.secrets.resolve_secret(endpoint.token_refs[index])), 300)
        except (LlmFailure, asyncio.TimeoutError):
            return endpoint.models
        # Retain configured models absent from provider discovery.
        merged = tuple(models) + tuple(m for m in endpoint.models if m.name not in {v.name for v in models})
        return self.config.save_endpoint(replace(endpoint, models=merged),
                                         expected_revision=endpoint.revision).models

    async def test_image_capability(self, endpoint_id: str, model: str, *, fixture: bytes | None = None,
                                    cancel: CancelToken | None = None) -> ModelConfig:
        fixture = capability_fixture() if fixture is None else fixture
        if fixture != capability_fixture():
            raise LlmFailure('llm:fixture_invalid', outcome='not_sent')
        endpoint = self.config.endpoint(endpoint_id)
        previous = next((m for m in endpoint.models if m.name == model), None)
        if previous is None:
            raise LlmFailure('llm:model_missing', outcome='not_sent')
        binding = RoleBinding(endpoint_id, 'vision', model, endpoint.protocol, endpoint.revision)
        test_endpoint = replace(endpoint, models=tuple(replace(m, supports_images=True) if m.name == model else m
                                                       for m in endpoint.models))
        self._frozen[id(binding)] = (binding, test_endpoint)
        try:
            reply = await self.complete(LlmRequest(uuid4().hex, binding,
                'Identify the solid color in the image. Reply with exactly one lowercase English color word.',
                {'question': 'What color is the image?'}, {}, cancel=cancel,
                image_input=ImageInput(data=fixture), transport_retry_limit=0))
            if reply.text.strip().lower() != 'red':
                raise LlmFailure('llm:capability_test_failed')
            tested = replace(previous, supports_images=True, capability_source='tested', tested_at=_utc())
            self.config.save_endpoint(replace(endpoint, models=tuple(tested if m.name == model else m
                                                                    for m in endpoint.models)),
                                      expected_revision=endpoint.revision)
            return tested
        finally:
            self._frozen.pop(id(binding), None)
