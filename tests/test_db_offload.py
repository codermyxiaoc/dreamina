"""Slow synchronous database work must not stop the asyncio event loop."""
import asyncio
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from dreamina_api.platform_runtime import PlatformRuntime
from dreamina_api.admin_api import create_admin_router


async def responsive(operation,entered,release):
    task=asyncio.create_task(operation)
    # If a call blocks the event loop, its blocking fake will timeout rather
    # than letting this coroutine observe entry and release it promptly.
    for _ in range(200):
        if entered.is_set():break
        await asyncio.sleep(.005)
    assert entered.is_set(),'Synchronous callback was not reached'
    assert not task.done(),'Blocking work completed without yielding to the loop'
    release.set()
    return await task


def blocking(entered,release,seen,return_value):
    def callback(*args,**kwargs):
        seen.append(threading.get_ident());entered.set()
        assert release.wait(2),'The event loop was blocked by synchronous storage'
        return return_value
    return callback


def test_runtime_task_lookup_uses_thread_without_blocking_loop():
    async def run():
        entered=threading.Event();release=threading.Event();seen=[];main=threading.get_ident()
        value={'id':'video_fixture','status':'completed','bridge_job_id':'already-finished'}
        store=SimpleNamespace(get_task=blocking(entered,release,seen,value))
        result=await responsive(PlatformRuntime(store,None).sync_task({'id':'video_fixture'}),entered,release)
        assert result==value and seen==[seen[0]] and seen[0]!=main
    asyncio.run(run())


def test_runtime_bridge_claim_check_uses_thread():
    async def run():
        entered=threading.Event();release=threading.Event();seen=[];main=threading.get_ident()
        job={'id':'job-fixture'}
        bridge=SimpleNamespace(verify_claim=blocking(entered,release,seen,job))
        runtime=PlatformRuntime(SimpleNamespace(),bridge)
        async def after(*_):return 'verified'
        runtime._credit_preflight_locked=after
        result=await responsive(runtime.credit_preflight(job,'worker','token',{}),entered,release)
        assert result=='verified' and seen[0]!=main
    asyncio.run(run())


def test_query_diagnostic_store_write_is_offloaded_but_poll_state_stays_on_loop():
    async def run():
        entered=threading.Event();release=threading.Event();seen=[];main=threading.get_ident()
        store=SimpleNamespace(append_event=blocking(entered,release,seen,None))
        runtime=PlatformRuntime(store,None,clock=lambda:100)
        state=runtime._poll_state('task','original','bridge')
        await responsive(runtime._defer_query('task',state,'network_unavailable'),entered,release)
        assert state['attempt']==1 and state['next_at']==103 and seen[0]!=main
    asyncio.run(run())


@pytest.mark.parametrize('endpoint,callback_name',[
    ('/api/admin/login','login_admin'),('/api/admin/accounts','create_account_with_proxy'),
    ('/api/admin/keys','create_key'),('/api/admin/settings','update_settings'),
])
def test_admin_async_mutation_offloads_synchronous_storage(endpoint,callback_name):
    async def run():
        entered=threading.Event();release=threading.Event();seen=[];main=threading.get_ident()
        value={'token':'synthetic','csrf_token':'csrf','username':'admin'} if callback_name=='login_admin' else {'ok':True}
        store=SimpleNamespace(account_proxy_required=True,**{callback_name:blocking(entered,release,seen,value)})
        router=create_admin_router(store);app=FastAPI();app.include_router(router)
        app.dependency_overrides[router.admin_dependency]=lambda:{'username':'admin'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            method='PATCH' if callback_name=='update_settings' else 'POST'
            payload={'username':'admin','password':'not-a-real-password'} if callback_name=='login_admin' else {}
            response=await responsive(client.request(method,endpoint,json=payload),entered,release)
        assert response.status_code in (200,201) and seen[0]!=main
    asyncio.run(run())


@pytest.mark.parametrize('endpoint,loader_name',[('/api/admin/models','models_loader'),('/api/admin/monitoring','monitor_loader')])
def test_admin_sync_provider_runs_in_thread(endpoint,loader_name):
    async def run():
        entered=threading.Event();release=threading.Event();seen=[];main=threading.get_ident()
        loader=blocking(entered,release,seen,[])
        router=create_admin_router(SimpleNamespace(),**{loader_name:loader});app=FastAPI();app.include_router(router)
        app.dependency_overrides[router.admin_dependency]=lambda:{'username':'admin'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await responsive(client.get(endpoint),entered,release)
        assert response.status_code==200 and seen[0]!=main
    asyncio.run(run())


def test_admin_async_provider_is_awaited_on_event_loop():
    async def run():
        seen=[];main=threading.get_ident()
        async def loader():
            await asyncio.sleep(0);seen.append(threading.get_ident());return []
        router=create_admin_router(SimpleNamespace(),models_loader=loader);app=FastAPI();app.include_router(router)
        app.dependency_overrides[router.admin_dependency]=lambda:{'username':'admin'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:response=await client.get('/api/admin/models')
        assert response.status_code==200 and seen==[main]
    asyncio.run(run())


def test_closed_submission_window_releases_only_pacing_via_thread():
    async def run():
        entered=threading.Event();release=threading.Event();seen=[]
        store=SimpleNamespace(finish_submission_turn=blocking(entered,release,seen,{'finished':True}))
        runtime=PlatformRuntime(store,None)
        await responsive(runtime.reported_submission({'id':'original','status':'uncertain','confirmed_at':None,'submission_window_closed_at':1000}),entered,release)
        assert seen and seen[0]!=threading.get_ident()
    asyncio.run(run())
