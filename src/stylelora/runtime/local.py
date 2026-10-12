"""Local project adapters for audit and resource metadata."""
from stylelora.project_db import read_snapshot, submit_write
from stylelora.project_db.repositories import images, annotations, schemes, sources
from stylelora.exchange.files import all_pages, image_lease, sha256
from stylelora.project_db.types import PageRequest, ItemFilter
from .contracts import Failure, check, fields, plain, version


def verify(rt, ctx, identity, token):
    with image_lease(ctx.handle, identity):
        with read_snapshot(ctx.handle) as db:
            row = images.get_image(db, identity)
        check(token, rt.images.image_version(ctx, row))
        path = ctx.handle.root/'images'/row.filename
        if path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
            raise Failure(422, 'path:unsafe')
        if not path.is_file():
            raise Failure(410, 'image:file_missing')
        if sha256(path) != row.sha256:
            raise Failure(409, 'image:hash_mismatch')
        result = submit_write(ctx.handle, lambda db: images.set_file_missing(db, identity,
                                missing=False, verified_sha256=row.sha256)).result()
        return rt.images.view(ctx, result.value)


def history(ctx, identity, params):
    source, kind = params.pop('source', None), params.pop('type', None)
    if source not in (None, 'original', 'wd', 'llm', 'manual', 'rule') or kind not in (None, 'tags', 'nl'):
        raise Failure(422, 'input:invalid')
    with read_snapshot(ctx.handle) as db:
        images.get_image(db, identity)
        rows = [plain(row) for layer in ((source,) if source else ('original', 'wd', 'llm', 'manual', 'rule'))
                for typ in ((kind,) if kind else ('tags', 'nl'))
                for row in all_pages(lambda p: annotations.list_history(db, identity, layer, typ, p))]
    return sorted(rows, key=lambda r: r['annotation_id'])


def set_default(rt, ctx, identity, value, resources):
    fields(value, {'expected_versions'}, {'expected_versions'})
    versions = value['expected_versions']
    if not isinstance(versions, dict):
        raise Failure(422, 'input:invalid')
    def work(db):
        target = schemes.get_import_scheme(db, identity)
        old = schemes.get_default_import_scheme(db)
        if set(versions) != {str(identity), str(old.scheme_id)}:
            raise Failure(422, 'input:invalid')
        for row in (target, old):
            check(versions[str(row.scheme_id)], resources.token(ctx, 'import-schemes', row))
        return resources.view(ctx, 'import-schemes', schemes.set_default_import_scheme(db, identity).value)
    return submit_write(ctx.handle, work).result()


def import_summary(ctx, identity):
    with read_snapshot(ctx.handle) as db:
        source = sources.get_source(db, identity)
        if source.type != 'import':
            raise Failure(422, 'source:not_import')
        rows = [plain(r) for r in all_pages(lambda p: sources.list_items(db, ItemFilter(identity), p))]
        return sources.count_item_states(db, identity), rows


def bulk_manual(rt, ctx, value, resources):
    from stylelora.annotations.editing import bulk_edit_manual
    from stylelora.project_db.types import CurrentRef, Tag
    from .contracts import positive
    fields(value, {'ids', 'add', 'remove', 'read_set', 'expected_versions'},
           {'ids', 'add', 'remove', 'read_set', 'expected_versions'})
    if not isinstance(value['ids'], list) or not value['ids']:
        raise Failure(422, 'input:invalid')
    ids = [positive(i) for i in value['ids']]
    if len(ids) > 5000:
        raise Failure(413, 'batch:too_large')
    fields(value['expected_versions'], {str(i) for i in ids}, {str(i) for i in ids})
    if not isinstance(value['read_set'], list):
        raise Failure(422, 'input:invalid')
    refs = {i: [] for i in ids}
    for raw in value['read_set']:
        ref = CurrentRef(**fields(raw, {'image_id', 'source', 'type', 'annotation_id'},
                                 {'image_id', 'source', 'type', 'annotation_id'}))
        if ref.image_id not in refs:
            raise Failure(422, 'input:invalid')
        refs[ref.image_id].append(ref)
    for i in ids:
        check(value['expected_versions'][str(i)], resources.annotation(ctx, i)['version'])
    def tags(key):
        if not isinstance(value[key], list):
            raise Failure(422, 'input:invalid')
        return tuple(Tag(**fields(t, {'tag', 'conf'}, {'tag'})) if isinstance(t, dict) else Tag(t) for t in value[key])
    return plain(bulk_edit_manual(ctx.handle, ids, add=tags('add'), remove=tags('remove'),
                                 expected=refs, catalog=rt.catalog))


def import_root(rt, ctx, identity, value, token, resources):
    from stylelora.exchange.importer import scan_import
    fields(value, {'root_handle'}, {'root_handle'})
    with read_snapshot(ctx.handle) as db:
        source = sources.get_source(db, identity)
    check(token, resources.token(ctx, 'sources', source))
    if source.type != 'import':
        raise Failure(422, 'source:not_import')
    root = rt.grant_path(value['root_handle'], 'import_root')
    manifest = scan_import(root, recursive=source.config_json['params'].get('recursive', True),
                           project_root=ctx.handle.root)
    if plain(manifest.entries) != source.config_json.get('manifest_evidence'):
        raise Failure(409, 'import:input_changed')
    if not hasattr(rt, 'import_roots'):
        rt.import_roots = {}
    rt.import_roots[(ctx.project_id, identity)] = value['root_handle']
    return dict(source_id=identity, verified=True)


def reconcile(rt, ctx, value, token):
    from stylelora.project_db.recovery import inspect_sent, reconcile_item
    from stylelora.project_db.types import SourceFilter
    fields(value, {'item_ids'})
    check(token, rt.project_view(ctx)['version'])
    with read_snapshot(ctx.handle) as db:
        if 'item_ids' in value:
            if not isinstance(value['item_ids'], list) or len(value['item_ids']) > 5000:
                raise Failure(422, 'input:invalid')
            rows = [sources.get_item(db, i) for i in value['item_ids']]
        else:
            rows = [item for source in all_pages(lambda p: sources.list_sources(db, SourceFilter(), p))
                    for item in all_pages(lambda p: sources.list_items(db, ItemFilter(source.source_id, ('sent',)), p))]
    results = []
    for row in rows:
        if row.status != 'sent':
            continue
        evidence = inspect_sent(ctx.handle, row)
        submit_write(ctx.handle, lambda db: reconcile_item(db, evidence)).result()
        results.append({'item_id': row.item_id, 'verdict': evidence.verdict})
    return {'items': results}
