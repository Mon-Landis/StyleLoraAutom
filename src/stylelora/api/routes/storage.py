from fastapi import APIRouter, Request
from stylelora.runtime.contracts import Failure, fields
from ..dependencies import runtime
from .session import accepted
from .product import service as catalog

router = APIRouter(prefix='/api')


def service(rt):
    if not hasattr(rt, 'storage'):
        from stylelora.runtime.storage import Storage
        rt.storage = Storage(rt)
    return rt.storage


@router.post('/projects/{project_id}/storage/scans')
async def scan(project_id: str, request: Request):
    rt = runtime(request)
    ctx = rt.require_project(project_id)
    async def work():
        return [await rt.blocking(lambda: service(rt).scan(ctx.handle.root, ctx))]
    return accepted(rt, 'storage_scan', work, project_id)


@router.get('/projects/{project_id}/storage/summary')
async def summary(project_id: str, request: Request):
    rt = runtime(request)
    rt.require_project(project_id)
    value = service(rt).summaries.get(project_id)
    return dict(value, stale=True) if value else None


@router.post('/storage/scans')
async def scan_known(request: Request):
    rt = runtime(request)
    value = fields(await request.json(), {'known_ids'})
    ids = value.get('known_ids', list(rt.known))
    if not isinstance(ids, list) or any(not isinstance(i, str) or i not in rt.known for i in ids):
        raise Failure(422, 'input:invalid')
    async def work():
        result = []
        for identity in ids:
            try:
                result.append(await rt.blocking(lambda: service(rt).scan(rt.known[identity], known_id=identity)))
            except (OSError, Failure):
                result.append(dict(known_id=identity, total_bytes=None, partial=True,
                                   errors=[{'code': 'storage:unavailable'}]))
        return result
    return accepted(rt, 'storage_scan', work)


@router.get('/storage/scans/{scan_id}')
async def scan_result(scan_id: str, request: Request):
    rt = runtime(request)
    operation = rt.supervisor.get(scan_id)
    if operation['kind'] != 'storage_scan':
        raise Failure(410, 'session:expired')
    params = dict(request.query_params)
    sort = params.pop('sort', 'known_id')
    if sort not in ('known_id', 'total_bytes', '-total_bytes'):
        raise Failure(422, 'input:invalid')
    rows = list(operation['result'] or [])
    rows.sort(key=lambda r: ((r.get('total_bytes') or 0) if 'total_bytes' in sort else (r.get('known_id') or ''),
                             r.get('known_id') or ''), reverse=sort.startswith('-'))
    return catalog(rt).page(rows, params,
                            'storage/' + scan_id + '/' + sort, operation['version']) | {'status': operation['status']}


@router.post('/projects/{project_id}/storage/cleanup/preview')
@router.post('/projects/{project_id}/storage/cleanup')
async def cleanup(project_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    ctx, svc = rt.require_project(project_id), service(rt)
    svc.busy(ctx)
    preview = request.url.path.endswith('/preview')
    async def work():
        return await rt.blocking(lambda: (svc.preview if preview else svc.cleanup)(ctx, value))
    return accepted(rt, 'cleanup_preview' if preview else 'cleanup', work, project_id)
