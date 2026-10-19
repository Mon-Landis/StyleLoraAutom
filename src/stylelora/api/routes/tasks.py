from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from ..dependencies import runtime
from stylelora.runtime.contracts import plain, fields, Failure
from stylelora.runtime.tasks import TaskQuery

router = APIRouter(prefix='/api/tasks')


@router.post('')
async def create_task(request: Request):
    from stylelora.runtime.prompt_completion import completion_service
    from stylelora.prompts.groups import GroupRef
    from stylelora.prompts.validation import require
    rt = runtime(request)
    value = await request.json()
    require(isinstance(value, dict))
    key = request.headers.get('idempotency-key')
    require(isinstance(key, str) and bool(key))
    if value.get('type') == 'prompt_generation':
        from stylelora.runtime.prompt_generation import generation_service
        value = fields(value, {'type', 'ref', 'brief', 'count'}, {'type', 'ref', 'brief', 'count'})
        result = await generation_service(rt).enqueue(GroupRef.parse(value['ref']), value['brief'], value['count'], key)
    else:
        value = fields(value, {'type', 'ref', 'entry_ids', 'base_revision', 'trigger'}, {'type', 'ref', 'base_revision'})
        require(value['type'] == 'prompt_completion')
        require(value.get('trigger', 'manual') != 'auto_leave' or 'entry_ids' not in value)
        result = await completion_service(rt).enqueue(GroupRef.parse(value['ref']), value.get('entry_ids'), value['base_revision'], key,
                                                     trigger=value.get('trigger', 'manual'))
    return JSONResponse(result, status_code=202)


@router.get('')
async def list_tasks(request: Request):
    rt = runtime(request)
    values = fields(dict(request.query_params), {'kind', 'status', 'project_id', 'limit', 'cursor'})
    try:
        values['limit'] = int(values.get('limit', 50))
    except (ValueError, TypeError):
        raise Failure(422, 'input:invalid') from None
    await rt.refresh_tasks()
    return plain(await rt.tasks.list(TaskQuery(**values)))


@router.get('/{task_id}')
async def detail(task_id: str, request: Request):
    rt = runtime(request)
    await rt.refresh_tasks()
    result = plain(await rt.tasks.get(task_id))
    adapter, linked, _ = rt.tasks._record(task_id)
    if linked.kind in ('prompt_completion', 'prompt_generation'):
        result['type'] = linked.kind
        result['reports'] = adapter.records[linked.id]['reports']
    return result


@router.post('/{task_id}/cancel')
async def cancel(task_id: str, request: Request):
    rt = runtime(request)
    value = fields(await request.json(), {'request_key'}, {'request_key'})
    result = await rt.tasks.cancel(task_id, value['request_key'])
    return JSONResponse(plain(result), status_code=200 if result.no_op or result.status != 'canceling' else 202)
