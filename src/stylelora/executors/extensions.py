from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DetectionResult:
    nsfw: bool | None
    confidence: float | None
    model: str
    regions: tuple[dict, ...] = ()


@dataclass(frozen=True)
class AestheticResult:
    schema_version: int
    score: float
    scale_min: float
    scale_max: float
    model: str
    revision: str


class LocalNsfwDetector(Protocol):
    async def detect(self, image, cancel) -> DetectionResult: ...


class AestheticScorer(Protocol):
    async def score(self, image, cancel) -> AestheticResult: ...


class CleanExecutor(Protocol):
    async def clean(self, value, *, role, cancel): ...


def capabilities():
    return {name: {'enabled': False, 'status': 'unsupported'} for name in ('clean', 'yolo', 'aesthetic')}
