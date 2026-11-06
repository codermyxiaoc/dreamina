"""Private proxy routes, sharing administrator authorization and CSRF."""
from fastapi import Depends,Request
from .admin_api import create_admin_router
from .db_async import db_call
from .platform_store import PlatformError
from .egress import EgressError

def create_proxy_router(store,service,admin_router):
    router=create_admin_router(store);router.routes.clear()
    auth=admin_router.admin_dependency;body=admin_router.read_body

    @router.get('/proxies')
    async def proxies(_=Depends(auth)):return {'items':await db_call(store.list_proxies),'proxy_required':store.account_proxy_required}
    @router.post('/proxies',status_code=201)
    async def create(request:Request,_=Depends(auth)):
        value=await db_call(store.create_proxy,await body(request));service.probe_later(value['id']);return value
    @router.post('/proxies/batch')
    async def batch(request:Request,_=Depends(auth)):
        data=await body(request)
        if set(data)!={'lines'}:raise PlatformError('Only lines is accepted.')
        result=await db_call(store.batch_create_proxies,data['lines'])
        for item in result['items']:service.probe_later(item['id'])
        return result
    @router.put('/proxies/{proxy_id}')
    async def update(proxy_id:str,request:Request,_=Depends(auth)):
        data=await body(request);value=await db_call(store.update_proxy,proxy_id,data)
        if 'url' in data:service.probe_later(proxy_id)
        return value
    @router.delete('/proxies/{proxy_id}')
    async def delete(proxy_id:str,_=Depends(auth)):return await db_call(store.delete_proxy,proxy_id)
    @router.post('/proxies/{proxy_id}/test')
    async def test(proxy_id:str,_=Depends(auth)):
        try:return await service.health.test(proxy_id)
        except PlatformError:raise
        except Exception:raise PlatformError('Proxy probe is unavailable; no direct connection was attempted.',code='proxy_probe_failed',status=503) from None
    @router.get('/proxies/{proxy_id}/accounts')
    async def bound(proxy_id:str,_=Depends(auth)):return {'items':await db_call(store.proxy_accounts,proxy_id)}
    @router.post('/accounts/{account_id}/proxy')
    async def bind(account_id:str,request:Request,_=Depends(auth)):
        data=await body(request)
        if set(data)-{'proxy_id','confirm_country_change','confirm_capacity_override'} or 'proxy_id' not in data:raise PlatformError('Invalid proxy binding fields.')
        try:return await service.bind(account_id,**data)
        except PlatformError:raise
        except EgressError as error:raise PlatformError(str(error),code=error.code,status=error.status) from None
        except Exception:raise PlatformError('Proxy change is protected but incomplete. Retry the same binding after checking node status.',code='proxy_change_pending',status=409) from None
    return router
