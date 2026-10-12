from fastapi import APIRouter, Request
from fastapi.responses import Response
from stylelora.runtime.contracts import Failure, check, plain
from ..dependencies import runtime, expected
from .session import resource

router = APIRouter(prefix='/api')


def service(rt):
    if not hasattr(rt, 'resources'):
        from stylelora.runtime.resources import Resources
        rt.resources = Resources(rt)
    return rt.resources


@router.get('/projects/{project_id}/sources')
@router.get('/projects/{project_id}/jobs')
@router.get('/projects/{project_id}/import-schemes')
@router.get('/projects/{project_id}/export-schemes')
@router.get('/projects/{project_id}/operation-log')
async def listing(project_id: str, request: Request):
    rt = runtime(request)
    kind = request.url.path.rsplit('/', 1)[-1]
    async with rt.lease(project_id) as ctx:
        return await rt.blocking(lambda: service(rt).list(ctx, kind, dict(request.query_params)))


@router.get('/projects/{project_id}/sources/{identity}')
@router.get('/projects/{project_id}/source-items/{identity}')
@router.get('/projects/{project_id}/jobs/{identity}')
@router.get('/projects/{project_id}/import-schemes/{identity}')
@router.get('/projects/{project_id}/export-schemes/{identity}')
@router.get('/projects/{project_id}/operation-log/{identity}')
async def get(project_id: str, identity: int, request: Request):
    rt = runtime(request)
    kind = request.url.path.rsplit('/', 2)[-2]
    async with rt.lease(project_id) as ctx:
        return resource(await rt.blocking(lambda: service(rt).get(ctx, kind, identity)))


@router.post('/projects/{project_id}/sources')
async def create_source(project_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    async with rt.lease(project_id) as ctx:
        result = await service(rt).source_create(ctx, value)
        return resource(result, 201, f'/api/projects/{project_id}/sources/{result["source_id"]}')


@router.patch('/projects/{project_id}/sources/{identity}')
@router.delete('/projects/{project_id}/sources/{identity}')
async def update_source(project_id: str, identity: int, request: Request):
    rt = runtime(request)
    delete = request.method == 'DELETE'
    value, token = {} if delete else await request.json(), expected(request)
    async with rt.lease(project_id) as ctx:
        result = await service(rt).source_update(ctx, identity, value, token, delete)
    return Response(status_code=204) if delete else resource(result)


@router.post('/projects/{project_id}/sources/{identity}/start')
@router.post('/projects/{project_id}/sources/{identity}/control')
@router.post('/projects/{project_id}/sources/{identity}/confirm-cost')
async def source_action(project_id: str, identity: int, request: Request):
    rt = runtime(request)
    value, token = await request.json(), expected(request)
    async with rt.lease(project_id) as ctx:
        result = await service(rt).source_action(ctx, identity, request.url.path.rsplit('/', 1)[-1], value, token)
    if 'operation_id' in result:
        return resource(result, 202, '/api/operations/' + result['operation_id'])
    return resource(result)


@router.get('/projects/{project_id}/sources/{identity}/items')
async def source_items(project_id: str, identity: int, request: Request):
    rt = runtime(request)
    async with rt.lease(project_id) as ctx:
        return await rt.blocking(lambda: service(rt).list(ctx, 'source-items', dict(request.query_params), identity))


@router.get('/projects/{project_id}/annotations/{identity}')
async def annotations(project_id: str, identity: int, request: Request):
    rt = runtime(request)
    async with rt.lease(project_id) as ctx:
        return resource(await rt.blocking(lambda: service(rt).annotation(ctx, identity)))


@router.put('/projects/{project_id}/annotations/{identity}/manual')
async def manual(project_id: str, identity: int, request: Request):
    rt = runtime(request)
    value, token = await request.json(), expected(request)
    async with rt.lease(project_id) as ctx:
        return await rt.blocking(lambda: service(rt).manual(ctx, identity, value, token))


@router.post('/projects/{project_id}/rules/preview')
async def rule_preview(project_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    async with rt.lease(project_id) as ctx:
        return resource(await rt.blocking(lambda: service(rt).rule_preview(ctx, value)))


@router.post('/projects/{project_id}/rules/apply')
async def rule_apply(project_id: str, request: Request):
    rt = runtime(request)
    value, token = await request.json(), expected(request)
    async with rt.lease(project_id) as ctx:
        return await rt.blocking(lambda: service(rt).rule_apply(ctx, value, token))


@router.get('/backends')
async def backends(request: Request):
    rt = runtime(request)
    return {'items': [{'backend_id': 'nai', 'available': rt.backend_factory is not None,
                       'reserved': False, 'reasons': [] if rt.backend_factory else ['backend:incompatible_schema']}],
            'limit': 50, 'has_more': False, 'next_cursor': None, 'upper_id': None}


@router.get('/backends/{backend_id}/account-status')
async def account(backend_id: str, request: Request):
    rt = runtime(request)
    if set(request.query_params) - {'project_id', 'source_id'}:
        raise Failure(422, 'input:invalid')
    pid, sid = request.query_params.get('project_id'), request.query_params.get('source_id')
    if not pid or not sid:
        return None
    try:
        identity = int(sid)
    except ValueError:
        raise Failure(422, 'input:invalid') from None
    async with rt.lease(pid) as ctx:
        row = await rt.blocking(lambda: service(rt).get(ctx, 'sources', identity))
        if row['type'] != backend_id:
            raise Failure(422, 'source:backend_mismatch')
        runner = rt.runners.get((pid, identity))
        return await rt.blocking(lambda: plain(runner.cost.account_status())) if runner else None


@router.get('/executors/capabilities')
@router.get('/tagger/capabilities')
async def capabilities(request: Request):
    return {'wd': {'available': False, 'reason': 'model_unavailable'},
            'llm_vision': {'available': False, 'reason': 'skill_unavailable'},
            'clean': {'available': False, 'reserved': True}, 'yolo': {'available': False, 'reserved': True}}


@router.get('/exchange/capabilities')
async def exchange_capabilities():
    return {'readers': ['txt_sidecar'], 'exporters': ['image_txt'], 'image_modes': ['copy'],
            'hardlink': 'unsupported', 'pipeline': 'unsupported', 'encodings': ['utf-8-sig', 'gbk']}


@router.post('/projects/{project_id}/source-items/{identity}/retry')
async def retry(project_id: str, identity: int, request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    async with rt.lease(project_id) as ctx:
        return resource(await service(rt).retry(ctx, identity, value, token))


@router.post('/projects/{project_id}/operation-log/{identity}/undo')
async def undo(project_id: str, identity: int, request: Request):
    rt, token = runtime(request), expected(request)
    async with rt.lease(project_id) as ctx:
        return await service(rt).undo(ctx, identity, token)


@router.post('/projects/{project_id}/import-schemes')
@router.post('/projects/{project_id}/export-schemes')
async def scheme_create(project_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    kind = request.url.path.rsplit('/', 1)[-1]
    async with rt.lease(project_id) as ctx:
        result = await service(rt).scheme_write(ctx, kind, value)
    return resource(result, 201, request.url.path+'/'+str(result['scheme_id']))


@router.put('/projects/{project_id}/import-schemes/{identity}')
@router.put('/projects/{project_id}/export-schemes/{identity}')
@router.delete('/projects/{project_id}/import-schemes/{identity}')
@router.delete('/projects/{project_id}/export-schemes/{identity}')
async def scheme_update(project_id: str, identity: int, request: Request):
    rt, token = runtime(request), expected(request)
    delete = request.method == 'DELETE'
    value = None if delete else await request.json()
    kind = request.url.path.rsplit('/', 2)[-2]
    async with rt.lease(project_id) as ctx:
        result = await service(rt).scheme_write(ctx, kind, value, identity, token, delete)
    return Response(status_code=204) if delete else resource(result)


@router.post('/projects/{project_id}/jobs/batches')
async def jobs_create(project_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    async with rt.lease(project_id) as ctx:
        result = await service(rt).jobs_create(ctx, value)
    return resource(result, 202, f'/api/projects/{project_id}/jobs')


@router.post('/projects/{project_id}/jobs/{identity}/control')
async def job_control(project_id: str, identity: int, request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    async with rt.lease(project_id) as ctx:
        return resource(await service(rt).job_control(ctx, identity, value, token))


@router.get('/projects/{project_id}/jobs/{identity}/outcomes')
async def job_outcomes(project_id: str, identity: int, request: Request):
    rt = runtime(request)
    async with rt.lease(project_id) as ctx:
        value = await rt.blocking(lambda: service(rt).get(ctx, 'jobs', identity))
    from .product import service as product
    params = dict(request.query_params)
    kind = params.pop('kind', None)
    rows = [dict(image_id=int(k), **v) for k,v in value['progress_json'].get('outcomes', {}).items() if kind is None or v['kind'] == kind]
    rows.sort(key=lambda v:v['image_id'])
    return product(rt).page(rows, params, f'job-outcomes/{project_id}/{identity}/{kind}', value['version'])
