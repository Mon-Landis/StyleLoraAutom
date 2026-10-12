import re
from .normalization import normalize_tag
from .tokenizer import tokenize_prompt, syntax_of
from .types import Issue
from stylelora.project_db.validation import digest


def override_digest(trigger, catalog):
    return digest({'trigger':normalize_tag(trigger),'catalog':catalog.revision})


def validate_trigger(trigger, style_prompt, *, catalog=None, trigger_override_digest=None):
    key = normalize_tag(trigger)
    if not key:
        return (Issue('trigger:missing',severity='error'),)
    if ',' in key or any(c in trigger for c in '\r\n') or syntax_of(key) != 'plain':
        return (Issue('trigger:invalid',severity='error'),)
    issues = []
    normalized_style = ' '.join(normalize_tag(style_prompt).split())
    if ' '.join(key.split()) in normalized_style:
        issues.append(Issue('trigger:in_style_prompt',severity='error'))
    if catalog is None or not catalog.complete:
        issues.append(Issue('trigger:unverifiable',severity='error'))
    if catalog is not None:
        if any(t.syntax != 'plain' for t in tokenize_prompt(style_prompt,catalog=catalog) if t.normalized):
            issues.append(Issue('trigger:unverifiable',severity='error'))
        hit = catalog.lookup(key)
        if hit.match in ('known','alias'):
            issues.append(Issue('trigger:existing_tag',field=override_digest(trigger,catalog)))
    return tuple(issues)
