from dataclasses import dataclass
from stylelora.project_db.types import Tag


@dataclass(frozen=True)
class CaptionConfig:
    schema_version: int = 1
    tag_layers: tuple[str, ...] = ('original','wd','llm','manual','rule')
    layer_priority: tuple[str, ...] = ('manual','rule','llm','wd','original')
    wd_min_confidence: float = 0.0
    wd_null_confidence: str = 'exclude'
    include_nl: bool = False
    nl_priority: tuple[str, ...] = ('manual','llm','original')
    tag_sort: str = 'layer_first'
    blocklist_mode: str = 'warn'
    separator: str = ', '
    trigger_position: str = 'first'


@dataclass(frozen=True)
class CaptionResult:
    tags: tuple[Tag, ...]
    natural_language: str | None
    warnings: tuple[str, ...]
    removed_blocklist: tuple[str, ...]
    trigger_valid: bool
    sorter: str

    def serialize(self):
        return ', '.join(t.tag for t in self.tags) + ('\n\n' + self.natural_language if self.natural_language else '')
