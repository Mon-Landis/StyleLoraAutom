from dataclasses import dataclass
from pathlib import Path
import asyncio
from stylelora.llm.types import CancelToken as LlmCancelToken
from stylelora.project_db.types import AnnotationInput, CurrentRef, RatingValue, Tag


class CancelToken(LlmCancelToken):
    @property
    def cancelled(self):
        return self.canceled

    def raise_if_cancelled(self):
        if self.canceled:
            raise asyncio.CancelledError()

    async def wait(self):
        while not self.canceled:
            await asyncio.sleep(.02)


class Unavailable(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ImageInput:
    image_id: int
    path: Path
    sha256: str
    file_size: int
    width: int
    height: int


@dataclass(frozen=True)
class Evidence:
    original_tags: tuple[Tag, ...]
    original_nl: str
    wd_tags: tuple[Tag, ...]
    refs: tuple[CurrentRef, ...]
    digest: str


@dataclass(frozen=True)
class ImageContext:
    image: ImageInput
    evidence: Evidence
    rating: RatingValue | None
    snapshot: dict
    cancel: CancelToken


@dataclass(frozen=True)
class ExecutionResult:
    kind: str
    reason: str | None = None
    annotations: tuple[AnnotationInput, ...] = ()
    rating: RatingValue | None = None
