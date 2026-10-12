from uuid import uuid4
from stylelora.tagging.jsonio import loads, atomic_json
from stylelora.tagging.catalog import LocalCatalog
from stylelora.tagging.translations import TranslationStore
from stylelora.tagging.migration import preview_tagcleaner_rules
from .contracts import Failure, plain, version, check


class CatalogImports:
    def __init__(self, rt):
        self.rt, self.previews = rt, {}

    def preview(self, raw, kind, format):
        if format != 'json' or kind not in ('rules', 'catalog', 'translations'):
            raise Failure(422, 'schema:invalid')
        issues = []
        value = loads(raw)
        if kind == 'rules':
            _, issues = preview_tagcleaner_rules(raw)
        elif kind == 'catalog':
            LocalCatalog(value)
        elif not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
            raise Failure(422, 'schema:invalid')
        else:
            LocalCatalog(translations=value)
        identity = str(uuid4())
        digest = version(self.rt.secret, kind, value)
        self.previews[identity] = (kind, raw, digest, self.rt.clock()+900)
        return dict(preview_id=identity, digest=digest, issues=plain(issues), expires_at=self.rt.clock()+900)

    def commit(self, value, token, key, catalog, rules=False):
        entry = self.previews.get(value['preview_id'])
        if not entry or entry[3] <= self.rt.clock():
            raise Failure(410, 'preview:expired')
        kind, raw, digest, _ = entry
        if digest != value['digest'] or (kind == 'rules') != rules:
            raise Failure(409, 'preview:stale')
        if rules:
            current = catalog.rule_library()
            check(token, current['version'])
            return plain(catalog.rules.import_tagcleaner_rules(raw, request_key=key, expected_revision=current['revision']))
        if kind == 'translations':
            store = TranslationStore(self.rt.product.root/'translations.json')
            current = store.load()
            check(token, str(current['revision']))
            return plain(store.import_tagcleaner_translations(raw, request_key=key, expected_revision=current['revision']))
        from stylelora.llm.config import read_json
        path = self.rt.product.root/'tagging'/'catalog.json'
        current = read_json(path, {'revision': 0})
        check(token, str(current['revision']))
        document = loads(raw)
        document['revision'] = current['revision']+1
        updated = LocalCatalog(document)
        atomic_json(path, document)
        self.rt.catalog = updated
        return dict(revision=document['revision'], inserted=len(document['tags']), skipped=0)
