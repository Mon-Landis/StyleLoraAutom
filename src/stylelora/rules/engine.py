from stylelora.tagging.normalization import normalize_tag
from stylelora.project_db.types import Tag
from .types import RuleDelta


def evaluate_rule(rule, view, condition_tags):
    before = view.tags
    keys = {normalize_tag(t.tag) for t in before}
    def unchanged(reason):
        return RuleDelta(view.image_id,before,before,(),(),reason)
    if rule.condition:
        original = {normalize_tag(t.tag) for t in condition_tags} if condition_tags is not None else set()
        required = set(rule.condition.tags)
        if not (required <= original if rule.condition.mode == 'all' else bool(required & original)):
            return unchanged('rule:condition_unmet')
    removed = set()
    if rule.delete:
        requested = set(rule.delete.tags)
        removed = keys & requested
        if not removed or rule.delete.mode == 'all' and not requested <= keys:
            return unchanged('rule:delete_unmet')
    remaining = keys - removed
    added = ()
    if rule.write:
        requested = rule.write.tags
        if rule.write.mode == 'all' and remaining & set(requested):
            # Deletion succeeds independently; write-all does not roll it back.
            added = ()
        else:
            added = tuple(t for t in requested if t not in remaining)
    after = tuple(t for t in before if normalize_tag(t.tag) not in removed) + tuple(Tag(t) for t in added)
    return RuleDelta(view.image_id,before,after,tuple(t.tag for t in before if t.tag in removed),added,None)
