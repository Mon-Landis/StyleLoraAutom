"""Read-only aggregation over one frozen image query."""
from dataclasses import dataclass, field
from collections.abc import Mapping
from types import MappingProxyType
from .profiles import ProfileError


@dataclass(frozen=True)
class ImageSummary:
    image_id: int
    rating: str | None = None
    review_status: str | None = None
    content_dimensions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    qc_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatsDTO:
    denominator: int
    groups: Mapping
    coverage: Mapping
    leakage_images: int


def summarize(rows, profile):
    if not profile.enabled or profile.statistics_policy != 'content-coverage-v1':
        raise ProfileError('profile:unavailable')
    rows = {row.image_id: row for row in rows}
    groups = {'rating': {}, 'review_status': {}}
    coverage = {}
    leakage = set()
    for row in rows.values():
        for key, counts in groups.items():
            value = getattr(row, key) or 'unknown'
            counts[value] = counts.get(value, 0) + 1
        for dimension, values in row.content_dimensions.items():
            counts = coverage.setdefault(dimension, {})
            for value in set(values):
                counts[value] = counts.get(value, 0) + 1
        if any(code in ('blocklist:hit', 'qc:style_leakage') for code in row.qc_codes):
            leakage.add(row.image_id)
    return StatsDTO(len(rows), MappingProxyType({k: MappingProxyType(v) for k, v in groups.items()}),
                    MappingProxyType({k: MappingProxyType(v) for k, v in coverage.items()}), len(leakage))
