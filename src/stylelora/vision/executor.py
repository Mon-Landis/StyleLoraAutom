"""R2/R5 execution. Product templates are supplied by content hash."""
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
from uuid import uuid4
from PIL import __version__ as PILLOW_VERSION
from stylelora.llm.types import (LlmRequest, ImageInput, DEFAULT_TIERS, ImageTier,
                               LlmFailure, RepairBudget)
from stylelora.llm.images import encode_image
from stylelora.llm.json_output import validate_json_reply
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db.repositories import images
from stylelora.project_db.types import AnnotationInput, RatingValue, Tag
from stylelora.project_db.validation import now
from stylelora.project_db.errors import StateConflict
from stylelora.executors.cache import cache_keys
from stylelora.executors.types import ExecutionResult, Unavailable
from stylelora.tagging.normalization import normalize_tag
from .rating import RatingRouter, decision_for, r0_applies

R2_SCHEMA = {'type': 'object', 'required': ['vision_tags', 'vision_description'], 'additionalProperties': False,
            'properties': {'vision_tags': {'type': 'array', 'items': {'type': 'string'}},
                           'vision_description': {'type': 'string'}}}
R0_SCHEMA = {'type': 'object', 'required': ['status', 'reason'], 'additionalProperties': False,
            'properties': {'status': {'const': 'refused_nsfw'}, 'reason': {'type': 'string'}}}
R5_SCHEMA = {'oneOf': [
    {'type': 'object', 'required': ['rating', 'reason'], 'additionalProperties': False,
     'properties': {'rating': {'enum': ['general', 'sensitive', 'questionable', 'explicit', 'uncertain']}, 'reason': {'type': 'string'}}}, R0_SCHEMA]}
VISION_SCHEMA = {'oneOf': [R2_SCHEMA, R0_SCHEMA]}
CONFIG_ERRORS = {'llm:unauthorized', 'llm:secret_unavailable', 'llm:binding_unavailable',
                 'llm:model_mismatch', 'llm:role_empty', 'llm:image_unsupported'}


def _tags(values):
    return list(dict.fromkeys(normalize_tag(value) for value in values if normalize_tag(value)))


class SkillStore:
    def __init__(self, texts=()):
        self.texts = {hashlib.sha256(text.encode()).hexdigest(): text for text in texts}

    def get(self, key):
        if key not in self.texts:
            raise Unavailable('skill_unavailable')
        text = self.texts[key]
        if hashlib.sha256(text.encode()).hexdigest() != key:
            raise Unavailable('skill_unavailable')
        return text


def build_vision_request(context, decision, binding, *, system_text, budget=None, tier=None):
    evidence = context.evidence
    params = context.snapshot['params']
    payload = {'tags': _tags([t.tag for t in evidence.original_tags + evidence.wd_tags]),
               'dataset_mode_policy': params.get('dataset_mode_policy', 'style'),
               'rating_verified': decision.verified,
               'rating_policy': {'rating': decision.rating, 'route': decision.route,
                                 'r0_applies': r0_applies(decision)}}
    tiers = tuple(ImageTier(**value) for value in params.get('tiers', [asdict(t) for t in DEFAULT_TIERS]))
    if tier is not None:
        tiers = tiers[tiers.index(tier):]
    rules = {'protected_tags': params.get('protected_tags', []),
             'dataset_mode_policy': payload['dataset_mode_policy']}
    system = system_text + '\nR0 applies ONLY on the normal route when rating_verified=false.\n'
    system += json.dumps(rules, ensure_ascii=False) + '\nReturn exactly JSON matching: ' + json.dumps(VISION_SCHEMA)
    return LlmRequest(uuid4().hex, binding, system, payload, VISION_SCHEMA,
        timeout_s=context.snapshot['timeout'], transport_retry_limit=context.snapshot['retries'],
        image_input=ImageInput(path=context.image.path), cancel=context.cancel,
        timeout_policy='vision_downgrade', tiers=tiers, sampling=params.get('sampling', {}),
        repair_budget=budget)


async def strict_json(llm, request):
    """Use complete: F15 rejects fences, unlike the shared optional text repair."""
    budget = request.repair_budget or RepairBudget()
    reply = await llm.complete(request)
    attempts = reply.attempts
    while True:
        try:
            return validate_json_reply(reply.text, request.response_schema), replace(reply, attempts=attempts)
        except LlmFailure as exc:
            if exc.code != 'llm:invalid_json' or not budget.claim():
                raise
            tier = reply.actual_tier or (request.tiers or DEFAULT_TIERS)[0]
            image = encode_image(request.image_input, tier) if request.image_input else None
            request = replace(request, request_id=uuid4().hex,
                payload={'validation_error': 'JSON contract validation failed', 'schema': request.response_schema},
                image_input=None, images=(image,) if image else request.images,
                timeout_policy='fail_unknown', repair_budget=budget)
            reply = await llm.complete(request)
            attempts += reply.attempts


async def persist_rating(project, image_id, *, expected, value):
    row = await asyncio.wrap_future(submit_write(project,
        lambda db: images.set_rating(db, image_id, expected=expected, value=value).value))
    return images.rating_of(row)


class VisionExecutor:
    name = 'llm_vision'

    def __init__(self, llm, *, skills, project=None, router=None, rating_snapshot=None):
        self.llm, self.skills, self.project = llm, skills, project
        self.router = router or RatingRouter()
        self.rating_snapshot = rating_snapshot
        self._bindings = {}

    def templates(self, snapshot):
        hashes = snapshot['params'].get('skill_hashes', {})
        return self.skills.get(hashes.get('R0')) + '\n' + self.skills.get(hashes.get('R2'))

    async def validate(self, snapshot):
        self.templates(snapshot)
        if snapshot['routing'].get('use_rating_model'):
            self.skills.get(snapshot['params'].get('skill_hashes', {}).get('R5'))
        roles = snapshot.get('roles', {'vision': {'model': snapshot['model'], 'protocol': snapshot['protocol']}})
        for role, frozen in roles.items():
            binding = await self.llm.resolve_role(role)
            if binding is None or (binding.model, binding.protocol) != (frozen['model'], frozen['protocol']):
                raise Unavailable('binding_unavailable')
            self._bindings[(role, frozen['model'], frozen['protocol'])] = binding
        return roles

    async def binding(self, context, role):
        roles = context.snapshot.get('roles', {'vision': {'model': context.snapshot['model'], 'protocol': context.snapshot['protocol']}})
        if role not in roles:
            return None
        frozen = roles[role]
        binding = self._bindings.get((role, frozen['model'], frozen['protocol']))
        if binding is None:
            binding = await self.llm.resolve_role(role)
        if binding is None or (binding.model, binding.protocol) != (roles[role]['model'], roles[role]['protocol']):
            raise Unavailable('binding_unavailable')
        return binding

    async def _save(self, context, decision, *, sent=False):
        context.cancel.raise_if_cancelled()
        if decision.rating is None:
            return context, decision
        existing = context.rating
        if existing and (existing.rating, existing.source, existing.verified) == (decision.rating, decision.source, decision.verified):
            return context, decision
        value = RatingValue(decision.rating, decision.source, decision.verified, now())
        if self.project is not None:
            try:
                value = await persist_rating(self.project, context.image.image_id, expected=existing, value=value)
            except StateConflict:
                if sent:
                    raise Unavailable('rating_conflict')
                with read_snapshot(self.project) as db:
                    value = images.rating_of(images.get_image(db, context.image.image_id))
                if not value or not value.verified:
                    raise Unavailable('rating_conflict')
                decision = decision_for(value.rating, value.verified, value.source)
        return replace(context, rating=value), decision

    async def decide_rating(self, context):
        local = self.router.local(context)
        if local:
            return await self._save(context, local)
        if context.snapshot['routing'].get('use_rating_model'):
            binding = await self.binding(context, 'rating')
            if binding is None:
                raise Unavailable('binding_unavailable')
            text = self.skills.get(context.snapshot['params'].get('skill_hashes', {}).get('R5'))
            text += '\nR0 applies only on normal route with rating_verified=false. Return JSON: ' + json.dumps(R5_SCHEMA)
            payload = {'tags': _tags([t.tag for t in context.evidence.original_tags + context.evidence.wd_tags]),
                       'natural_language': context.evidence.original_nl, 'rating_verified': False,
                       'rating_policy': {'route': 'normal'}}
            capability = context.snapshot['routing'].get('rating_capability', {})
            with_image = capability.get('supports_images') and capability.get('capability_source') == 'tested'
            if with_image or payload['tags'] or payload['natural_language']:
                request = LlmRequest(uuid4().hex, binding, text, payload, R5_SCHEMA,
                    timeout_s=context.snapshot['timeout'], transport_retry_limit=context.snapshot['retries'],
                    image_input=ImageInput(path=context.image.path) if with_image else None,
                    timeout_policy='vision_downgrade' if with_image else 'fail_unknown',
                    cancel=context.cancel, repair_budget=RepairBudget())
                try:
                    value, reply = await strict_json(self.llm, request)
                    context.cancel.raise_if_cancelled()
                    if reply.actual_model != binding.model:
                        raise Unavailable('model_mismatch')
                    if value.get('status') == 'refused_nsfw':
                        return await self._save(context, decision_for('questionable', True, 'refusal'))
                    if value['rating'] != 'uncertain':
                        return await self._save(context, decision_for(value['rating'], True, 'model'))
                except LlmFailure as exc:
                    if exc.code in CONFIG_ERRORS:
                        raise Unavailable(exc.code.split(':')[-1]) from exc
        fallback = context.snapshot['routing'].get('nsfwfallback', self.router.fallback)
        decision = decision_for('questionable', False, 'fallback') if fallback else RatingRouter().default()
        return await self._save(context, decision)

    async def execute(self, context):
        text = self.templates(context.snapshot)
        context, decision = await self.decide_rating(context)
        budget, tier = RepairBudget(), None
        for reroute in range(2):
            role = 'vision_nsfw' if decision.route == 'nsfw' else 'vision'
            binding = await self.binding(context, role)
            if binding is None:
                return ExecutionResult('skipped' if role == 'vision_nsfw' else 'failed', 'role_empty', rating=context.rating)
            request = build_vision_request(context, decision, binding, system_text=text, budget=budget, tier=tier)
            value, reply = await strict_json(self.llm, request)
            context.cancel.raise_if_cancelled()
            if reply.actual_model != binding.model:
                raise Unavailable('model_mismatch')
            tier = reply.actual_tier or request.tiers[0]
            if value.get('status') == 'refused_nsfw':
                context, refusal = await self._save(context, decision_for('questionable', True, 'refusal'), sent=True)
                if decision.route == 'nsfw' or reroute:
                    return ExecutionResult('skipped', 'refusal_repeated', rating=context.rating)
                decision = refusal
                continue
            content = ' '.join(value['vision_tags']) + ' ' + value['vision_description']
            params = context.snapshot['params']
            if params.get('dataset_mode_policy', 'style') == 'style':
                if any(word.lower() in content.lower() for word in params.get('forbidden_terms', [])):
                    return ExecutionResult('failed', 'contract_violation', rating=context.rating)
                if any(word.lower() in content.lower() for word in params.get('review_terms', [])):
                    return ExecutionResult('failed', 'review_required', rating=context.rating)
            actual = {'role': role, 'protocol': binding.protocol, 'route': decision.route,
                      'route_reason': decision.reason, 'rating': decision.rating,
                      'rating_verified': decision.verified, 'rating_source': decision.source,
                      'tier': asdict(tier), 'encoder_version': 'pillow-' + PILLOW_VERSION,
                      'evidence_digest': context.evidence.digest,
                      'sampling': params.get('sampling', {}), 'attempts': reply.attempts}
            keys = cache_keys(context, actual_model=reply.actual_model, actual_params=actual)
            saved = {'schema_version': 1, 'requested': params, 'actual': actual,
                     'evidence_ids': [ref.annotation_id for ref in context.evidence.refs]}
            contents = (('tags', tuple(Tag(tag) for tag in _tags(value['vision_tags']))),
                        ('nl', value['vision_description']))
            return ExecutionResult('done', annotations=tuple(AnnotationInput(context.image.image_id,
                'llm', kind, data, model_name=reply.actual_model, params=saved,
                cache_key=keys['llm', kind]) for kind, data in contents), rating=context.rating)


async def reclassify(project, scope, *, executor, cancel):
    from stylelora.executors.scope import resolve_scope
    from stylelora.executors.runner import context_in_snapshot
    if executor.rating_snapshot is None:
        raise Unavailable('rating_snapshot_required')
    ids = resolve_scope(project, scope, mode='all')
    cancel.raise_if_cancelled()
    await asyncio.wrap_future(submit_write(project, lambda db: images.clear_recomputable_ratings(db, ids)))
    processed = []
    for identity in ids:
        cancel.raise_if_cancelled()
        with read_snapshot(project) as db:
            row = images.get_image(db, identity)
            context = context_in_snapshot(project, db, identity, executor.rating_snapshot, cancel)
        if row.file_missing or not context.image.path.is_file():
            continue
        await executor.decide_rating(context)
        processed.append(identity)
    return tuple(processed)
