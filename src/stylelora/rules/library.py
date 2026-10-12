from pathlib import Path
from uuid import UUID, uuid4
from stylelora.tagging.jsonio import loads, atomic_json
from stylelora.tagging.translations import CommitReport, _lock
from stylelora.tagging.migration import preview_tagcleaner_rules
from stylelora.project_db.validation import digest
from stylelora.project_db.errors import ValidationError, StateConflict
from .types import parse_rule


class RuleLibrary:
    def __init__(self,path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {'schema_version':1,'kind':'replacement_rules','revision':0,'rules':[],'operations':{}}
        value = loads(self.path.read_bytes())
        self.validate(value)
        return value

    @staticmethod
    def validate(value):
        if (not isinstance(value,dict) or set(value) != {'schema_version','kind','revision','rules','operations'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['kind'] != 'replacement_rules' or type(value['revision']) is not int
                or not isinstance(value['rules'],list) or not isinstance(value['operations'],dict)):
            raise ValidationError('rules:invalid_library')
        ids = set()
        for row in value['rules']:
            if set(row) != {'id','name','rule'} or not isinstance(row['name'],str):
                raise ValidationError('rules:invalid_library')
            try:
                UUID(row['id'])
            except (ValueError,TypeError,AttributeError) as exc:
                raise ValidationError('rules:invalid_id') from exc
            if row['id'] in ids:
                raise ValidationError('rules:duplicate_id')
            ids.add(row['id'])
            parse_rule(row['rule'])

    def save_rule_library(self,value,*,expected_revision,request_key):
        self.validate(value)
        signature = digest(value['rules'])
        with _lock:
            old = self.load()
            receipt = old['operations'].get(request_key)
            if receipt:
                if receipt['digest'] != signature:
                    raise StateConflict('rules:request_conflict')
                return CommitReport(receipt['revision'],receipt['inserted'],0)
            if old['revision'] != expected_revision:
                raise StateConflict('catalog:revision_conflict')
            result = {**value,'revision':old['revision']+1,'operations':old['operations']}
            result['operations'][request_key] = {'digest':signature,'revision':result['revision'],'inserted':len(value['rules'])}
            atomic_json(self.path,result)
            return CommitReport(result['revision'],len(value['rules']),0)

    def import_tagcleaner_rules(self,data,*,request_key,expected_revision):
        rows, issues = preview_tagcleaner_rules(data)
        if any(i.severity == 'error' for i in issues):
            raise ValidationError('rules:invalid_migration')
        from uuid import uuid5, NAMESPACE_URL
        signature = digest(loads(data))
        with _lock:
            value = self.load()
            for index,row in enumerate(rows):
                identity = str(uuid5(NAMESPACE_URL,signature+str(index)))
                if not any(r['id']==identity for r in value['rules']):
                    value['rules'].append({'id':identity,**row})
            result = self.save_rule_library(value,expected_revision=expected_revision,request_key=request_key)
            return CommitReport(result.revision,result.inserted,result.skipped,issues)
