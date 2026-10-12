from .jsonio import loads
from .types import Issue
from stylelora.rules.types import parse_rule
from stylelora.project_db.errors import ValidationError


def preview_tagcleaner_rules(data):
    values = loads(data)
    if not isinstance(values,list):
        raise ValidationError('rules:invalid_library')
    result, issues = [], [Issue('rules:condition_semantics','Conditions use original; deletions use effective tags.')]
    allowed = {'id','name','delete','write','condition','deleteMode','writeMode','conditionMode'}
    for index, value in enumerate(values):
        if not isinstance(value,dict) or set(value)-allowed:
            issues.append(Issue('rules:unknown_field',severity='error',field=str(index)))
            continue
        rule = {'schema_version':1}
        for key in ('delete','write','condition'):
            tags = value.get(key,[])
            if isinstance(tags,str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]
            rule[key] = {'mode':value.get(key+'Mode','any'),'tags':tags} if tags else None
        try:
            result.append({'name':str(value.get('name','Imported rule')),'rule':parse_rule(rule).to_json()})
        except ValidationError:
            issues.append(Issue('rules:invalid_rule',severity='error',field=str(index)))
    return tuple(result), tuple(issues)
