"""Same-origin administrative API; separate from customer Bearer API keys."""
from __future__ import annotations

import hmac
import inspect
from typing import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from .models import get_models
from .platform_store import PlatformError, PlatformStore
from .db_async import db_call

COOKIE_NAME = 'dreamina_admin_session'


def create_admin_router(store: PlatformStore, bridge_store=None, *,
                        account_check: Callable | None = None, models_loader: Callable | None = None,
                        managed_workers=None,task_reconciler=None,monitor_loader=None,gate_releaser=None) -> APIRouter:
    class AdminRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def route(request: Request):
                try:
                    response = await original(request)
                except PlatformError as error:
                    response = JSONResponse({'error': {'code': error.code, 'message': str(error)}}, status_code=error.status)
                response.headers['Cache-Control'] = 'no-store'
                response.headers['X-Content-Type-Options'] = 'nosniff'
                return response
            return route

    router = APIRouter(prefix='/api/admin', tags=['Administration'], route_class=AdminRoute)

    def same_origin(request: Request) -> None:
        origin = request.headers.get('origin')
        if not origin:
            return
        parsed = urlsplit(origin)
        expected = urlsplit(str(request.base_url))
        if (parsed.scheme, parsed.hostname, parsed.port) != (expected.scheme, expected.hostname, expected.port):
            raise PlatformError('Cross-origin administration requests are disabled.', code='origin_denied', status=403)

    def current_admin(request: Request) -> dict:
        same_origin(request)
        principal = store.authenticate_admin(request.cookies.get(COOKIE_NAME, ''))
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('x-csrf-token', '')
            if not hmac.compare_digest(token, principal['csrf_token']):
                raise PlatformError('A valid CSRF token is required.', code='csrf_required', status=403)
        return principal

    async def body(request: Request) -> dict:
        # Account credentials can be long Cookie strings, but this is not an upload API.
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 100_000:
                raise PlatformError('Administrative request is too large.', status=413)
            chunks.append(chunk)
        raw = b''.join(chunks)
        try:
            import json
            value = json.loads(raw)
        except ValueError as exc:
            raise PlatformError('A JSON object is required.') from exc
        if not isinstance(value, dict):
            raise PlatformError('A JSON object is required.')
        return value

    async def model_items() -> list:
        if models_loader:
            result = await invoke_provider(models_loader)
            if isinstance(result, dict):
                result = result.get('items', result.get('data', []))
            return result
        available = {model for account in (await db_call(store.list_accounts)) if account['enabled'] and account['ready'] for model in account['capabilities']}
        result = []
        for model in get_models():
            item = dict(model)
            item.update(kind='video', available=item['id'] in available and item['id'] == 'seedance-2.0-mini',
                        verification_status='chrome-baseline-verified' if item['id'] == 'seedance-2.0-mini' else 'configuration-only')
            result.append(item)
        result.append({'id': 'seedream-5.0', 'name': 'Seedream 5.0', 'kind': 'image', 'available': False,
                       'verification_status': 'not-connected', 'description': 'Image generation adapter needs a verified worker capability.'})
        return result

    async def invoke_provider(callback,*args,**kwargs):
        # A synchronous model/monitor provider can perform several DB reads.
        # Coroutine providers keep their network operations on the event loop.
        result=callback(*args,**kwargs) if inspect.iscoroutinefunction(callback) else await db_call(callback,*args,_store=store,**kwargs)
        return await result if inspect.isawaitable(result) else result

    @router.post('/login')
    async def login(request: Request, response: Response):
        same_origin(request)
        data = await body(request)
        if set(data) != {'username', 'password'}:
            raise PlatformError('username and password are required.')
        result = (await db_call(store.login_admin,data['username'], data['password'], client_id=request.client.host if request.client else ''))
        response.set_cookie(COOKIE_NAME, result.pop('token'), httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', max_age=28800, path='/api/admin')
        return result

    @router.get('/me')
    def me(principal=Depends(current_admin)):
        return principal

    @router.post('/logout')
    def logout(request: Request, response: Response, _=Depends(current_admin)):
        store.logout_admin(request.cookies.get(COOKIE_NAME, ''))
        response.delete_cookie(COOKIE_NAME, path='/api/admin', httponly=True, samesite='strict')
        return {'ok': True}

    @router.get('/dashboard')
    def dashboard(_=Depends(current_admin)):
        return store.dashboard()

    @router.get('/reliability')
    def reliability(_=Depends(current_admin)):
        from .reliability import reliability_snapshot
        return reliability_snapshot(store)

    @router.get('/model-blocks')
    def model_blocks(_=Depends(current_admin)):
        return {'items':store.account_model_blocks()}

    @router.delete('/accounts/{account_id}/model-blocks/{model}')
    def clear_model_block(account_id:str,model:str,_=Depends(current_admin)):
        return store.clear_account_model_block(account_id,model)

    @router.get('/accounts')
    def account_list(_=Depends(current_admin)):
        values=store.list_accounts()
        blocks=store.account_model_blocks()
        for account in values:account['model_blocks']=[b for b in blocks if b['account_id']==account['id']]
        if managed_workers:
            for account in values:account['managed_worker']=managed_workers.status(account['id'])
        return {'items': values}

    @router.post('/accounts', status_code=201)
    async def account_create(request: Request, _=Depends(current_admin)):
        data=await body(request)
        if 'proxy' in data:raise PlatformError('Use a managed proxy_id instead of inline proxy credentials.',code='managed_proxy_required')
        if 'proxy_id' in data or store.account_proxy_required:
            proxy_id=data.pop('proxy_id',None);override=data.pop('confirm_capacity_override',False)
            return await db_call(store.create_account_with_proxy,data,proxy_id,confirm_capacity_override=override)
        return await db_call(store.create_account,data)

    @router.patch('/accounts/{account_id}')
    async def account_update(account_id: str, request: Request, _=Depends(current_admin)):
        data=await body(request)
        if any(k in data for k in ('proxy','proxy_id','browser_timezone','browser_locale','proxy_exit_ip_at_bind')):raise PlatformError('Use the verified account proxy binding endpoint.',code='safe_proxy_binding_required')
        account=await db_call(store.update_account,account_id,data)
        if 'max_concurrent' in data:
            if managed_workers and hasattr(managed_workers,'reconfigure'):
                await invoke_provider(managed_workers.reconfigure,account_id)
            if bridge_store is not None:
                await db_call(bridge_store.prune_workers,account_id,keep_slots=account['effective_max_concurrent'])
            if managed_workers:account['managed_worker']=await invoke_provider(managed_workers.status,account_id)
        return account

    @router.delete('/accounts/{account_id}')
    async def account_delete(account_id: str, _=Depends(current_admin)):
        if bridge_store is not None:
            for account in (await db_call(bridge_store.accounts)):
                if account.get('account_id')==account_id and any((account.get('jobs') or {}).get(state,0) for state in ('queued','claimed','submitted','uncertain')):
                    raise PlatformError('This account has unfinished browser work; reconcile it before deletion.',code='account_busy',status=409)
        previous=(await db_call(store.begin_account_delete,account_id))
        if previous.get('already_deleted'):
            if bridge_store is not None:await db_call(bridge_store.retire_account,account_id)
            return (await db_call(store.delete_account,account_id))
        try:
            if managed_workers:
                await invoke_provider(getattr(managed_workers,'remove_node',managed_workers.stop),account_id)
            result=await db_call(store.delete_account,account_id)
            if bridge_store is not None:result['nodes_cleanup']=await db_call(bridge_store.retire_account,account_id)
            return result
        except Exception:
            # Another identical DELETE may have finished while this one awaited
            # the same worker. Return the durable deletion rather than restore it.
            account=(await db_call(store.get_account,account_id,include_deleted=True))
            if account.get('deleted'):
                return dict(await db_call(store.delete_account,account_id),nodes_cleanup_pending=True)
            (await db_call(store.cancel_account_delete,account_id,previous))
            raise

    @router.post('/accounts/{account_id}/check')
    async def check_account(account_id: str, _=Depends(current_admin)):
        (await db_call(store.get_account,account_id))
        if account_check is None:
            raise PlatformError('An account check provider is not configured.', code='not_configured', status=503)
        try:
            checked = await invoke_provider(account_check,account_id,(await db_call(store.settings_for_account,account_id)))
            if not isinstance(checked, dict):
                raise ValueError('Account check did not return a result.')
            status = checked.get('status', 'ready' if checked.get('authenticated') else 'error')
            account = (await db_call(store.record_account_check,account_id, status=status, balance=checked.get('balance')))
            safe = {key: checked[key] for key in ('status', 'balance', 'authenticated') if key in checked}
            return {'account': account, 'check': safe}
        except PlatformError:
            raise
        except Exception as error:
            if str(getattr(error,'code','')).startswith('proxy_'):
                return JSONResponse({'account':await db_call(store.get_account,account_id),'error':{'code':'proxy_unavailable',
                    'message':'The configured proxy is unavailable. Account credentials and balance were preserved.'}},status_code=502)
            account = (await db_call(store.record_account_check,account_id, status='error', balance=None))
            # Upstream exceptions can carry signed URLs/cookies; do not echo them.
            return JSONResponse({'account': account, 'error': {'code': 'account_check_failed',
                                'message': 'Account check failed. Verify the account credentials and network.'}}, status_code=502)

    @router.get('/keys')
    def key_list(_=Depends(current_admin)):
        return {'items': store.list_keys()}

    @router.post('/keys', status_code=201)
    async def key_create(request: Request, _=Depends(current_admin)):
        return (await db_call(store.create_key,await body(request)))

    @router.patch('/keys/{key_id}')
    async def key_update(key_id: str, request: Request, _=Depends(current_admin)):
        return (await db_call(store.update_key,key_id, await body(request)))

    @router.delete('/keys/{key_id}')
    def key_revoke(key_id: str, _=Depends(current_admin)):
        return store.delete_key(key_id)

    @router.get('/tasks')
    def task_list(kind: str | None = None, status: str | None = None, account_id: str | None = None,
                  limit: int = 50, _=Depends(current_admin)):
        return {'items': store.list_tasks(kind=kind, status=status, account_id=account_id, limit=limit)}

    @router.get('/tasks/{task_id}')
    def task_detail(task_id: str, _=Depends(current_admin)):
        return dict(store.get_task(task_id),events=store.list_events(task_id))

    @router.post('/tasks/{task_id}/reconcile')
    async def reconcile_task(task_id:str,request:Request,_=Depends(current_admin)):
        data=await body(request)
        if set(data)-{'submit_id','note'}:raise PlatformError('Unknown review fields.')
        note=data.get('note','');sid=data.get('submit_id')
        if not isinstance(note,str) or len(note)>500:raise PlatformError('Review note must be at most 500 characters.')
        if sid is not None:
            import uuid
            try:sid=str(uuid.UUID(sid))
            except (ValueError,TypeError,AttributeError):raise PlatformError('A valid upstream submit UUID is required.') from None
        if not task_reconciler:raise PlatformError('Review is not configured.',status=503)
        try:return await task_reconciler(task_id,submit_id=sid,note=note)
        except PlatformError:raise
        except Exception:raise PlatformError('Original-task verification is unavailable. No new generation or credit release occurred.',code='review_unavailable',status=503) from None

    @router.get('/monitoring')
    async def monitoring(_=Depends(current_admin)):
        if not monitor_loader:raise PlatformError('Monitoring is not configured.',status=503)
        return await invoke_provider(monitor_loader)

    @router.get('/workers')
    def workers(_=Depends(current_admin)):
        records = bridge_store.accounts() if bridge_store is not None else list(store._workers().values())
        visible_ids={account['id'] for account in store.list_accounts()}
        items = []
        for account in records:
            if account['account_id'] not in visible_ids:continue
            for worker in account.get('workers', []):
                items.append(dict(worker, account_id=account['account_id'],
                                  blocked_by_uncertain=account.get('blocked_by_uncertain', False),
                                  capabilities=account.get('capabilities', {})))
        return {'items': items}

    @router.delete('/accounts/{account_id}/risk-cooldown')
    async def risk_clear(account_id:str,request:Request,_=Depends(current_admin)):
        data=await body(request)
        if set(data)!={'note'}:raise PlatformError('A review note is required.')
        return await db_call(store.clear_risk_cooldown,account_id,note=data['note'])

    @router.post('/submission-gates/{scope}/release')
    async def gate_release(scope:str,request:Request,_=Depends(current_admin)):
        data=await body(request)
        if set(data)!={'note'} or not isinstance(data['note'],str) or not data['note'].strip() or len(data['note'])>500:raise PlatformError('A review note of 1–500 characters is required.')
        if not gate_releaser:raise PlatformError('Gate review is unavailable.',status=503)
        return await gate_releaser(scope,note=data['note'])

    @router.get('/models')
    async def models(_=Depends(current_admin)):
        return {'items': await model_items()}

    @router.get('/settings')
    async def settings(request: Request, _=Depends(current_admin)):
        return dict((await db_call(store.get_settings)), api_base_url=str(request.base_url).rstrip('/') + '/v1', capabilities=await model_items())

    @router.patch('/settings')
    async def settings_update(request: Request, _=Depends(current_admin)):
        return (await db_call(store.update_settings,await body(request)))

    router.admin_dependency = current_admin
    router.read_body=body
    return router
