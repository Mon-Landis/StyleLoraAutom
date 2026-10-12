from fastapi import APIRouter, Request
from fastapi.responses import Response
from stylelora.runtime.contracts import Failure, check, fields, plain
from stylelora.prompts import dictionaries, exchange
from ..dependencies import runtime, expected
from .product import service
from .session import resource

router = APIRouter(prefix='/api')


@router.get('/tagger/models')
async def tagger_models(request: Request):
    rt = runtime(request)
    rows = []
    for manifest in getattr(rt, 'model_manifests', ()):
        from stylelora.vision.models import ModelStore, validate_manifest
        validate_manifest(manifest)
        installed = False
        try:
            ModelStore().verify_model(manifest)
            installed = True
        except Exception:
            pass
        rows.append(plain(manifest) | {'manifest_id': manifest.digest, 'installed': installed})
    from stylelora.runtime.contracts import version
    return service(rt).page(rows, dict(request.query_params), 'tagger_models', version(rt.secret, rows))


@router.post('/coverage/preview')
async def coverage_preview(request: Request):
    from stylelora.runtime.prompts import sampling
    from stylelora.prompts.sampling import plan_coverage
    from stylelora.prompts.types import FeatureDictionary
    from .session import accepted
    from uuid import uuid4
    rt, value = runtime(request), await request.json()
    query = sampling(value)
    if query.live_features:
        raise Failure(501, 'launcher:unsupported_schema')
    async def work():
        def run():
            root = service(rt).dictionary_root
            prompts = tuple(dictionaries.load_dictionary(i, root=root) for i in query.prompt_dictionary_ids)
            features = [dictionaries.load_dictionary(i, root=root) for i in query.feature_dictionary_ids]
            dims = {}
            for feature in features:
                for key, rows in feature.dimensions.items():
                    dims.setdefault(key, []).extend(rows)
            merged = FeatureDictionary(str(uuid4()), 'Preview', dimensions={k: tuple(v) for k, v in dims.items()}) if dims else None
            return plain(plan_coverage(query, dictionaries=prompts, features=merged, catalog=rt.catalog))
        return await rt.blocking(run)
    return accepted(rt, 'coverage_preview', work)


@router.post('/rules/imports/preview')
@router.post('/tags/imports/preview')
@router.post('/rules/imports/commit')
@router.post('/tags/imports/commit')
async def catalog_import(request: Request):
    from stylelora.runtime.catalog_imports import CatalogImports
    rt = runtime(request)
    if not hasattr(rt, 'catalog_imports'):
        rt.catalog_imports = CatalogImports(rt)
    rules = request.url.path.startswith('/api/rules')
    if request.url.path.endswith('/preview'):
        from ..uploads import upload
        form = await upload(request, {'file', 'kind', 'format'}, {'file'})
        raw = form['file']
        kind, format = 'rules' if rules else form.get('kind'), form.get('format', 'json')
        return await rt.product.call(lambda: rt.catalog_imports.preview(raw, kind, format))
    value = fields(await request.json(), {'preview_id', 'digest'}, {'preview_id', 'digest'})
    token = expected(request)
    return await rt.product.call(lambda: rt.catalog_imports.commit(value, token,
        request.headers['idempotency-key'], service(rt), rules))


@router.get('/presets')
@router.get('/presets/{preset_id}')
@router.post('/presets')
@router.put('/presets/{preset_id}')
@router.delete('/presets/{preset_id}')
async def presets(request: Request, preset_id: str = None):
    from stylelora.runtime.presets import Presets
    from stylelora.runtime.contracts import version
    rt = runtime(request)
    value = await request.json() if request.method in ('POST', 'PUT') else None
    token = expected(request) if request.method in ('PUT', 'DELETE') else None
    def work():
        svc = Presets(rt)
        if request.method == 'GET':
            rows = svc.rows()
            return svc.get(preset_id) if preset_id else service(rt).page(rows,
                dict(request.query_params), 'presets', version(rt.secret, rows))
        return svc.write(value, preset_id, token, request.method == 'DELETE')
    result = await rt.product.call(work)
    return Response(status_code=204) if request.method == 'DELETE' else resource(result, 201 if request.method == 'POST' else 200)


@router.delete('/dictionaries/{dictionary_id}')
async def delete_dictionary(dictionary_id: str, request: Request):
    rt, token = runtime(request), expected(request)
    def work():
        svc = service(rt)
        value = svc.dictionary(dictionary_id)
        check(token, value['version'])
        dictionaries.delete_dictionary(dictionary_id, expected_revision=value['revision'], root=svc.dictionary_root)
    await rt.product.call(work)
    return Response(status_code=204)


@router.get('/dictionaries/{dictionary_id}/export')
async def export_dictionary(dictionary_id: str, request: Request):
    rt = runtime(request)
    fields(dict(request.query_params), {'format', 'dimension'})
    format = request.query_params.get('format', 'json')
    if format not in ('json', 'csv', 'txt'):
        raise Failure(422, 'input:invalid')
    data = await rt.product.call(lambda: exchange.export_dictionary(dictionary_id, format,
        root=service(rt).dictionary_root, dimension=request.query_params.get('dimension')))
    headers = {'Content-Disposition': f'attachment; filename="dictionary.{format}"'}
    if format == 'txt':
        headers['X-Lossy-Fields'] = 'aspect,rating,dictionary-metadata'
    return Response(data, media_type={'json': 'application/json', 'csv': 'text/csv', 'txt': 'text/plain'}[format], headers=headers)


@router.post('/dictionaries/imports/preview')
async def dictionary_preview(request: Request):
    rt = runtime(request)
    from ..uploads import upload
    form = await upload(request, {'file', 'format', 'kind', 'dimension'}, {'file', 'format', 'kind'})
    raw = form['file']
    value = await rt.product.call(lambda: exchange.preview_import(raw, format=form['format'],
        kind=form['kind'], dimension=form.get('dimension'), catalog=rt.catalog))
    if not hasattr(rt, 'dictionary_previews'):
        rt.dictionary_previews = {}
    rt.dictionary_previews[value.preview_id] = (value, rt.clock()+900)
    return plain(value)


@router.post('/dictionaries/{dictionary_id}/imports')
async def dictionary_commit(dictionary_id: str, request: Request):
    from dataclasses import replace
    rt, token = runtime(request), expected(request)
    value = fields(await request.json(), {'preview_id', 'digest'}, {'preview_id', 'digest'})
    def work():
        entry = getattr(rt, 'dictionary_previews', {}).get(value['preview_id'])
        if not entry or entry[1] <= rt.clock():
            raise Failure(410, 'preview:expired')
        preview = entry[0]
        if preview.digest != value['digest']:
            raise Failure(409, 'preview:stale')
        svc = service(rt)
        current = svc.dictionary(dictionary_id)
        check(token, current['version'])
        preview = replace(preview, base_revision=current['revision'])
        preview = replace(preview, digest=exchange.preview_digest(preview))
        return plain(exchange.commit_import(dictionary_id, preview,
            request_key=request.headers['idempotency-key'], root=svc.dictionary_root))
    return await rt.product.call(work)


@router.post('/tags/translations/lookup')
@router.put('/translations')
async def translations(request: Request):
    from stylelora.tagging.translations import TranslationStore
    from stylelora.tagging.normalization import normalize_tag
    rt, value = runtime(request), await request.json()
    token = expected(request) if request.method == 'PUT' else None
    def work():
        store = TranslationStore(rt.product.root/'translations.json')
        current = store.load()
        if request.method == 'PUT':
            fields(value, {'tag', 'value'}, {'tag', 'value'})
            check(token, str(current['revision']))
            result = store.put_translation(value['tag'], value['value'], expected_revision=current['revision'])
            return {'revision': result.value, 'version': str(result.value)}
        fields(value, {'tags', 'provider'}, {'tags'})
        if value.get('provider', 'local') != 'local':
            raise Failure(501, 'launcher:unsupported_schema')
        if not isinstance(value['tags'], list) or any(not isinstance(t, str) for t in value['tags']):
            raise Failure(422, 'input:invalid')
        if len(value['tags']) > 5000:
            raise Failure(413, 'batch:too_large')
        rows = []
        for tag in value['tags']:
            translation = current['entries'].get(normalize_tag(tag))
            if translation is None and rt.catalog:
                translation = rt.catalog.lookup(tag).translation
            rows.append({'tag': tag, 'translation': translation, 'unknown': translation is None})
        return {'items': rows, 'revision': current['revision'], 'version': str(current['revision'])}
    return resource(await rt.product.call(work))
