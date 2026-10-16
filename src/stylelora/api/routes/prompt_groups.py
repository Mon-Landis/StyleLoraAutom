"""F27 HTTP contract. Writes use group-level strong ETags and durable receipts."""
import json
from fastapi import APIRouter, Request
from fastapi.responses import Response
from stylelora.runtime.prompt_groups import service
from stylelora.runtime.contracts import Failure
from stylelora.prompts.groups import GroupRef, content
from stylelora.prompts.types import plain
from stylelora.prompts.validation import require, strict_loads
from ..dependencies import runtime
from ..uploads import upload
from .session import resource

router = APIRouter(prefix='/api')


def key(request):
    value = request.headers.get('idempotency-key')
    require(isinstance(value, str) and 0 < len(value) <= 256)
    return value


def revision(request, create=False):
    if create:
        if request.headers.get('if-none-match') != '*':
            raise Failure(428, 'revision:required')
        return 0
    token = request.headers.get('if-match')
    if token is None:
        raise Failure(428, 'revision:required')
    if len(token) < 3 or not token.startswith('"') or not token.endswith('"') or not token[1:-1].isdigit():
        raise Failure(412, 'dictionary:revision_conflict')
    return int(token[1:-1])


def result(value, status=200):
    value = dict(value)
    if 'revision' in value:
        value['version'] = str(value['revision'])
    return resource(value, status)


@router.get('/prompt-groups')
async def listing(request: Request):
    rt = runtime(request)
    return await rt.product.call(lambda: service(rt).listing(dict(request.query_params)))


@router.post('/prompt-groups')
async def create(request: Request):
    rt, value = runtime(request), await request.json()
    revision(request, True)
    return result(await rt.product.call(lambda: service(rt).create(value, key(request))), 201)


@router.get('/prompt-groups/{group_id}')
async def get(group_id: str, request: Request):
    rt = runtime(request)
    def work():
        svc = service(rt)
        return svc.summary(svc.store.load(svc.ref(group_id, request.query_params)))
    return result(await rt.product.call(work))


@router.patch('/prompt-groups/{group_id}')
@router.delete('/prompt-groups/{group_id}')
async def edit_group(group_id: str, request: Request):
    rt, expected = runtime(request), revision(request)
    value = await request.json() if request.method == 'PATCH' else {}
    def work():
        svc = service(rt); ref = svc.ref(group_id, request.query_params)
        if request.method == 'DELETE':
            return svc.store.delete(ref, expected, key(request))
        return svc.edit(ref, value, expected, key(request))
    return result(await rt.product.call(work))


@router.get('/prompt-groups/{group_id}/entries')
async def entries(group_id: str, request: Request):
    rt = runtime(request)
    def work():
        svc = service(rt); params = dict(request.query_params)
        require(not set(params)-{'scope', 'project_token', 'disable', 'cursor', 'limit'})
        require(params.get('disable') in (None, 'true', 'false'))
        ref = svc.ref(group_id, params); group = svc.store.load(ref)
        rows = [plain(e) | {'nsfw': group.nsfw} for e in group.entries if params.get('disable') is None or e.disable == (params['disable'] == 'true')]
        page = svc.page(rows, params, [plain(ref), params.get('disable')], group.revision)
        return page | {'revision': group.revision, 'nsfw': group.nsfw}
    return result(await rt.product.call(work))


@router.post('/prompt-groups/{group_id}/entries')
@router.patch('/prompt-groups/{group_id}/entries/{entry_id}')
@router.delete('/prompt-groups/{group_id}/entries/{entry_id}')
async def edit_entry(group_id: str, request: Request, entry_id: str = None):
    rt, expected = runtime(request), revision(request)
    value = {} if request.method == 'DELETE' else await request.json()
    action = {'POST': 'add', 'PATCH': 'patch', 'DELETE': 'delete'}[request.method]
    def work():
        svc = service(rt)
        return svc.edit(svc.ref(group_id, request.query_params), value, expected, key(request), entry_id, action)
    return result(await rt.product.call(work), 201 if action == 'add' else 200)


@router.get('/prompt-groups/{group_id}/entries/{entry_id}')
@router.get('/prompt-groups/{group_id}/entries/{entry_id}/export')
async def entry(group_id: str, entry_id: str, request: Request):
    rt = runtime(request)
    def work():
        svc = service(rt); group = svc.store.load(svc.ref(group_id, request.query_params))
        item = svc.entry(group, entry_id)
        if request.url.path.endswith('/export'):
            require(request.query_params.get('format', 'json') == 'json')
            return content(item)
        return plain(item) | {'revision': group.revision, 'nsfw': group.nsfw}
    return result(await rt.product.call(work))


@router.put('/prompt-groups/{group_id}/entries/{entry_id}/preview')
async def put_image(group_id: str, entry_id: str, request: Request):
    rt, expected = runtime(request), revision(request)
    form = await upload(request, {'image'}, {'image'}, limit=11*1024**2, binary=('image',))
    data = form['image']
    def work():
        svc = service(rt)
        return svc.edit(svc.ref(group_id, request.query_params), data, expected, key(request), entry_id, 'image')
    value = await rt.product.call(work)
    return result(value['preview'] | {'revision': value['revision'], 'nsfw': value['nsfw']})


@router.get('/prompt-groups/{group_id}/entries/{entry_id}/preview')
async def image(group_id: str, entry_id: str, request: Request):
    rt = runtime(request)
    def work():
        svc = service(rt); ref = svc.ref(group_id, request.query_params)
        group = svc.store.load(ref); item = svc.entry(group, entry_id)
        data, sha = svc.store.image(ref, item, request.query_params.get('size', 'full'), request.query_params.get('asset_revision'))
        return data, sha, group.nsfw
    data, sha, nsfw = await rt.product.call(work)
    headers = {'ETag': '"'+sha+'"', 'Cache-Control': 'private, no-cache', 'X-NSFW': str(nsfw).lower()}
    return Response(status_code=304, headers=headers) if request.headers.get('if-none-match') == headers['ETag'] else Response(data, media_type='image/webp', headers=headers)


@router.post('/prompt-imports/preview')
async def preview(request: Request):
    rt = runtime(request)
    if 'multipart/form-data' in request.headers.get('content-type', ''):
        form = await upload(request, {'options', 'file'}, {'file'}, limit=101*1024**2)
        options = strict_loads(form.get('options', '{}'))
        data = form['file']
    else:
        value = await request.json()
        require(isinstance(value, dict) and not set(value)-{'options', 'grant_id', 'data'})
        options = value.get('options', {})
        if options.get('format') == 'folder':
            data = rt.grant_path(value.get('grant_id'), 'import_root')
        else:
            data = json.dumps(value.get('data'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    return await rt.product.call(lambda: service(rt).exchange.preview_import(data, options, key(request)))


@router.post('/prompt-imports/{preview_id}/confirm')
async def confirm(preview_id: str, request: Request):
    rt, value = runtime(request), await request.json()
    require(isinstance(value, dict) and not set(value)-{'digest', 'ref'} and 'digest' in value)
    def work():
        svc = service(rt)
        frozen = svc.exchange.previews.get(preview_id)
        require(frozen is not None, 'preview:expired')
        expected = revision(request, frozen[1]['base_revision'] == 0)
        ref = GroupRef.parse(value['ref']) if 'ref' in value else None
        return svc.exchange.commit_import(preview_id, value['digest'], expected, key(request), ref)
    return result(await rt.product.call(work))


@router.get('/prompt-groups/{group_id}/export')
async def export(group_id: str, request: Request):
    rt = runtime(request); format = request.query_params.get('format', 'json')
    def work():
        svc = service(rt); ref = svc.ref(group_id, request.query_params)
        return svc.exchange.export_group(ref, format)
    artifact = await rt.product.call(work)
    if format == 'folder':
        return artifact
    return Response(artifact, media_type='application/json' if format == 'json' else 'application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{group_id}.{format}"'})


@router.get('/prompt-artifacts/{handle}')
async def artifact(handle: str, request: Request):
    rt = runtime(request); svc = service(rt)
    entry = svc.exchange.artifacts.get(handle)
    require(entry is not None and entry[0] > rt.clock(), 'preview:expired')
    return Response(entry[1], media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="prompt-group.zip"'})


@router.get('/settings')
async def settings(request: Request):
    rt = runtime(request)
    value = await rt.product.call(rt.product.get)
    return result(value | {'revealed_this_run': service(rt).revealed_this_run})


@router.patch('/settings')
async def patch_settings(request: Request):
    rt, value, expected = runtime(request), await request.json(), revision(request)
    require(isinstance(value, dict) and not set(value)-{'nsfw_enabled', 'nsfw_display_mode', 'image_display'})
    request_key = key(request)
    return result(await rt.product.call(lambda: rt.product.update_safety(value, expected, request_key)))


@router.post('/safety/reveal')
async def reveal(request: Request):
    svc = service(runtime(request))
    raw = await request.body()
    require(not raw or strict_loads(raw) == {})
    key(request); svc.revealed_this_run = True
    return {'revealed_this_run': True}
