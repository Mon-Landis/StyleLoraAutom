from dataclasses import dataclass
from typing import Literal
from stylelora.project_db.types import Tag


@dataclass(frozen=True)
class Issue:
    code: str
    message: str = ''
    severity: Literal['warning', 'error'] = 'warning'
    field: str | None = None


@dataclass(frozen=True)
class CatalogHit:
    query: str
    canonical: str | None
    category: str | None
    translation: str | None
    match: str
    catalog_revision: str


@dataclass(frozen=True)
class Token:
    raw: str
    normalized: str
    syntax: str
    escaped: bool
    weight: float | None
    lookup: str
    offset: int = 0


@dataclass(frozen=True)
class ParsedSegment:
    raw: str
    token: Token | None
    kind: str
    reason: str | None = None


@dataclass(frozen=True)
class ParsedPrompt:
    tags: tuple[Tag, ...]
    natural_language: str | None
    segments: tuple[ParsedSegment, ...]
    tokenizer_version: str
    unverifiable: tuple[str, ...]
