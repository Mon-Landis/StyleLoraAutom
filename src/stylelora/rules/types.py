from dataclasses import dataclass, asdict
from collections.abc import Mapping
from stylelora.project_db.types import Tag, RulePlan
from stylelora.project_db.errors import ValidationError
from stylelora.tagging.normalization import normalize_tag
from stylelora.tagging.tokenizer import syntax_of


@dataclass(frozen=True)
class RuleGroup:
    mode: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class RuleSpec:
    delete: RuleGroup | None = None
    write: RuleGroup | None = None
    condition: RuleGroup | None = None
    schema_version: int = 1

    def to_json(self):
        value = asdict(self)
        for key in ('delete','write','condition'):
            if value[key]:
                value[key]['tags'] = list(value[key]['tags'])
        return value


def parse_rule(value, *, catalog=None):
    if isinstance(value,RuleSpec):
        value = value.to_json()
    if (not isinstance(value,dict) or set(value) != {'schema_version','delete','write','condition'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValidationError('rule:invalid')
    groups = {}
    for key in ('delete','write','condition'):
        group = value[key]
        if group is None:
            groups[key] = None
            continue
        if not isinstance(group,dict) or set(group) != {'mode','tags'} or group['mode'] not in ('all','any') or not isinstance(group['tags'],list):
            raise ValidationError('rule:invalid_group')
        tags = []
        for tag in group['tags']:
            if not isinstance(tag,str) or not normalize_tag(tag):
                raise ValidationError('rule:invalid_tag')
            tag = normalize_tag(tag)
            known = catalog is not None and catalog.lookup(tag).match in ('known','alias')
            if ',' in tag or syntax_of(tag,known=known) != 'plain':
                raise ValidationError('tag:unverifiable')
            if tag not in tags:
                tags.append(tag)
        groups[key] = RuleGroup(group['mode'],tuple(tags)) if tags else None
    if not groups['delete'] and not groups['write']:
        raise ValidationError('rule:empty')
    return RuleSpec(**groups)


@dataclass(frozen=True)
class RuleDelta:
    image_id: int
    before: tuple[Tag, ...]
    after: tuple[Tag, ...]
    removed: tuple[str, ...]
    added: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True)
class RulePreview:
    preview_id: str
    digest: str
    plan: RulePlan
    matched_count: int
    changed_count: int
    examples: tuple[RuleDelta, ...]
    reasons: Mapping[str, int]
