def normalize_tag(value: str) -> str:
    return value.strip().lower().replace('_', ' ').strip()


def resolve_input_tag(value, catalog, *, collapse_spaces=False):
    value = normalize_tag(value)
    return catalog.lookup(' '.join(value.split()) if collapse_spaces else value)
