from typing import Protocol, Literal
from pathlib import Path
from ..types import FileResult


class CaptionWriter(Protocol):
    name: str
    def write_caption(self,path: Path,caption: str,*,encoding: str) -> FileResult: ...
    def write_image(self,src: Path,dst: Path,*,mode: Literal['copy','hardlink']) -> FileResult: ...
