"""Strict boundaries and R1 acceptance, sharing the product tag tokenizer."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from uuid import UUID
from functools import lru_cache
from stylelora.tagging.normalization import normalize_tag as shared_normalize
from stylelora.tagging.tokenizer import syntax_of, tokenize_prompt
from stylelora.tagging.catalog import LocalCatalog
from .types import *


def require(condition, code='schema:invalid'):
    if not condition:
        raise PromptError(code)


def integer(value, minimum=0, maximum=None):
    require(type(value) is int and value >= minimum and (maximum is None or value <= maximum))


def text(value, *, empty=False):
    require(isinstance(value, str) and (empty or bool(value.strip())))


def identifier(value):
    try:
        require(isinstance(value, str) and str(UUID(value)) == value)
    except (ValueError, TypeError, AttributeError):
        raise PromptError('schema:invalid') from None
    return value


def normalize_tag(tag, catalog=None):
    text(tag)
    # Placeholder identifiers are syntax, not tag words: retain their underscores.
    key = ' '.join(shared_normalize(tag).split())
    for placeholder in re.findall(r'{{[^{}]*}}', tag):
        key = key.replace(' '.join(shared_normalize(placeholder).split()), placeholder)
    if catalog is not None:
        hit = catalog.lookup(key)
        if hit.match in ('known', 'alias'):
            key = ' '.join(shared_normalize(hit.canonical).split())
    return key


def tag_key(entry, catalog=None):
    tags = tuple(sorted({normalize_tag(t, catalog) for t in entry.tags}))
    return tags or ('\x00nl:'+' '.join((entry.natural_language or '').split()),)


def entry_key(entry, catalog=None):
    return tag_key(entry, catalog), ' '.join((entry.natural_language or '').split()), entry.aspect


def strict_loads(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            require(key not in obj)
            obj[key] = value
        return obj
    def constant(_):
        raise PromptError('schema:invalid')
    try:
        if isinstance(data, bytes):
            data = data.decode('utf-8-sig')
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise PromptError('schema:invalid') from None


def construct(cls, data):
    require(isinstance(data, Mapping) and not set(data) - {f.name for f in fields(cls)})
    try:
        return cls(**data)
    except TypeError:
        raise PromptError('schema:invalid') from None


def validate_dto(v):
    # Common scalar fields; bool never counts as an integer.
    for name in ('schema_version', 'revision', 'index', 'seq', 'target_count', 'random_seed',
                 'requested', 'available', 'base_revision', 'inserted', 'duplicates', 'count'):
        if hasattr(v, name):
            integer(getattr(v, name))
    if hasattr(v, 'schema_version'):
        versions = (1, 2) if isinstance(v, (PromptDictionary, FeatureDictionary)) else (1,)
        require(v.schema_version in versions, 'newer_version' if v.schema_version > max(versions) else 'schema:invalid')
    for name in ('dictionary_id', 'task_id', 'preview_id'):
        if hasattr(v, name):
            identifier(getattr(v, name))
    for name in ('name', 'dimension', 'tag', 'digest', 'code'):
        if hasattr(v, name):
            text(getattr(v, name))
    for name in ('aspect', 'aspect_hint'):
        if hasattr(v, name):
            require(getattr(v, name) is None or getattr(v, name) in ASPECTS)
    if hasattr(v, 'rating'):
        require(v.rating is None or v.rating in RATINGS)
    for name in ('nl_enabled', 'enabled', 'nsfwfallback', 'use_rating_model', 'verified', 'ready',
                 'live_features', 'optimize', 'coverage_enabled'):
        if hasattr(v, name):
            require(type(getattr(v, name)) is bool)
    if isinstance(v, (CompositionRequest, CompositionOptions)):
        require(v.composition_mode in ('normal', 'scenery_first'))
        require(isinstance(v.tag_range, tuple) and len(v.tag_range) == 2)
        integer(v.tag_range[0], 1, 100)
        integer(v.tag_range[1], v.tag_range[0], 100)
    if isinstance(v, CompositionRequest):
        require(v.rating_policy in ('sfw', 'nsfw') and isinstance(v.features, Mapping) and bool(v.features))
        for dim, tags in v.features.items():
            text(dim)
            strings(tags, nonempty=True)
    if isinstance(v, (PromptEntry, ComposerResult)):
        strings(v.tags, nonempty=isinstance(v, PromptEntry) and not (isinstance(v.natural_language, str) and bool(v.natural_language.strip())))
        require(v.natural_language is None or isinstance(v.natural_language, str))
        if isinstance(v, PromptEntry):
            object.__setattr__(v, 'tags', tuple(normalize_tag(t) for t in v.tags))
            object.__setattr__(v, 'natural_language', (v.natural_language or '').strip() or None)
    if isinstance(v, FeatureCandidate):
        require(v.source in ('dictionary', 'launcher', 'manual'))
        object.__setattr__(v, 'tag', normalize_tag(v.tag))
    if isinstance(v, (FeatureDictionary, PromptDictionary)):
        integer(v.revision, 1)
        if isinstance(v, FeatureDictionary):
            require(isinstance(v.dimensions, Mapping))
            for dim, candidates in v.dimensions.items():
                text(dim)
                require(isinstance(candidates, tuple))
                require(all(isinstance(c, FeatureCandidate) and c.dimension == dim for c in candidates))
        else:
            require(v.category == 'full_content', 'dictionary:category_unsupported')
            require(isinstance(v.entries, tuple) and all(isinstance(e, PromptEntry) for e in v.entries))
    if isinstance(v, ConstraintSpec):
        for n in ('minimum_ratio', 'maximum_ratio'):
            x = getattr(v, n)
            require(x is None or type(x) in (float, int) and math.isfinite(x) and 0 <= x <= 1)
        for n in ('minimum_count', 'maximum_count'):
            if getattr(v, n) is not None:
                integer(getattr(v, n))
    if isinstance(v, Predicate):
        strings(v.all_tags)
        strings(v.none_tags)
    if isinstance(v, CoveragePlan):
        integer(v.target_count, 1, 10000)
        integer(v.random_seed, 0, 2**64 - 1)
        require(v.algorithm_version == 'coverage-v1')
        require(v.compatibility_version in (None, 'default-v1'))
        strings(v.primary_dimensions, nonempty=True)
        require(2 <= len(v.primary_dimensions) <= 3 and len(set(v.primary_dimensions)) == len(v.primary_dimensions))
        require(isinstance(v.cell_values, Mapping) and set(v.cell_values) == set(v.primary_dimensions))
        for values in v.cell_values.values():
            strings(values, nonempty=True)
            require(len({normalize_tag(t) for t in values}) == len(values))
        require(all(isinstance(c, ConstraintSpec) for c in v.constraints))
        require(all(isinstance(p, Predicate) for p in v.exclude))
        require(isinstance(v.cell_weights, Mapping))
        for k, weight in v.cell_weights.items():
            text(k)
            integer(weight, 1)
    if isinstance(v, FencePolicy):
        require(v.threshold in RATINGS)
    if isinstance(v, SamplingRequest):
        require(isinstance(v.plan, CoveragePlan) and isinstance(v.options, CompositionOptions) and isinstance(v.fence, FencePolicy))
        for ids in (v.prompt_dictionary_ids, v.feature_dictionary_ids):
            require(isinstance(ids, tuple))
            for key in ids:
                identifier(key)
        require(isinstance(v.source_weights, Mapping) and not set(v.source_weights) - {'dictionary_pool', 'feature_pool'})
        for weight in v.source_weights.values():
            integer(weight, 1)
        require(v.launcher_selection is None or isinstance(v.launcher_selection, Mapping) and v.live_features)
        if v.append_target_id is not None:
            identifier(v.append_target_id)
    if isinstance(v, RatingDecision):
        require(v.reason in ('explicit', 'keyword', 'rating_model', 'nsfwfallback', 'refused_nsfw', 'default'))
        require(v.route in ('normal', 'nsfw'))
    if isinstance(v, ComposerResult):
        require(v.status in ('ok', 'refused_nsfw', 'invalid_input'))
        strings(v.out_of_frame)
        strings(v.added)
        require(isinstance(v.dropped, tuple))
        for d in v.dropped:
            require(isinstance(d, Mapping) and set(d) == {'tag', 'reason'})
            text(d['tag'])
            text(d['reason'])
        if v.status == 'ok':
            require(bool(v.tags) and v.aspect in ASPECTS and v.reason is None)
        else:
            require(not v.tags and not v.out_of_frame and not v.added and not v.dropped and v.aspect is None and v.natural_language is None)
            text(v.reason)
    if isinstance(v, TaskOutcome):
        require(v.status in ('pending', 'running', 'ok', 'skipped', 'failed'))
        require(v.reason_code is None or isinstance(v.reason_code, str))
        require(v.entry is None or isinstance(v.entry, PromptEntry))
    if isinstance(v, CoverageReport):
        require(v.status in ('satisfied', 'unsatisfiable', 'search_exhausted'))
        for n in ('cell_counts', 'constraint_counts', 'deficits', 'cell_targets', 'diagnostics', 'source_counts'):
            require(isinstance(getattr(v, n), Mapping))
            for k, count in getattr(v, n).items():
                text(k)
                integer(count)
    if isinstance(v, Issue):
        text(v.message, empty=True)
        require(v.severity in ('error', 'warning'))
        if v.row is not None:
            integer(v.row, 1)
        require(v.field is None or isinstance(v.field, str))
    if isinstance(v, (AppendResult, ImportPreview)):
        require(all(isinstance(i, Issue) for i in (v.conflicts if isinstance(v, AppendResult) else v.issues)))
    if isinstance(v, ImportPreview):
        require(v.kind in ('prompts', 'features'))
        require(all(isinstance(e, PromptEntry) for e in v.entries))
        require(all(isinstance(f, FeatureCandidate) for f in v.features))
        strings(v.raw_fragments)
    if isinstance(v, SeedRecord):
        integer(v.seq, 1)
        text(v.prompt)
        strings(v.normalized_tags, nonempty=True)
    if isinstance(v, TaskView):
        require(v.status in ('pending', 'running', 'paused', 'done', 'failed', 'canceled'))
        require(all(isinstance(o, TaskOutcome) for o in v.outcomes) and isinstance(v.coverage, CoverageReport))
    if isinstance(v, FrozenSeeds):
        require(v.count == len(v.items) and all(isinstance(i, ItemSeed) for i in v.items))
        require(tuple(i.seq for i in v.items) == tuple(range(1, v.count + 1)))
        for item in v.items:
            integer(item.seq,1); text(item.prompt); require(item.aspect is None or item.aspect in ASPECTS)


def strings(values, nonempty=False):
    require(isinstance(values, tuple) and (bool(values) or not nonempty))
    for value in values:
        text(value)


@lru_cache(maxsize=1)
def _style_skill_text():
    return (Path(__file__).resolve().parents[3] / 'skills/prompt-composer/SKILL.md').read_text(encoding='utf-8')


def skill_text(profile=None):
    if profile is not None:
        require(profile.enabled and profile.templates['R1'].enabled, 'profile:unavailable')
    return _style_skill_text()


@lru_cache(maxsize=8)
def forbidden_terms(template=None):
    section = (template or skill_text()).split('#### A.', 1)[1].split('#### I.', 1)[0]
    terms = set()
    for line in section.splitlines():
        if line.startswith('- ') and not line.startswith(('- 画幅类的边界', '- 输入里的')):
            terms.update(normalize_tag(t) for t in re.findall(r'`([^`]+)`', line))
    return frozenset(terms)


SHORT = {'mid', 'early', 'recent', 'general', 'safe', 'sensitive', 'explicit', 'cover', 'sample', 'text', 'border', 'landscape', 'square', 'frame'}
EXCEPTIONS = {'gradient hair', 'pastel pink dress', 'stained glass window', 'holding paintbrush', 'easel', 'holding photo', 'border collie', 'gothic architecture', 'steampunk goggles'}


def validate_content(entry, *, catalog=None, input_tags=None, terms=None, profile=None):
    if profile is not None:
        require(profile.enabled and profile.qc_policy == 'visible-content-v1', 'profile:unavailable')
    catalog = catalog or LocalCatalog()
    terms = terms if terms is not None else forbidden_terms()
    allowed = {normalize_tag(t, catalog) for t in (input_tags if input_tags is not None else entry.tags)}
    for value, is_tag in [(t, True) for t in entry.tags] + ([(entry.natural_language, False)] if entry.natural_language else []):
        key = normalize_tag(value, catalog if is_tag else None)
        exception = key in EXCEPTIONS and key in allowed
        hit = catalog.lookup(key)
        known = hit.match in ('known', 'alias') or bool(re.fullmatch(r'[^()]+ \([^()]+\)', key))
        if is_tag:
            require(',' not in key and syntax_of(key, known=known) == 'plain', 'composer:forbidden_content')
        require(not re.search(r'\b(?:camera|without)\b|\bno\s+(?!humans\b)|(?:artist|rating):|\b(?:art |drawn |illustrated )?by\s|in the style of|inspired by|\bscore\s*\d|\bsource anime\b|\b\d+\s*[x:]\s*\d+\b|\byear\s+\d{4}\b|\((?:medium|style|artist)\)', key), 'composer:forbidden_content')
        require(not key.startswith('@') and hit.category != 'artist', 'composer:forbidden_content')
        if not exception:
            comparison=key.replace('-', ' ') if not is_tag else key
            for term in terms:
                if term in SHORT:
                    matched = is_tag and key == term
                elif any(c in term for c in ':(){}[]|') or 'xxx' in term or term in ('and', 'break'):
                    matched = is_tag and key == term
                else:
                    matched = re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', comparison)
                require(not matched, 'composer:forbidden_content')
        require(not re.search(r'brushstroke|rendering|dreamy atmosphere|hazy background|artistic|ethereal|pictorial', key), 'composer:review_required')
    return entry


def validate_composer_result(text_value, request, *, catalog=None, terms=None, profile=None):
    data = strict_loads(text_value)
    require(isinstance(data, dict), 'llm:schema_contract')
    expected = {'status', 'tags', 'natural_language', 'out_of_frame', 'added', 'dropped', 'aspect'}
    if data.get('status') != 'ok':
        expected.add('reason')
    require(set(data) == expected, 'llm:schema_contract')
    result = construct(ComposerResult, data)
    if result.status != 'ok':
        return result
    require(request.tag_range[0] <= len(result.tags) <= request.tag_range[1], 'llm:schema_contract')
    keys = [normalize_tag(t, catalog) for t in result.tags]
    require(len(set(keys)) == len(keys) and all(t == normalize_tag(t) for t in result.tags), 'llm:schema_contract')
    require((bool(result.natural_language) if request.nl_enabled else result.natural_language is None), 'llm:schema_contract')
    if result.natural_language:
        require(not re.search(r'[\u4e00-\u9fff\n\r]', result.natural_language), 'llm:schema_contract')
    require(request.aspect_hint is None or result.aspect == request.aspect_hint, 'llm:schema_contract')
    inputs = {normalize_tag(t, catalog) for tags in request.features.values() for t in tags}
    added = {normalize_tag(t, catalog) for t in result.added}
    dropped = {normalize_tag(d['tag'], catalog) for d in result.dropped}
    invisible = {normalize_tag(t, catalog) for t in result.out_of_frame}
    require(added == set(keys) - inputs and not dropped & set(keys) and dropped <= inputs, 'llm:schema_contract')
    require(inputs - set(keys) <= dropped | invisible, 'llm:schema_contract')
    require(not invisible & set(keys), 'composer:forbidden_content')
    nl = normalize_tag(result.natural_language) if result.natural_language else ''
    require(not any(re.search(r'(?<!\w)' + re.escape(t) + r'(?!\w)', nl) for t in invisible), 'composer:forbidden_content')
    if 'no humans' in keys:
        require(not any(re.fullmatch(r'\d+(?:girls?|boys?|people)|solo', t) for t in keys), 'llm:schema_contract')
    validate_content(PromptEntry(result.tags, result.natural_language, result.aspect), catalog=catalog, input_tags=inputs, terms=terms, profile=profile)
    return result


def serialization_issues(entry, catalog):
    issues = []
    for t in entry.tags:
        if catalog.lookup(t).match not in ('known', 'alias'):
            issues.append(Issue('serialization:unknown_tag', field=t, severity='warning'))
    for token in tokenize_prompt(entry.natural_language or '', catalog=catalog):
        if token.lookup in ('known', 'alias'):
            issues.append(Issue('serialization:nl_tag_collision', field=token.raw, severity='warning'))
    return tuple(issues)
