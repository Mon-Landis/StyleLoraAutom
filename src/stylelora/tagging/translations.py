"""Local translation revisions with atomic CAS and replay receipts."""
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from .jsonio import loads, atomic_json
from .normalization import normalize_tag
from .types import Issue
from stylelora.project_db.validation import digest
from stylelora.project_db.errors import ValidationError, StateConflict
from stylelora.project_db.types import WriteResult

_lock = RLock()


@dataclass(frozen=True)
class CommitReport:
    revision: int
    inserted: int
    skipped: int
    issues: tuple[Issue, ...] = ()


class TranslationStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {'schema_version':1,'kind':'translations','revision':0,'entries':{},'operations':{}}
        value = loads(self.path.read_bytes())
        if (set(value) != {'schema_version','kind','revision','entries','operations'} or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or value['kind'] != 'translations' or type(value['revision']) is not int
                or value['revision'] < 0 or not isinstance(value['entries'],dict) or not isinstance(value['operations'],dict)):
            raise ValidationError('translations:invalid')
        for key,translation in value['entries'].items():
            if not key or normalize_tag(key) != key or not isinstance(translation,str) or not translation.strip():
                raise ValidationError('translations:invalid')
        return value

    def get_translation(self, tag):
        return self.load()['entries'].get(normalize_tag(tag))

    def put_translation(self, tag, value, *, expected_revision):
        if not isinstance(tag,str) or not normalize_tag(tag) or not isinstance(value,str):
            raise ValidationError('translations:invalid')
        with _lock:
            data = self.load()
            if type(expected_revision) is not int or data['revision'] != expected_revision:
                raise StateConflict('catalog:revision_conflict')
            key = normalize_tag(tag)
            value = value.strip()
            if data['entries'].get(key,'') == value:
                return WriteResult(data['revision'],False)
            if value:
                data['entries'][key] = value
            else:
                data['entries'].pop(key,None)
            data['revision'] += 1
            atomic_json(self.path,data)
            return WriteResult(data['revision'],True)

    def import_tagcleaner_translations(self, raw, *, request_key, expected_revision=None):
        source = loads(raw)
        if not isinstance(source,dict):
            raise ValidationError('translations:invalid')
        entries, issues = {}, []
        for key,value in source.items():
            if not isinstance(value,str) or not normalize_tag(key):
                raise ValidationError('translations:invalid')
            key = normalize_tag(key)
            if not value.strip():
                issues.append(Issue('translations:empty',field=key))
                continue
            if key in entries and entries[key] != value:
                raise StateConflict('translations:conflict')
            entries[key] = value
        signature = digest(source)
        with _lock:
            data = self.load()
            receipt = data['operations'].get(request_key)
            if receipt:
                if receipt['digest'] != signature:
                    raise StateConflict('catalog:request_conflict')
                return CommitReport(receipt['revision'],receipt['inserted'],receipt['skipped'],tuple(issues))
            if expected_revision is not None and expected_revision != data['revision']:
                raise StateConflict('catalog:revision_conflict')
            for key,value in entries.items():
                if key in data['entries'] and data['entries'][key] != value:
                    raise StateConflict('translations:conflict')
            inserted = len(entries.keys()-data['entries'].keys())
            skipped = len(source)-inserted
            data['entries'].update(entries)
            data['revision'] += 1
            data['operations'][request_key] = {'digest':signature,'revision':data['revision'],'inserted':inserted,'skipped':skipped}
            atomic_json(self.path,data)
            return CommitReport(data['revision'],inserted,skipped,tuple(issues))
