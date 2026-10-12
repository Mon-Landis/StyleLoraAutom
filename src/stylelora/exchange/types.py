from dataclasses import dataclass
from stylelora.project_db.types import OriginalInput, JsonObject
from stylelora.tagging.types import Issue


@dataclass(frozen=True)
class ImportParams:
    schema_version: int = 1
    encoding: str = 'utf-8-sig'
    recursive: bool = True
    layout: str = 'first_line_blank_nl'
    tokenizer_version: str = 'comma-v1'
    on_unverifiable: str = 'preview_error'


@dataclass(frozen=True)
class OutputConfig:
    schema_version: int = 1
    image_mode: str = 'copy'
    filename: str = '{image_id}{ext}'
    caption_encoding: str = 'utf-8'
    include_json: bool = False
    require_nonempty_tags: bool = False
    subdirectory: str = ''
    overwrite: str = 'error'


@dataclass(frozen=True)
class ReadResult:
    originals: tuple[OriginalInput,...]
    encoding: str | None
    warnings: tuple[Issue,...]
    sidecar_sha256: str | None


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    fallback: bool


@dataclass(frozen=True)
class ManifestEntry:
    seq: int
    relative_path: str
    image_sha256: str
    sidecar_sha256: str | None


@dataclass(frozen=True)
class ImportManifest:
    entries: tuple[ManifestEntry,...]
    warnings: tuple[Issue,...] = ()


@dataclass(frozen=True)
class FileResult:
    path: str
    bytes: int
    sha256: str
    action: str


@dataclass(frozen=True)
class PipelineStep:
    processor: str
    params: JsonObject


@dataclass(frozen=True)
class PipelineSpec:
    schema_version: int = 1
    steps: tuple[PipelineStep,...] = ()
