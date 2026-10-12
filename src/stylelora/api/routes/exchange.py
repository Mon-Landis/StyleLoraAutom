from fastapi import APIRouter, Request
from stylelora.runtime.contracts import Failure, check
from ..dependencies import runtime, expected
from .session import resource, accepted

router = APIRouter(prefix='/api/projects/{project_id}')


def service(rt):
    if not hasattr(rt, 'exchange'):
        from stylelora.runtime.exchange import Exchange
        rt.exchange = Exchange(rt)
    return rt.exchange


@router.post('/imports/preview')
@router.post('/exports/preview')
async def preview(project_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    ctx = rt.require_project(project_id)
    kind = 'import' if '/imports/' in request.url.path else 'export'
    async def work():
        return await rt.blocking(lambda: service(rt).preview(ctx, kind, value))
    return accepted(rt, kind+'_preview', work, project_id)


@router.post('/imports')
@router.post('/exports')
async def execute(project_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    async with rt.lease(project_id) as ctx:
        kind = 'import' if request.url.path.endswith('/imports') else 'export'
        result = await service(rt).execute(ctx, kind, value)
    location = '/api/operations/'+result['operation_id'] if kind == 'import' else f'/api/projects/{project_id}/exports/'+result['export_id']
    return resource(result, 202, location)


@router.get('/exports')
async def exports(project_id: str, request: Request):
    rt = runtime(request)
    ctx = rt.require_project(project_id)
    await rt.blocking(lambda: service(rt).restore(ctx))
    from .product import service as catalog
    from stylelora.runtime.contracts import version
    rows = sorted([dict(v[0]) for v in service(rt).exports.values() if v[0]['project_id'] == project_id],
                  key=lambda r: r['export_id'])
    return catalog(rt).page(rows, dict(request.query_params), 'exports/'+project_id, version(rt.secret, rows))


@router.get('/exports/{export_id}')
@router.get('/exports/{export_id}/report')
async def get_export(project_id: str, export_id: str, request: Request):
    rt = runtime(request)
    async with rt.lease(project_id) as ctx:
        return resource(await rt.blocking(lambda: service(rt).get(ctx, export_id, request.url.path.endswith('/report'))))


@router.post('/exports/{export_id}/cancel')
async def cancel(project_id: str, export_id: str, request: Request):
    rt = runtime(request)
    svc = service(rt)
    value = svc.get(rt.require_project(project_id), export_id)
    check(expected(request), value['version'])
    if value['status'] == 'finished':
        raise Failure(409, 'export:already_finished')
    svc.exports[export_id][1].set()
    return resource(value)
