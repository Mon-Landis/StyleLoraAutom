from pathlib import Path
from typing import Protocol
from stylelora.tagging.catalog import TagCatalog
from ..types import ImportParams, ReadResult


class CaptionReader(Protocol):
    name: str
    def read(self,image: Path,params: ImportParams,catalog: TagCatalog) -> ReadResult: ...
