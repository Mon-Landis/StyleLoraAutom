from fastapi import APIRouter, Request
from stylelora.runtime.contracts import Failure, plain, version
from stylelora.prompts import dictionaries
from ..dependencies import runtime, expected, body
from .session import resource

router = APIRouter(prefix='/api')


def service(rt):
    if not hasattr(rt, 'catalog_service'):
        from stylelora.runtime.catalog import Catalog
        rt.catalog_service = Catalog(rt)
    return rt.catalog_service


@router.get('/llm/endpoints')
async def endpoints(request: Request):
    rt = runtime(request)
    def read():
        svc = service(rt)
        rows = [svc.endpoint_view(e) for e in rt.product.llm.load_endpoints()]
        rows.sort(key=lambda r:r['endpoint_id'])
        return svc.page(rows, dict(request.query_params), 'endpoints', version(rt.secret, rows))
    return await rt.product.call(read)


@router.get('/llm/endpoints/{endpoint_id}')
async def endpoint(endpoint_id: str, request: Request):
    rt = runtime(request)
    return resource(await rt.product.call(lambda: service(rt).endpoint_view(service(rt).endpoint(endpoint_id))))


@router.post('/llm/endpoints')
async def create_endpoint(request: Request):
    rt, value = runtime(request), await request.json()
    result = await rt.product.call(lambda: service(rt).save_endpoint(value))
    return resource(result, 201, '/api/llm/endpoints/'+result['endpoint_id'])


@router.put('/llm/endpoints/{endpoint_id}')
async def update_endpoint(endpoint_id: str, request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    return resource(await rt.product.call(lambda: service(rt).save_endpoint(value, endpoint_id, token)))


@router.post('/llm/endpoints/{endpoint_id}/tokens')
@router.put('/llm/endpoints/{endpoint_id}/tokens/{token_ref}')
async def secret(endpoint_id: str, request: Request, token_ref: str = None):
    rt, value = runtime(request), await request.json()
    token = expected(request) if token_ref else None
    result = await rt.product.call(lambda: service(rt).secret(endpoint_id, value, token, token_ref))
    return resource(result, 200 if token_ref else 201, '/api/llm/endpoints/'+endpoint_id+'/tokens/'+result['token_ref'])


@router.get('/llm/roles')
async def roles(request: Request):
    rt = runtime(request)
    return resource(await rt.product.call(lambda: service(rt).roles()))


@router.put('/llm/roles')
async def update_roles(request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    return resource(await rt.product.call(lambda: service(rt).save_roles(value, token)))


@router.get('/rules')
async def rules(request: Request):
    rt = runtime(request)
    return resource(await rt.product.call(lambda: service(rt).rule_library()))


@router.put('/rules')
async def update_rules(request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    return resource(await rt.product.call(lambda: service(rt).save_rules(value, token, request.headers.get('idempotency-key', request.state.request_id))))


@router.get('/dictionaries')
async def dictionary_list(request: Request):
    rt = runtime(request)
    def read():
        svc = service(rt)
        rows = [svc.dictionary_view(d) for d in dictionaries.list_dictionaries(root=svc.dictionary_root, kind=request.query_params.get('kind'))]
        rows = [r for r in rows if request.query_params.get('q', '').casefold() in r['name'].casefold()]
        rows.sort(key=lambda r:(r['name'], r['dictionary_id']))
        return svc.page(rows, dict(request.query_params), 'dictionaries', version(rt.secret, rows))
    return await rt.product.call(read)


@router.get('/dictionaries/{dictionary_id}')
async def dictionary(dictionary_id: str, request: Request):
    rt = runtime(request)
    return resource(await rt.product.call(lambda: service(rt).dictionary(dictionary_id)))


@router.post('/dictionaries')
@router.put('/dictionaries/{dictionary_id}')
async def save_dictionary(request: Request, dictionary_id: str = None):
    rt, value = runtime(request), await request.json()
    token = expected(request) if dictionary_id else None
    result = await rt.product.call(lambda: service(rt).save_dictionary(value, request.headers.get('idempotency-key', request.state.request_id), dictionary_id, token))
    return resource(result, 200 if dictionary_id else 201, '/api/dictionaries/'+result['dictionary_id'])


@router.get('/dictionaries/{dictionary_id}/entries')
async def entries(dictionary_id: str, request: Request):
    rt = runtime(request)
    def read():
        svc = service(rt)
        value = svc.dictionary(dictionary_id)
        rows = value.get('entries', []) if value['kind'] == 'prompts' else value['dimensions'].get(request.query_params.get('dimension'), [])
        return svc.page(rows, dict(request.query_params), 'entries/'+dictionary_id, value['version'])
    return await rt.product.call(read)


@router.get('/tags/catalog-status')
async def catalog_status(request: Request):
    catalog = runtime(request).catalog
    return dict(available=bool(catalog and catalog.available), complete=bool(catalog and catalog.complete),
                revision=catalog.revision if catalog else None)


@router.get('/tags/search')
async def search(request: Request):
    rt = runtime(request)
    if request.query_params.get('provider', 'local') != 'local' or rt.catalog is None:
        raise Failure(503, 'dependency:unavailable')
    params = dict(request.query_params)
    params.pop('provider', None)
    def work():
        rows = plain(rt.catalog.search(params.get('q', ''), limit=1000))
        return service(rt).page(rows, params, 'tags', rt.catalog.revision)
    return await rt.product.call(work)


@router.post('/validation/trigger')
async def trigger(request: Request):
    from stylelora.tagging.trigger_validation import validate_trigger
    rt = runtime(request)
    value = await body(request, {'trigger', 'style_prompts'}, {'trigger', 'style_prompts'})
    if not isinstance(value['trigger'], str) or not isinstance(value['style_prompts'], list) or any(not isinstance(s,str) for s in value['style_prompts']):
        raise Failure(422, 'input:invalid')
    return {'issues': [plain(i) for style in value['style_prompts'] for i in validate_trigger(value['trigger'], style, catalog=rt.catalog)]}
