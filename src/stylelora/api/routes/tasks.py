from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from ..dependencies import runtime
from stylelora.runtime.contracts import plain, fields, Failure
from stylelora.runtime.tasks import TaskQuery

router = APIRouter(prefix='/api/tasks')


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
    return plain(await rt.tasks.get(task_id))


@router.post('/{task_id}/cancel')
async def cancel(task_id: str, request: Request):
    rt = runtime(request)
    value = fields(await request.json(), {'request_key'}, {'request_key'})
    result = await rt.tasks.cancel(task_id, value['request_key'])
    return JSONResponse(plain(result), status_code=200 if result.no_op or result.status != 'canceling' else 202)
