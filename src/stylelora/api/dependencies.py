from fastapi import Request
from stylelora.runtime.contracts import Failure, fields


def runtime(request: Request):
    return request.app.state.runtime


def require_project(request: Request):
    return runtime(request).require_project(request.path_params['project_id'])


def expected(request):
    value = request.headers.get('if-match')
    if value is None:
        raise Failure(428, 'concurrency:precondition_required')
    if len(value) < 3 or value[0] != '"' or value[-1] != '"' or '"' in value[1:-1]:
        raise Failure(422, 'input:invalid')
    return value[1:-1]


async def body(request, allowed, required=()):
    return fields(await request.json(), allowed, required)
