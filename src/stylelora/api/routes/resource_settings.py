from fastapi import APIRouter, Request
from ..dependencies import runtime

router = APIRouter(prefix='/api/settings/resources')


@router.get('')
async def get(request: Request):
    rt = runtime(request)
    return await rt.product.call(rt.product.get_resources)


@router.put('')
async def update(request: Request):
    from stylelora.resources.network import NetworkPolicy
    from stylelora.resources.compute import ComputePolicy
    rt, value = runtime(request), await request.json()
    async with rt.resource_settings_lock:
        result = await rt.product.call(lambda: rt.product.update_resources(value))
        rt.network.configure(NetworkPolicy(**result['network']))
        rt.compute.configure(ComputePolicy(**result['compute']))
        rt.events.publish('settings.updated', resource={'kind':'settings', 'id':'resources'},
                          version=result['revision'], payload={'revision':result['revision']})
        return result
