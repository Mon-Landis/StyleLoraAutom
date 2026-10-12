from fastapi import APIRouter, Request
from fastapi.responses import Response
from stylelora.runtime.contracts import Failure
from ..dependencies import runtime, body, expected
from .session import resource

router = APIRouter(prefix='/api/projects/{project_id}/images')


def query(request, extras=()):
    params = request.query_params
    if set(params) - {'source_id', 'review_status', 'rating', 'file_missing', 'layer', 'annotation_coverage'} - set(extras):
        raise Failure(422, 'input:invalid')
    result = {}
    try:
        for key, target in [('source_id', 'source_ids'), ('review_status', 'review_statuses'), ('rating', 'rating')]:
            if key in params:
                result[target] = [int(v) for v in params.getlist(key)] if key == 'source_id' else params.getlist(key)
        for key in ('layer', 'annotation_coverage'):
            if key in params:
                result[key] = params[key]
        if 'file_missing' in params:
            if params['file_missing'] not in ('true', 'false'):
                raise ValueError
            result['file_missing'] = params['file_missing'] == 'true'
    except ValueError:
        raise Failure(422, 'input:invalid') from None
    return result


@router.get('')
async def image_list(project_id: str, request: Request):
    rt = runtime(request)
    params = request.query_params
    filters = query(request, ('limit', 'sort', 'cursor', 'around_id'))
    try:
        limit = int(params.get('limit', '50'))
        around = int(params['around_id']) if 'around_id' in params else None
    except ValueError:
        raise Failure(422, 'input:invalid') from None
    async with rt.lease(project_id) as ctx:
        return await rt.blocking(lambda: rt.images.list(ctx, filters, limit=limit, sort=params.get('sort', 'id'), cursor=params.get('cursor'), around_id=around))


@router.get('/stats')
async def stats(project_id: str, request: Request):
    rt = runtime(request)
    filters = query(request, ('group_by',))
    async with rt.lease(project_id) as ctx:
        return await rt.blocking(lambda: rt.images.stats(ctx, filters, request.query_params.getlist('group_by') or ['review_status', 'rating']))


@router.post('/review/preview')
async def preview(project_id: str, request: Request):
    rt = runtime(request)
    value = await body(request, {'query'}, {'query'})
    async with rt.lease(project_id) as ctx:
        return resource(await rt.blocking(lambda: rt.images.preview(ctx, value['query'])))


@router.post('/review')
async def review(project_id: str, request: Request):
    rt = runtime(request)
    value = await request.json()
    async with rt.lease(project_id) as ctx:
        return await rt.images.review(ctx, value)


@router.get('/{image_id}')
async def detail(project_id: str, image_id: int, request: Request):
    rt = runtime(request)
    if set(request.query_params) - {'include'} or request.query_params.get('include', 'metadata') != 'metadata':
        raise Failure(422, 'input:invalid')
    async with rt.lease(project_id) as ctx:
        return resource(await rt.blocking(lambda: rt.images.detail(ctx, image_id, metadata='include' in request.query_params)))


@router.patch('/{image_id}')
async def patch(project_id: str, image_id: int, request: Request):
    rt = runtime(request)
    value = await body(request, {'review_status'}, {'review_status'})
    token = expected(request)
    async with rt.lease(project_id) as ctx:
        result = await rt.images.review(ctx, dict(ids=[image_id], target=value['review_status'], expected_versions={str(image_id): token}))
        return resource(result['items'][0]['image'])


async def media(project_id, image_id, request, thumbnail=False):
    rt = runtime(request)
    if set(request.query_params)-({'size'} if thumbnail else set()):
        raise Failure(422, 'input:invalid')
    try:
        size = int(request.query_params.get('size', '256')) if thumbnail else None
    except ValueError:
        raise Failure(422, 'input:invalid') from None
    async with rt.lease(project_id) as ctx:
        data, mime, etag = await rt.blocking(lambda: rt.images.media(ctx, image_id, size))
    headers = {'ETag': etag, 'Cache-Control': 'private, max-age=0, must-revalidate', 'Vary': 'Authorization',
               'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline', 'Accept-Ranges': 'none'}
    candidates = [v.strip().removeprefix('W/') for v in request.headers.get('if-none-match', '').split(',')]
    if '*' in candidates or etag in candidates:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=mime, headers=headers)


@router.get('/{image_id}/content')
async def content(project_id: str, image_id: int, request: Request):
    return await media(project_id, image_id, request)


@router.get('/{image_id}/thumbnail')
async def thumbnail(project_id: str, image_id: int, request: Request):
    return await media(project_id, image_id, request, True)
