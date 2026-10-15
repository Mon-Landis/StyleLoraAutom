from stylelora.project_db.types import Tag
from stylelora.project_db.errors import ValidationError
from stylelora.tagging.normalization import normalize_tag
from stylelora.tagging.tokenizer import syntax_of
from .blocklist import check_blocklist
from .sorters import checked_sort
from .types import CaptionResult


def assemble_caption(view,config,trigger,guard,sorter=None,*,profile=None):
    from stylelora.modes.profiles import get_profile
    profile = profile or get_profile()
    if not profile.enabled or profile.trigger_semantics != 'style_identity' or guard.policy != profile.blocklist_policy:
        raise ValidationError('profile:unavailable')
    trigger = normalize_tag(trigger)
    if not trigger or ',' in trigger or syntax_of(trigger) != 'plain':
        raise ValidationError('trigger:invalid')
    nl = view.nl if config.include_nl else None
    hits = check_blocklist(view.tags,nl,guard)
    trigger_hits = check_blocklist((Tag(trigger),),None,guard)
    warnings = ['blocklist:hit'] if hits else []
    if guard.unverifiable:
        warnings.append('blocklist:style_unverifiable')
    removed = ()
    if guard.mode == 'remove':
        if trigger_hits:
            raise ValidationError('trigger:blocklist_conflict')
        if guard.unverifiable or any(h.nl_span is not None or h.certainty != 'exact' for h in hits):
            raise ValidationError('blocklist:nl_review_required')
        removed = tuple(dict.fromkeys(h.tag for h in hits))
    elif trigger_hits:
        warnings.append('trigger:blocklist_conflict')
    tags = tuple(t for t in view.tags if t.tag not in removed and normalize_tag(t.tag) != trigger)
    if config.tag_sort == 'prompt_pyramid':
        values = checked_sort(sorter,tuple(t.tag for t in tags))
        lookup = {t.tag:t for t in tags}
        tags = tuple(lookup[t] for t in values)
    if not view.tags:
        warnings.append('caption:empty_tags')
    return CaptionResult((Tag(trigger),)+tags,nl,tuple(warnings),removed,True,config.tag_sort)
