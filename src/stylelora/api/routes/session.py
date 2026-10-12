import asyncio
import json
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from stylelora.runtime.contracts import Failure, check
from ..dependencies import runtime, body, expected

router = APIRouter(prefix='/api')


def resource(value, status=200, location=None):
    headers = {}
    if isinstance(value, dict) and value.get('version'):
        headers['ETag'] = '"' + value['version'] + '"'
    if location:
        headers['Location'] = location
    if status == 202:
        headers['Retry-After'] = '1'
    return JSONResponse(value, status_code=status, headers=headers)


def accepted(rt, kind, work, project_id=None):
    operation = rt.supervisor.submit(kind, work, project_id=project_id)
    return resource(operation, 202, '/api/operations/' + operation['operation_id'])


@router.get('/session')
async def session(request: Request):
    rt = runtime(request)
    return dict(instance_id=rt.instance_id, epoch=rt.events.epoch, api_version=1,
                project=rt.project_view(), capabilities={'picker': rt.picker is not None}, draining=rt.draining)


@router.post('/session/shutdown')
async def shutdown(request: Request):
    rt = runtime(request)
    value = await body(request, {'grace_seconds'})
    grace = value.get('grace_seconds', 30)
    if type(grace) is not int or not 0 <= grace <= 30:
        raise Failure(422, 'input:invalid')
    if rt.shutdown_callback is None:
        raise Failure(503, 'dependency:unavailable')
    async def work():
        rt.draining = True
        await rt.close_project()
        if hasattr(rt, 'prompts'):
            await rt.prompts.pause()
        await rt.supervisor.join()
        rt.shutdown_callback()
    return accepted(rt, 'shutdown', work)


@router.post('/session/bootstrap')
async def bootstrap(request: Request):
    value = await body(request, {'code'}, {'code'})
    return JSONResponse(runtime(request).redeem(value['code'], request.headers.get('origin')), headers={'Cache-Control': 'no-store'})


@router.get('/config')
async def config(request: Request):
    rt = runtime(request)
    return resource(await rt.product.call(rt.product.get))


@router.patch('/config')
async def patch_config(request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    result = await rt.product.call(lambda: rt.product.update(value, token))
    rt.events.publish('config.changed', payload={'resource_kind': 'config', 'resource_id': None, 'revision': result['revision']})
    return resource(result)


@router.post('/filesystem/grants')
async def pick(request: Request):
    rt = runtime(request)
    value = await body(request, {'purpose', 'project_id'}, {'purpose'})
    if rt.picker is None:
        raise Failure(501, 'feature:unsupported')
    async def work():
        path = await rt.blocking(lambda: rt.picker(value['purpose']))
        return rt.grant(path, value['purpose'], value.get('project_id')) if path else None
    return accepted(rt, 'picker', work)


@router.delete('/filesystem/grants/{grant_id}')
async def revoke(grant_id: str, request: Request):
    check(expected(request), '1')
    runtime(request).grants.pop(grant_id, None)
    return Response(status_code=204)


@router.get('/operations/{operation_id}')
async def operation(operation_id: str, request: Request):
    return resource(runtime(request).supervisor.get(operation_id))


@router.post('/operations/{operation_id}/cancel')
async def cancel_operation(operation_id: str, request: Request):
    rt = runtime(request)
    check(expected(request), rt.supervisor.get(operation_id)['version'])
    # Arbitrary file threads cannot safely be canceled by dropping their owner.
    raise Failure(409, 'operation:not_cancelable')


@router.get('/projects/current')
async def current(request: Request):
    return resource(runtime(request).project_view())


@router.get('/projects')
async def recent(request: Request):
    rt = runtime(request)
    return dict(items=[{'known_id': k, 'display_name': p.name} for k, p in rt.known.items()],
                next_cursor=None, has_more=False, limit=50, upper_id=None)


@router.post('/projects')
async def create(request: Request):
    rt = runtime(request)
    value = await body(request, {'grant_id', 'name', 'trigger_word'}, {'grant_id', 'name'})
    path = rt.grant_path(value['grant_id'], 'project_create')
    return accepted(rt, 'project_create', lambda: rt.open(path, create={'name': value['name'], 'trigger_word': value.get('trigger_word', '')}))


@router.post('/projects/open')
async def open_project(request: Request):
    rt = runtime(request)
    value = await body(request, {'grant_id', 'known_id'})
    if len(value) != 1:
        raise Failure(422, 'input:invalid')
    path = rt.grant_path(value['grant_id'], 'project_open') if 'grant_id' in value else rt.known.get(value['known_id'])
    if path is None:
        raise Failure(404, 'resource:not_found')
    return accepted(rt, 'project_open', lambda: rt.open(path))


@router.get('/projects/{project_id}')
async def project(project_id: str, request: Request):
    rt = runtime(request)
    return resource(rt.project_view(rt.require_project(project_id)))


@router.post('/projects/{project_id}/close')
async def close_project(project_id: str, request: Request):
    rt = runtime(request)
    check(expected(request), rt.project_view(rt.require_project(project_id))['version'])
    return accepted(rt, 'project_close', rt.close_project)


@router.patch('/projects/{project_id}')
async def patch_project(project_id: str, request: Request):
    rt, value, token = runtime(request), await request.json(), expected(request)
    return resource(await rt.update_project(project_id, value, token))


@router.get('/projects/{project_id}/recovery')
async def recovery(project_id: str, request: Request):
    from stylelora.runtime.contracts import plain
    rt = runtime(request)
    ctx = rt.require_project(project_id)
    report = plain(ctx.handle.writer.report)
    return dict(items=[{'kind':k, 'resource_id':v} for k, values in report.items() for v in values],
                next_cursor=None, has_more=False, limit=50, upper_id=None)


@router.get('/events')
async def events(request: Request):
    rt = runtime(request)
    if set(request.query_params)-{'topics', 'project_id', 'resource_id'}:
        raise Failure(422, 'input:invalid')
    pid = request.query_params.get('project_id')
    if pid:
        rt.require_project(pid)
    key, queue, replay = rt.events.connect(request.headers.get('last-event-id'),
        topics=request.query_params.getlist('topics'), project_id=pid, resource_id=request.query_params.get('resource_id'))
    def frame(kind, event):
        if 'sequence' not in event:
            event = dict(schema_version=1, epoch=rt.events.epoch, sequence=rt.events.sequence,
                         project_id=pid, resource=None, version=None, payload=event)
        return f'id: {event["epoch"]}:{event["sequence"]}\nevent: {kind}\ndata: {json.dumps(event)}\n\n'
    async def stream():
        try:
            yield frame('stream.ready', {'instance_id': rt.instance_id, 'current_sequence': rt.events.sequence, 'replayed': bool(replay)})
            for kind, event in replay:
                yield frame(kind, event)
            while True:
                try:
                    kind, event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ': heartbeat\n\n'
                    continue
                yield frame(kind, event)
                if kind == 'stream.resync_required':
                    break
        finally:
            rt.events.disconnect(key)
    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
