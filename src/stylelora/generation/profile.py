from dataclasses import fields
from collections.abc import Mapping
import math
from stylelora.backends.types import (GenerationProfile, SourceGenerationConfig,
    GenerationRequest, PixelSize, BackendError, plain)
from stylelora.project_db.validation import relative_path

SMALL = {'landscape': PixelSize(768, 512), 'portrait': PixelSize(512, 768), 'square': PixelSize(640, 640)}
LARGE = {'landscape': PixelSize(1536, 1024), 'portrait': PixelSize(1024, 1536), 'square': PixelSize(1472, 1472)}


def validate_value(value, schema):
    kind = schema.get('type')
    valid = {'integer': type(value) is int, 'number': type(value) in (int, float) and math.isfinite(value),
             'boolean': type(value) is bool, 'string': isinstance(value, str), 'null': value is None,
             'array': isinstance(value, (tuple, list)), 'object': isinstance(value, Mapping)}
    if kind and not any(valid.get(k, False) for k in (kind if isinstance(kind, (tuple, list)) else (kind,))):
        raise BackendError('profile:invalid', 'profile')
    if 'enum' in schema and value not in schema['enum']:
        raise BackendError('profile:invalid', 'profile')
    if 'const' in schema and value != schema['const']:
        raise BackendError('profile:invalid', 'profile')
    if type(value) in (int, float) and (value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf)):
        raise BackendError('profile:invalid', 'profile')


def profile_values(profile):
    result = plain(profile)
    result.pop('backend')
    extra = result.pop('extra')
    if set(extra) & {f.name for f in fields(GenerationProfile)}:
        raise BackendError('profile:invalid', 'profile')
    result.update(extra)
    return result


def validate_profile(profile, caps):
    if profile.backend != caps.backend or profile.model not in caps.models or profile.quality_toggle is not False or profile.uc_preset is not None:
        raise BackendError('profile:invalid', 'profile')
    values, specs = profile_values(profile), {s.key: s for s in caps.parameters}
    for key, value in values.items():
        if key not in specs or specs[key].read_only:
            raise BackendError('profile:invalid', 'profile')
        validate_value(value, specs[key].value_schema)
        locks = specs[key].locked_by_effort
        if profile.effort in locks and (locks[profile.effort] is None or value != locks[profile.effort]):
            raise BackendError('profile:effort_conflict', 'profile')
    if any(s.required and s.key not in values for s in specs.values() if not s.read_only):
        raise BackendError('profile:invalid', 'profile')


def resolve_aspect(entry, llm):
    for value in (entry, llm):
        if value is not None and value not in SMALL:
            raise BackendError('profile:invalid', 'profile')
    return entry if entry is not None else llm


def to_snapshot(config):
    values = profile_values(config.profile)
    model = values.pop('model')
    return {'schema_version': 1, 'backend': config.profile.backend, 'model': model,
            'quality_toggle': False, 'uc_preset': None, 'params': values,
            'default_aspect': config.default_aspect,
            'dimensions': {k: [v.width, v.height] for k, v in config.sizes.items()}}


def from_snapshot(value):
    try:
        params = dict(value['params'])
        known = {f.name for f in fields(GenerationProfile)} - {'backend', 'model', 'extra'}
        extra = {k: params.pop(k) for k in tuple(params) if k not in known}
        if value['quality_toggle'] is not False or value['uc_preset'] is not None:
            raise ValueError()
        profile = GenerationProfile(backend=value['backend'], model=value['model'], extra=extra, **params)
        return SourceGenerationConfig(profile, {k: PixelSize(*v) for k, v in value['dimensions'].items()}, value['default_aspect'], value['schema_version'])
    except (KeyError, TypeError, ValueError):
        raise BackendError('profile:invalid', 'profile') from None


def build_request(config, source, item, project_root, *, mode_context=None):
    from stylelora.modes.profiles import get_profile, freeze_profile, resolve_profile, ModeContext
    from stylelora.prompts.formatters import apply_formatter, FormatContext
    from stylelora.prompts.types import PromptEntry
    mode_context = mode_context or ModeContext(freeze_profile(get_profile()))
    profile = resolve_profile(mode_context.profile)
    if profile.generation_policy != 'existing-content-v1':
        raise BackendError('mode:unsupported', 'profile')
    apply_formatter(PromptEntry(tuple(part.strip() for part in item.prompt.split(',') if part.strip())),
                    context=FormatContext(mode_context.profile, {}, str(item.item_id)))
    if config.profile.effort == 'medium' and source.negative_prompt.strip():
        raise BackendError('profile:effort_conflict', 'profile')
    size = config.sizes[item.aspect or config.default_aspect]
    attempt = max(1, item.attempts)
    path = relative_path(project_root, f'images/.pending/{item.item_id}/{attempt}/out.png')
    return GenerationRequest(item_id=item.item_id, attempt=attempt,
        positive=', '.join(s.strip() for s in (source.style_prompt, item.prompt) if s.strip()),
        negative=source.negative_prompt, width=size.width, height=size.height, save_path=path)
