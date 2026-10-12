from dataclasses import dataclass
from collections.abc import Mapping
from stylelora.tagging.types import Issue
from stylelora.caption.blocklist import Hit
from .types import FileResult


@dataclass(frozen=True)
class ImageResult:
    image_id: int
    kind: str
    reason: str | None
    files: tuple[FileResult,...] = ()


@dataclass(frozen=True)
class ExportReport:
    schema_version: int
    export_id: str
    dir: str
    selected: int
    exported: int
    skipped: int
    failed: int
    skip_reasons: Mapping[str,int]
    failure_reasons: Mapping[str,int]
    blocklist_hits: tuple[Hit,...]
    tag_frequency: Mapping[str,int]
    trigger_count: int
    files: tuple[FileResult,...]
    warnings: tuple[Issue,...]
    images: tuple[ImageResult,...]
    frequency_includes_trigger: bool = True
    trigger_override_digest: str | None = None
