from fastapi import APIRouter, Request
from stylelora.runtime.local import verify, history
from ..dependencies import runtime, expected
from .session import accepted

router = APIRouter(prefix='/api/projects/{project_id}')


@router.post('/ratings/reclassify')
async def ratings(project_id: str, request: Request):
    from stylelora.runtime.contracts import fields, Failure
    from stylelora.vision.executor import reclassify
    from .resources import service
    rt = runtime(request)
    ctx = rt.require_project(project_id)
    value = fields(await request.json(), {'scope'}, {'scope'})
    scope = service(rt).scope(value['scope'])
    executor = rt.executors.get('llm_vision')
    if executor is None or getattr(executor, 'rating_snapshot', None) is None:
        raise Failure(503, 'rating:unavailable')
    class Cancel:
        def raise_if_cancelled(self):
            pass
    async def work():
        return {'image_ids': await reclassify(ctx.handle, scope, executor=executor, cancel=Cancel())}
    return accepted(rt, 'ratings', work, project_id)


@router.post('/recovery/reconcile')
async def reconcile_project(project_id: str, request: Request):
    from stylelora.runtime.local import reconcile
    from .storage import service
    rt, value, token = runtime(request), await request.json(), expected(request)
    ctx = rt.require_project(project_id)
    service(rt).busy(ctx)
    async def work():
        return await rt.blocking(lambda: reconcile(rt, ctx, value, token))
    return accepted(rt, 'recovery', work, project_id)


@router.post('/sources/{source_id}/import-root')
async def reselect_root(project_id: str, source_id: int, request: Request):
    from stylelora.runtime.local import import_root
    from .resources import service
    rt, value, token = runtime(request), await request.json(), expected(request)
    ctx = rt.require_project(project_id)
    async def work():
        return await rt.blocking(lambda: import_root(rt, ctx, source_id, value, token, service(rt)))
    return accepted(rt, 'import_root', work, project_id)


@router.post('/images/{image_id}/verify-file')
async def verify_file(project_id: str, image_id: int, request: Request):
    rt, ctx, token = runtime(request), None, expected(request)
    ctx = rt.require_project(project_id)
    from .storage import service as storage
    storage(rt).busy(ctx)
    async def work():
        return await rt.blocking(lambda: verify(rt, ctx, image_id, token))
    return accepted(rt, 'image_verify', work, project_id)


@router.get('/annotations/{image_id}/history')
async def annotation_history(project_id: str, image_id: int, request: Request):
    from .product import service
    from stylelora.runtime.contracts import version
    rt, ctx = runtime(request), runtime(request).require_project(project_id)
    params = dict(request.query_params)
    rows = await rt.blocking(lambda: history(ctx, image_id, params))
    filters = (request.query_params.get('source'), request.query_params.get('type'))
    return service(rt).page(rows, params, f'history/{project_id}/{image_id}/{filters}', version(rt.secret, rows))


@router.post('/import-schemes/{scheme_id}/set-default')
async def default_scheme(project_id: str, scheme_id: int, request: Request):
    from stylelora.runtime.local import set_default
    from .resources import service
    from .session import resource
    rt, value = runtime(request), await request.json()
    async with rt.lease(project_id) as ctx:
        result = await rt.blocking(lambda: set_default(rt, ctx, scheme_id, value, service(rt)))
    return resource(result)


@router.get('/imports/{source_id}/summary')
async def summary(project_id: str, source_id: int, request: Request):
    from stylelora.runtime.local import import_summary
    from stylelora.runtime.contracts import version
    from .product import service
    rt = runtime(request)
    async with rt.lease(project_id) as ctx:
        counts, rows = await rt.blocking(lambda: import_summary(ctx, source_id))
    return service(rt).page(rows, dict(request.query_params), f'import/{project_id}/{source_id}',
                            version(rt.secret, rows)) | {'counts': counts}


@router.post('/annotations/manual/bulk')
async def bulk(project_id: str, request: Request):
    from stylelora.runtime.local import bulk_manual
    from .resources import service
    rt, value = runtime(request), await request.json()
    async with rt.lease(project_id) as ctx:
        return await rt.blocking(lambda: bulk_manual(rt, ctx, value, service(rt)))


@router.post('/duplicates/scans')
async def duplicates(project_id: str, request: Request):
    from stylelora.runtime.contracts import fields, Failure, plain
    from stylelora.vision.phash import scan_duplicates
    from .resources import service
    from .session import resource
    rt = runtime(request)
    ctx = rt.require_project(project_id)
    value = fields(await request.json(), {'scope', 'threshold'}, {'scope', 'threshold'})
    if type(value['threshold']) is not int or not 0 <= value['threshold'] <= 64:
        raise Failure(422, 'input:invalid')
    scope = service(rt).scope(value['scope'])
    class Cancel:
        def raise_if_cancelled(self):
            pass
    async def work():
        pairs, skipped = [], {}
        try:
            async for block in scan_duplicates(ctx.handle, scope, threshold=value['threshold'],
                                              cancel=Cancel(), report=skipped.update):
                pairs.extend(plain(block))
                if len(pairs) > 100000:
                    raise Failure(413, 'batch:too_large')
        except ValueError:
            raise Failure(413, 'batch:too_large') from None
        return {'pairs': pairs, 'skipped': skipped}
    operation = rt.supervisor.submit('duplicates', work, project_id=project_id)
    return resource(dict(operation, scan_id=operation['operation_id']), 202)


@router.get('/duplicates/scans/{scan_id}')
@router.delete('/duplicates/scans/{scan_id}')
async def duplicate_result(project_id: str, scan_id: str, request: Request):
    from stylelora.runtime.contracts import Failure, check
    from .product import service
    from fastapi.responses import Response
    rt = runtime(request)
    rt.require_project(project_id)
    record = rt.supervisor.get(scan_id)
    if record['kind'] != 'duplicates' or record['project_id'] != project_id:
        raise Failure(410, 'session:expired')
    if request.method == 'DELETE':
        check(expected(request), record['version'])
        task = rt.supervisor.tasks[scan_id]
        if not task.done():
            task.cancel()
            import asyncio
            await asyncio.gather(task, return_exceptions=True)
        rt.supervisor.records.pop(scan_id)
        rt.supervisor.tasks.pop(scan_id)
        return Response(status_code=204)
    result = record['result'] or {'pairs': [], 'skipped': {}}
    return service(rt).page(result['pairs'], dict(request.query_params), 'duplicates/'+scan_id,
        record['version']) | {'status': record['status'], 'version': record['version'], 'skipped': result['skipped']}
