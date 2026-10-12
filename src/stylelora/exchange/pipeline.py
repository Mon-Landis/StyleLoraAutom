from dataclasses import dataclass
from typing import Protocol
from pathlib import Path
from stylelora.project_db.types import JsonObject
from stylelora.project_db.errors import UnsupportedScheme
from stylelora.tagging.types import Issue
from .types import FileResult


@dataclass(frozen=True)
class PipelineReport:
    files: tuple[FileResult,...] = ()
    warnings: tuple[Issue,...] = ()


class ExportProcessor(Protocol):
    name: str
    def process(self,image: Path,params: JsonObject) -> FileResult: ...


def execute_pipeline(path,pipeline):
    if getattr(pipeline,'steps',pipeline):
        raise UnsupportedScheme('pipeline:unsupported')
    return PipelineReport()
