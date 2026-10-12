"""Immutable product DTOs; no project provenance is carried by these objects."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal
import secrets
from stylelora.project_db.types import Aspect, ItemSeed

Rating = Literal['general', 'sensitive', 'questionable', 'explicit']
RATINGS = ('general', 'sensitive', 'questionable', 'explicit')
ASPECTS = ('landscape', 'portrait', 'square')


class PromptError(ValueError):
    def __init__(self, code='schema:invalid', *, issues=()):
        self.code, self.issues, self.retryable = code, tuple(issues), False
        # Never include input, model output, endpoint or credentials in diagnostics.
        super().__init__(code)


def immutable(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: immutable(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(immutable(v) for v in value)
    return value


def plain(value):
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


class Checked:
    def __post_init__(self):
        for f in fields(self):
            object.__setattr__(self, f.name, immutable(getattr(self, f.name)))
        from .validation import validate_dto
        validate_dto(self)


@dataclass(frozen=True)
class FeatureCandidate(Checked):
    dimension: str
    tag: str
    rating: Rating | None = None
    source: Literal['dictionary', 'launcher', 'manual'] = 'dictionary'


@dataclass(frozen=True)
class PromptEntry(Checked):
    tags: tuple[str, ...]
    natural_language: str | None = None
    aspect: Aspect | None = None
    rating: Rating | None = None


@dataclass(frozen=True)
class FeatureDictionary(Checked):
    dictionary_id: str
    name: str
    schema_version: int = 1
    dimensions: Mapping[str, tuple[FeatureCandidate, ...]] = field(default_factory=dict)
    revision: int = 1


@dataclass(frozen=True)
class PromptDictionary(Checked):
    dictionary_id: str
    name: str
    schema_version: int = 1
    entries: tuple[PromptEntry, ...] = ()
    revision: int = 1


@dataclass(frozen=True)
class ConstraintSpec(Checked):
    dimension: str
    tag: str
    minimum_ratio: float | None = None
    maximum_ratio: float | None = None
    minimum_count: int | None = None
    maximum_count: int | None = None


@dataclass(frozen=True)
class Predicate(Checked):
    all_tags: tuple[str, ...] = ()
    none_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoveragePlan(Checked):
    primary_dimensions: tuple[str, ...]
    cell_values: Mapping[str, tuple[str, ...]]
    constraints: tuple[ConstraintSpec, ...] = ()
    target_count: int = 100
    random_seed: int = field(default_factory=lambda: secrets.randbits(64))
    schema_version: int = 1
    algorithm_version: str = 'coverage-v1'
    cell_weights: Mapping[str, int] = field(default_factory=dict)
    exclude: tuple[Predicate, ...] = ()
    compatibility_version: str | None = None


@dataclass(frozen=True)
class CompositionOptions(Checked):
    nl_enabled: bool = True
    composition_mode: str = 'normal'
    tag_range: tuple[int, int] = (15, 40)
    aspect_hint: Aspect | None = None


@dataclass(frozen=True)
class CompositionRequest(Checked):
    features: Mapping[str, tuple[str, ...]]
    rating_policy: str = 'sfw'
    nl_enabled: bool = True
    composition_mode: str = 'normal'
    tag_range: tuple[int, int] = (15, 40)
    aspect_hint: Aspect | None = None


@dataclass(frozen=True)
class FencePolicy(Checked):
    enabled: bool = True
    threshold: Rating = 'sensitive'
    nsfwfallback: bool = False
    use_rating_model: bool = False


@dataclass(frozen=True)
class SamplingRequest(Checked):
    plan: CoveragePlan
    prompt_dictionary_ids: tuple[str, ...] = ()
    feature_dictionary_ids: tuple[str, ...] = ()
    live_features: bool = False
    source_weights: Mapping[str, int] = field(default_factory=lambda: {'dictionary_pool': 1, 'feature_pool': 1})
    optimize: bool = False
    options: CompositionOptions = field(default_factory=CompositionOptions)
    fence: FencePolicy = field(default_factory=FencePolicy)
    coverage_enabled: bool = True
    launcher_selection: Mapping | None = None
    append_target_id: str | None = None


@dataclass(frozen=True)
class RatingDecision(Checked):
    rating: Rating | None
    verified: bool
    reason: str
    route: str


@dataclass(frozen=True)
class ComposerResult(Checked):
    status: str
    tags: tuple[str, ...]
    natural_language: str | None
    out_of_frame: tuple[str, ...]
    added: tuple[str, ...]
    dropped: tuple[Mapping[str, str], ...]
    aspect: Aspect | None
    reason: str | None = None


@dataclass(frozen=True)
class SeedRecord(Checked):
    seq: int
    prompt: str
    aspect: Aspect | None
    normalized_tags: tuple[str, ...]


@dataclass(frozen=True)
class TaskOutcome(Checked):
    task_id: str
    index: int
    status: str = 'pending'
    reason_code: str | None = None
    entry: PromptEntry | None = None


@dataclass(frozen=True)
class CoverageReport(Checked):
    requested: int
    available: int
    cell_counts: Mapping[str, int]
    constraint_counts: Mapping[str, int]
    deficits: Mapping[str, int]
    status: str
    cell_targets: Mapping[str, int] = field(default_factory=dict)
    diagnostics: Mapping[str, int] = field(default_factory=dict)
    source_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Issue(Checked):
    code: str
    message: str = ''
    row: int | None = None
    field: str | None = None
    severity: str = 'error'


@dataclass(frozen=True)
class ImportPreview(Checked):
    preview_id: str
    digest: str
    entries: tuple[PromptEntry, ...]
    features: tuple[FeatureCandidate, ...]
    issues: tuple[Issue, ...]
    base_revision: int
    kind: str = 'prompts'
    raw_fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppendResult(Checked):
    revision: int
    inserted: int
    duplicates: int
    conflicts: tuple[Issue, ...] = ()


@dataclass(frozen=True)
class TaskView(Checked):
    task_id: str
    revision: int
    status: str
    outcomes: tuple[TaskOutcome, ...]
    coverage: CoverageReport
    ready: bool


@dataclass(frozen=True)
class FrozenSeeds(Checked):
    digest: str
    items: tuple[ItemSeed, ...]
    count: int
