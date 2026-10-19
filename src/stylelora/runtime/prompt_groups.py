"""F27 application service; GroupRef never accepts a filesystem path."""
from dataclasses import replace
from uuid import uuid4
from stylelora.prompts.groups import (Group, GroupRef, GeneratedFields, FieldVersions,
                                      BASE_FIELDS, _check, _variant_key,
                                      validate_entry, content, freeze_groups)
from stylelora.prompts.group_store import GroupStore, prepare_preview
from stylelora.prompts.group_exchange import GroupExchange
from stylelora.prompts.dictionaries import digest
from stylelora.prompts.types import plain
from stylelora.prompts.validation import require, identifier
from stylelora.prompts.prompt_normalize import normalize_prompt
from .contracts import Failure


def normalize_fields(patch):
    require(isinstance(patch, dict))
    values = dict(patch)
    source = values.pop('source_format', 'nai')
    require(source in ('nai', 'anima'))
    require(not set(values) - {'nl', 'variants', 'disable', 'aspect'})
    if 'nl' in values:
        values['nl'] = normalize_prompt(values['nl'], source).text
    if 'variants' in values:
        _check(isinstance(values['variants'], dict), 'variants')
        variants = {}
        for key, value in values['variants'].items():
            _check(value is None or isinstance(value, str), f'variants[{key!r}]')
            variants[key] = normalize_prompt(value, source).text if value is not None and value.strip() else value
        values['variants'] = variants
    return values


def apply_user_patch(entry, patch):
    patch = normalize_fields(patch)
    require(isinstance(patch, dict) and not set(patch)-set(content(entry)))
    values = content(entry) | patch
    touched = set(patch) & BASE_FIELDS
    variants = dict(entry.variants)
    marks = dict(entry.generated.variants)
    versions = dict(entry.edit_versions.variants)
    if 'variants' in patch:
        raw = patch['variants']
        _check(isinstance(raw, dict), 'variants')
        seen = set()
        for key, value in raw.items():
            path = f'variants[{key!r}]'
            key = _variant_key(key, path)
            _check(key not in seen, path)
            seen.add(key)
            _check(value is None or isinstance(value, str), path)
            versions[key] = versions.get(key, 0) + 1
            if value is None:
                variants.pop(key, None)
                marks.pop(key, None)
            else:
                variants[key] = value
                marks[key] = False
        values['variants'] = variants
    updated = validate_entry(values | {'entry_id': entry.entry_id})
    generated = {k: getattr(entry.generated, k) for k in BASE_FIELDS}
    edits = {k: getattr(entry.edit_versions, k) for k in BASE_FIELDS}
    for field in touched:
        generated[field] = False
        edits[field] += 1
    return replace(updated, preview=entry.preview, pending=entry.pending,
                   completion=updated.completion if touched else entry.completion,
                   generated=GeneratedFields(**generated, variants=marks),
                   edit_versions=FieldVersions(**edits, variants=versions))


class PromptGroups:
    def __init__(self, rt):
        self.rt = rt
        self.store = GroupStore(rt.product.root, self.project,
                                auto_complete_default=lambda: rt.product.get()['prompt_auto_complete_default'])
        self.exchange = GroupExchange(self.store, rt.clock)
        self.pages = {}
        self.revealed_this_run = False

    def project(self):
        ctx = self.rt.current
        return (ctx.project_id, ctx.handle.root) if ctx and not ctx.closing else None

    def ref(self, identity, params):
        current = self.project()
        scope = params.get('scope', 'global')
        return GroupRef(scope, params.get('project_token'), identity)

    def summary(self, group):
        result = {key: getattr(group, key) for key in ('dictionary_id', 'name', 'kind', 'category', 'schema_version', 'scope', 'nsfw', 'revision', 'auto_complete')}
        result.update(entry_count=len(group.entries), version=str(group.revision),
                      project_token=self.project()[0] if group.scope == 'project' and self.project() else None)
        return result

    def page(self, rows, params, binding, revision=None, key=lambda x: x['entry_id']):
        try:
            limit = int(params.get('limit', 50))
        except (TypeError, ValueError):
            raise Failure(422, 'pagination:invalid') from None
        if not 1 <= limit <= 200:
            raise Failure(422, 'pagination:invalid')
        binding = digest([binding, self.project()[0] if self.project() else None, revision])
        cursor = params.get('cursor')
        after = None
        if cursor:
            frozen = self.pages.get(cursor)
            if frozen is None:
                raise Failure(422, 'pagination:invalid')
            if frozen[0] != binding:
                raise Failure(409, 'pagination:stale')
            after = frozen[1]
        rows = sorted(rows, key=key)
        if after is not None:
            rows = [row for row in rows if key(row) > after]
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = str(uuid4()); self.pages[next_cursor] = (binding, key(selected[-1]))
        return dict(items=selected, next_cursor=next_cursor)

    def listing(self, params):
        require(not set(params)-{'scope', 'nsfw', 'q', 'limit', 'cursor', 'project_token'})
        require(params.get('scope') in (None, 'global', 'project'))
        require(params.get('nsfw') in (None, 'true', 'false'))
        if params.get('project_token'):
            require(self.project() and params['project_token'] == self.project()[0], 'project:closed')
        groups, issues = self.store.list()
        rows = [self.summary(g) for g in groups if (not params.get('scope') or g.scope == params['scope']) and
                (params.get('nsfw') is None or g.nsfw == (params['nsfw'] == 'true')) and
                params.get('q', '').casefold() in g.name.casefold()]
        result = self.page(rows, params, {k:v for k,v in params.items() if k not in ('limit', 'cursor')},
                           key=lambda g: (0 if g['scope'] == 'project' else 1, g['dictionary_id']))
        result['issues'] = plain(issues)
        return result

    def create(self, value, key):
        require(isinstance(value, dict) and not set(value)-{'dictionary_id', 'name', 'kind', 'category', 'nsfw', 'scope', 'project_token', 'auto_complete'})
        current = self.project()
        scope = value.get('scope', 'project' if current else 'global')
        # A deterministic identity makes retries across process restarts address the receipt.
        from uuid import uuid5, NAMESPACE_URL
        identity = value.get('dictionary_id', str(uuid5(NAMESPACE_URL, 'stylelora:f27:'+key)))
        ref = GroupRef(scope, value.get('project_token', current[0] if current and scope == 'project' else None), identity)
        def change(old):
            require(old is None, 'group:id_conflict')
            default = value['auto_complete'] if 'auto_complete' in value else self.store.auto_complete_default()
            group = Group(identity, value.get('name', ''), scope, value.get('nsfw', False),
                          kind=value.get('kind', 'prompts'), category=value.get('category', 'full_content'),
                          auto_complete=default)
            return group, self.summary(group)
        return self.store.mutate(ref, 0, key, 'create', value, change)

    def entry(self, group, identity):
        identifier(identity)
        entry = next((e for e in group.entries if e.entry_id == identity), None)
        require(entry is not None, 'entry:not_found')
        return entry

    def edit(self, ref, patch, revision, key, entry_id=None, action='patch'):
        def change(group):
            require(group is not None, 'dictionary:not_found')
            if entry_id is None and action == 'patch':
                require(isinstance(patch, dict) and not set(patch)-{'name', 'nsfw', 'auto_complete'})
                updated = replace(group, **patch, revision=revision+1)
                return updated, self.summary(updated)
            entries = list(group.entries)
            if action == 'add':
                require('entry_id' not in patch)
                entry = validate_entry(normalize_fields(patch), allow_id=False)
                entries.append(entry)
            else:
                entry = self.entry(group, entry_id)
                index = entries.index(entry)
                if action == 'delete':
                    entries.pop(index)
                elif action == 'image':
                    asset = prepare_preview(patch, entry_id)
                    entry = replace(entry, preview=self.store.publish(asset, ref, entry_id))
                    entries[index] = entry
                else:
                    entry = apply_user_patch(entry, patch)
                    entries[index] = entry
            result = {'deleted': entry_id} if action == 'delete' else plain(entry)
            return replace(group, entries=tuple(entries)), result | {'nsfw': group.nsfw}
        import hashlib
        payload = {'entry_id': entry_id, 'patch': hashlib.sha256(patch).hexdigest() if isinstance(patch, bytes) else patch}
        return self.store.mutate(ref, revision, key, action, payload, change)

    def freeze(self, refs, mode, seed, count, default_aspect=None):
        from stylelora.prompts.dictionaries import LOCK
        with LOCK:
            return freeze_groups(tuple(self.store.load(ref) for ref in refs), mode, seed, count, default_aspect)


def service(rt):
    if not hasattr(rt, 'prompt_groups'):
        rt.prompt_groups = PromptGroups(rt)
    return rt.prompt_groups
