from fastapi import APIRouter, Request
from fastapi.responses import Response
from stylelora.runtime.contracts import Failure, plain, check, version
from ..dependencies import runtime, expected
from .session import resource
from .product import service as catalog

router = APIRouter(prefix='/api/prompt-tasks')


@router.post('/{task_id}/append')
async def append(task_id: str, request: Request):
    from stylelora.runtime.contracts import fields
    rt, value = runtime(request), await request.json()
    fields(value, {'dictionary_id', 'indices', 'expected_versions'}, {'dictionary_id', 'expected_versions'})
    def work():
        svc = service(rt)
        dictionary = catalog(rt).dictionary(value['dictionary_id'])
        keys = {task_id, value['dictionary_id']}
        versions = fields(value['expected_versions'], keys, keys)
        check(versions[task_id], svc.get(task_id)['version'])
        check(versions[value['dictionary_id']], dictionary['version'])
        return plain(svc.service.append_results(task_id, value['dictionary_id'],
            expected_revision=dictionary['revision'], indices=value.get('indices')))
    return await rt.product.call(work)


def service(rt):
    if not hasattr(rt, 'prompts'):
        from stylelora.runtime.prompts import Prompts
        rt.prompts = Prompts(rt)
    return rt.prompts


@router.post('')
async def create(request: Request):
    rt, value = runtime(request), await request.json()
    result = await service(rt).create(value, request.headers['idempotency-key'])
    return resource(result, 202, '/api/prompt-tasks/'+result['task_id'])


@router.get('')
async def listing(request: Request):
    rt = runtime(request)
    def read():
        svc = service(rt)
        rows = [svc.get(path.stem) for path in sorted(svc.service.store.root.glob('*.json'))]
        params = dict(request.query_params)
        status = params.pop('status', None)
        if status:
            rows = [r for r in rows if r['status'] == status]
        return catalog(rt).page(rows, params, 'prompt-tasks/'+str(status), version(rt.secret, rows))
    return await rt.blocking(read)


@router.get('/{task_id}')
@router.get('/{task_id}/coverage')
@router.get('/{task_id}/outcomes')
async def get(task_id: str, request: Request):
    rt = runtime(request)
    def read():
        svc = service(rt)
        value = svc.service.get_task(task_id)
        if request.url.path.endswith('/coverage'):
            return plain(value.coverage)
        if request.url.path.endswith('/outcomes'):
            params = dict(request.query_params)
            status = params.pop('status', None)
            rows = [plain(o) for o in value.outcomes if status is None or o.status == status]
            return catalog(rt).page(rows, params, 'prompt-outcomes/'+task_id+'/'+str(status), value.revision)
        return svc.view(value)
    return resource(await rt.blocking(read))


@router.post('/{task_id}/control')
async def control(task_id: str, request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    return resource(await service(rt).control(task_id, value, token))


@router.post('/{task_id}/seeds')
async def seeds(task_id: str, request: Request):
    rt, token = runtime(request), expected(request)
    def read():
        svc = service(rt)
        current = svc.get(task_id)
        check(token, current['version'])
        return plain(svc.service.freeze_seeds(task_id, expected_revision=current['revision']))
    return await rt.blocking(read)


@router.delete('/{task_id}')
async def delete(task_id: str, request: Request):
    rt, token = runtime(request), expected(request)
    def work():
        svc = service(rt)
        current = svc.get(task_id)
        check(token, current['version'])
        if task_id in svc.active or current['status'] not in ('done', 'failed', 'canceled'):
            raise Failure(409, 'task:in_use')
        svc.service.store.delete(task_id, expected_revision=current['revision'])
    await rt.product.call(work)
    return Response(status_code=204)
