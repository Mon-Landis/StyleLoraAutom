"""Versioned, immutable dataset policy boundary. Reserved modes cannot execute."""
from dataclasses import dataclass, fields, is_dataclass
from collections.abc import Mapping
from types import MappingProxyType
import hashlib
import json


class ProfileError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _plain(value):
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [_plain(v) for v in sorted(value)] if isinstance(value, frozenset) else [_plain(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class TemplateRef:
    variant: str
    content_hash: str | None
    enabled: bool


@dataclass(frozen=True)
class ModeProfile:
    mode: str
    revision: int
    enabled: bool
    display_name: str
    templates: Mapping[str, TemplateRef]
    dataset_mode_policy: str
    blocklist_policy: str
    protected_tags_policy: str
    trigger_semantics: str
    caption_order: tuple[str, ...]
    default_sort: str
    blocklist_action: str
    statistics_policy: str
    generation_policy: str
    qc_policy: str
    capabilities: frozenset[str]

    def __post_init__(self):
        object.__setattr__(self, 'templates', MappingProxyType(dict(self.templates)))
        object.__setattr__(self, 'caption_order', tuple(self.caption_order))
        object.__setattr__(self, 'capabilities', frozenset(self.capabilities))


@dataclass(frozen=True)
class ProfileSnapshot:
    mode: str
    revision: int
    digest: str
    policies: Mapping[str, str]
    template_hashes: Mapping[str, str]

    def __post_init__(self):
        for name in ('policies', 'template_hashes'):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class ModeContext:
    profile: ProfileSnapshot
    trigger: str = ''
    protected_tags: tuple[str, ...] = ()


def get_profile(mode='style', revision=None, *, executable=True):
    if mode not in ('style', 'character', 'action', 'object'):
        raise ProfileError('mode:unknown')
    if revision is not None and (type(revision) is not int or revision != 1):
        raise ProfileError('profile:unavailable')
    enabled = mode == 'style'
    if executable and not enabled:
        raise ProfileError('mode:unsupported')
    # R2 text is supplied by the existing content-addressed SkillStore at execution.
    # There is no built-in R2 text whose hash could truthfully be declared here.
    from stylelora.prompts.validation import skill_text
    r1 = hashlib.sha256(skill_text().encode()).hexdigest() if enabled else None
    return ModeProfile(mode, 1, enabled,
        {'style': '画风模式', 'character': '角色模式', 'action': '动作模式', 'object': '物体模式'}[mode],
        MappingProxyType({
            'R1': TemplateRef(mode + '/content', r1, enabled),
            'R2': TemplateRef(mode + ('/visible-content' if enabled else '/visible-features'), None, enabled),
            'R3': TemplateRef(mode + '/clean', None, False)}),
        'style: describe visible content only; exclude style, brushwork and medium' if enabled else 'reserved',
        'style-content-v1' if enabled else 'character-protect-v1',
        'trigger-v1' if enabled else 'identity-v1',
        'style_identity' if enabled else 'character_identity',
        ('trigger', 'subject' if enabled else 'identity_features', 'action_expression', 'clothing', 'composition', 'scene', 'lighting', 'nl'),
        'preserve' if enabled else 'reserved', 'warn' if enabled else 'reserved',
        'content-coverage-v1' if enabled else 'identity-coverage-v1',
        'existing-content-v1' if enabled else 'character-reference-v1',
        'visible-content-v1' if enabled else 'identity-consistency-v1',
        frozenset({'mode.style'}) if enabled else frozenset())


def freeze_profile(profile):
    from stylelora.caption.blocklist import WORDS, EXCEPTIONS, AMBIGUOUS
    policies = {f.name: getattr(profile, f.name) for f in fields(profile)
                if f.name.endswith('_policy') or f.name in ('trigger_semantics', 'default_sort', 'blocklist_action')}
    policies['rules_hash'] = digest((WORDS, EXCEPTIONS, AMBIGUOUS))
    hashes = {key: value.content_hash for key, value in profile.templates.items() if value.content_hash}
    return ProfileSnapshot(profile.mode, profile.revision, digest((profile, policies)), policies, hashes)


def resolve_profile(snapshot):
    if isinstance(snapshot, Mapping):
        try:
            snapshot = ProfileSnapshot(**snapshot)
        except (TypeError, ValueError):
            raise ProfileError('profile:unavailable') from None
    profile = get_profile(snapshot.mode, snapshot.revision)
    if freeze_profile(profile) != snapshot:
        raise ProfileError('profile:unavailable')
    return profile


def profile_from_snapshot(snapshot):
    frozen = snapshot.get('profile', snapshot.get('params', {}).get('mode_profile'))
    return resolve_profile(frozen) if frozen is not None else get_profile()
