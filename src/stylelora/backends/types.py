"""Immutable generation values; secrets and transport replies stay at the boundary."""
from dataclasses import dataclass, field, fields, is_dataclass
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
import hashlib
import json


def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


def plain(value):
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def canonical(value):
    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class Immutable:
    def __post_init__(self):
        for f in fields(self):
            object.__setattr__(self, f.name, freeze(getattr(self, f.name)))


class BackendError(Exception):
    def __init__(self, code, phase='backend', outcome='not_sent', retry='manual'):
        # Messages are local reason codes, never server text or exception strings.
        self.code, self.phase, self.outcome, self.retry = code, phase, outcome, retry
        self.safe_message = code
        super().__init__(code)


class UnsupportedFeature(BackendError):
    def __init__(self):
        super().__init__('profile:unsupported_feature', 'profile', retry='fatal')


@dataclass(frozen=True)
class PixelSize:
    width: int
    height: int

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 or v % 64 for v in (self.width, self.height)):
            raise BackendError('profile:invalid', 'profile')


@dataclass(frozen=True, kw_only=True)
class GenerationProfile(Immutable):
    backend: str
    effort: str
    sampler: str
    steps: int
    scale: float
    cfg_rescale: float
    noise_schedule: str
    smea: bool
    smea_dyn: bool
    variety_plus: bool
    decrisp: bool
    model: str = 'nai-diffusion-5-full'
    uc_preset: None = None
    quality_toggle: bool = False
    extra: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class SourceGenerationConfig(Immutable):
    profile: GenerationProfile
    sizes: Mapping
    default_aspect: str = 'square'
    schema_version: int = 1

    def __post_init__(self):
        super().__post_init__()
        if self.schema_version != 1 or set(self.sizes) != {'landscape', 'portrait', 'square'} or self.default_aspect not in self.sizes:
            raise BackendError('profile:invalid', 'profile')
        for aspect, size in self.sizes.items():
            if not isinstance(size, PixelSize) or (aspect == 'landscape' and size.width <= size.height) or (aspect == 'portrait' and size.width >= size.height) or (aspect == 'square' and size.width != size.height):
                raise BackendError('profile:invalid', 'profile')


@dataclass(frozen=True)
class ReferenceInput:
    image: Path
    strength: float


@dataclass(frozen=True)
class CharacterInput:
    positive: str
    negative: str
    position: tuple[float, float] | None = None


@dataclass(frozen=True, kw_only=True)
class GenerationRequest(Immutable):
    item_id: int
    attempt: int
    positive: str
    negative: str
    width: int
    height: int
    save_path: Path
    seed: int | None = None
    count: int = 1
    references: tuple = ()
    source_image: Path | None = None
    characters: tuple = ()

    def __post_init__(self):
        super().__post_init__()
        PixelSize(self.width, self.height)
        if self.references or self.source_image is not None or self.characters:
            raise UnsupportedFeature()
        if type(self.count) is not int or self.count != 1 or any(type(v) is not int or v < 1 for v in (self.item_id, self.attempt)) or not self.save_path.is_absolute():
            raise BackendError('profile:invalid', 'profile')
        if self.seed is not None and (type(self.seed) is not int or not 0 <= self.seed <= 4294967295):
            raise BackendError('profile:invalid', 'profile')


@dataclass(frozen=True)
class ParameterSpec(Immutable):
    key: str
    value_schema: Mapping
    default: object = None
    required: bool = True
    read_only: bool = False
    locked_by_effort: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class BackendCapabilities(Immutable):
    backend: str
    schema_version: int
    models: tuple[str, ...]
    parameters: tuple[ParameterSpec, ...]
    per_request_parameters: tuple[str, ...] = ()
    session_settings: bool = False
    supports_cost: bool = False
    supports_cancel: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileHandle(Immutable):
    lease_id: str
    original: Mapping
    applied: Mapping
    writable_keys: tuple[str, ...]
    backend_instance: str


@dataclass(frozen=True)
class Preparation(Immutable):
    handle: str
    request_digest: str
    estimated_anlas: int | None
    effective: Mapping
    expires_at: str | None = None


@dataclass(frozen=True)
class GenerationResult(Immutable):
    resource_id: str | None = None
    saved_path: Path | None = None
    image_bytes: bytes | None = field(default=None, repr=False)
    metadata_stripped: bool | None = None
    actual: Mapping = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class AccountStatus:
    source: str
    subscription_tier: str | None
    anlas_balance: int | None
    fixed_quota_remaining: int | None
    remaining_estimated_anlas: int | None
    unpriced_items: int
    refreshed_at: str
