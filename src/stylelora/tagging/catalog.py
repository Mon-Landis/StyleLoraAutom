"""Immutable local versioned index. No network fallback."""
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
import os
from .jsonio import loads
from .normalization import normalize_tag
from .types import CatalogHit
from stylelora.project_db.errors import ValidationError
from stylelora.project_db.validation import digest


class TagCatalog(Protocol):
    revision: str
    complete: bool
    available: bool
    def lookup(self, value: str) -> CatalogHit: ...
    def search(self, query: str, *, limit: int = 20) -> tuple[CatalogHit, ...]: ...


class LocalCatalog:
    def __init__(self, data=None, translations=None, *, complete=False):
        entries, aliases = {}, {}
        if data is not None:
            if (type(data) is not dict or set(data) - {'schema_version','kind','revision','tags','source','source_revision'}
                    or type(data.get('schema_version')) is not int or data['schema_version'] != 1
                    or data.get('kind') != 'tag_catalog' or type(data.get('revision')) is not int
                    or data['revision'] < 0 or not isinstance(data.get('tags'), list)):
                raise ValidationError('catalog:invalid')
            for row in data['tags']:
                if not isinstance(row, dict) or set(row) - {'tag','category','aliases'}:
                    raise ValidationError('catalog:invalid')
                if not isinstance(row.get('tag'), str) or not normalize_tag(row['tag']):
                    raise ValidationError('catalog:invalid')
                key = normalize_tag(row['tag'])
                if key in entries or not isinstance(row.get('category', 'general'), str):
                    raise ValidationError('catalog:conflict')
                entries[key] = row.get('category', 'general')
                if not isinstance(row.get('aliases', []), list):
                    raise ValidationError('catalog:invalid')
                for alias in row.get('aliases', []):
                    if not isinstance(alias, str) or not normalize_tag(alias):
                        raise ValidationError('catalog:invalid')
                    alias = normalize_tag(alias)
                    if alias in aliases and aliases[alias] != key:
                        raise ValidationError('catalog:alias_conflict')
                    aliases[alias] = key
            if any(a in entries and a != k for a,k in aliases.items()):
                raise ValidationError('catalog:alias_conflict')
        translations = translations or {}
        translated = {}
        for key, value in translations.items():
            if not isinstance(key, str) or not isinstance(value, str) or not normalize_tag(key):
                raise ValidationError('translations:invalid')
            key = aliases.get(normalize_tag(key), normalize_tag(key))
            if key in translated and translated[key] != value:
                raise ValidationError('translations:conflict')
            translated[key] = value
        self.available = data is not None or bool(translated)
        self.complete = bool(complete and data is not None)
        self.revision = digest({'index':data,'translations':translated,'complete':self.complete})
        self.entries = MappingProxyType(entries)
        self.aliases = MappingProxyType(aliases)
        self.translations = MappingProxyType(translated)

    def lookup(self, value):
        key = normalize_tag(value)
        canonical = self.aliases.get(key, key)
        known = canonical in self.entries or canonical in self.translations
        match = ('alias' if canonical != key else 'known') if known else ('unknown' if self.available else 'unverifiable')
        return CatalogHit(value, canonical if known else None, self.entries.get(canonical),
                          self.translations.get(canonical), match, self.revision)

    def search(self, query, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError('catalog:invalid_limit')
        key = normalize_tag(query)
        return tuple(self.lookup(k) for k in sorted(set(self.entries) | set(self.translations))
                     if key in k or key in self.translations.get(k, ''))[:limit]


def load_catalog(path=None, *, translations_path=None, complete=False):
    path = Path(path) if path else Path(os.environ.get('APPDATA', Path.home())) / 'StyleLoraAutom/tagging/catalog.json'
    if not path.is_file():
        raise ValidationError('catalog:unavailable')
    translations = None
    if translations_path:
        envelope = loads(Path(translations_path).read_bytes())
        if (type(envelope.get('schema_version')) is not int or envelope.get('schema_version') != 1
                or envelope.get('kind') != 'translations' or type(envelope.get('revision')) is not int
                or set(envelope)-{'schema_version','kind','revision','entries','operations'}):
            raise ValidationError('translations:invalid')
        translations = envelope['entries']
    return LocalCatalog(loads(path.read_bytes()), translations, complete=complete)


def lookup_tag(catalog, value):
    return catalog.lookup(value)
