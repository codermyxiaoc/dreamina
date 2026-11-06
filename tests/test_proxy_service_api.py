import asyncio,json
from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dreamina_api.admin_api import create_admin_router
from dreamina_api.proxy_api import create_proxy_router
from dreamina_api.proxy_service import ProxyBindingService
from dreamina_api.proxy_health import ProxyHealthManager
from dreamina_api.platform_store import PlatformStore,PlatformError
from dreamina_api.egress_clients import close_account_client_pool


def fixture(tmp_path,*,logged=True,enabled=True):
    store=PlatformStore('sqlite:///'+str(tmp_path/'service.db'),encryption_key=Fernet.generate_key())
    data={'id':'a','name':'A','enabled':enabled,'capabilities':['seedance-2.0-mini']}
    if logged:data['session_id']='fixture-private-session'
    store.create_account(data)
    proxy=store.create_proxy({'name':'proxy','url':'http://private-user:private-password@proxy.example:80'})
    calls=[]
    async def probe(_):calls.append('probe');return {'ok':True,'exit_ip':'203.0.113.9','latency_ms':3,'country':'US','timezone':'UTC'}
    health=ProxyHealthManager(store,probe=probe)
    class Workers:
        running=logged
        stop_error=False
        def status(self,_):return {'state':'running' if self.running else 'stopped'}
        async def stop(self,aid):
            calls.append('stop');assert store.get_account(aid)['enabled'] is False
            if self.stop_error:raise PlatformError('Worker has not exited','worker_stop_pending',status=409)
            self.running=False
        async def start(self,aid):calls.append('start');assert store.get_account(aid)['enabled'];self.running=True;return {'state':'running'}
    workers=Workers()
    async def check(aid,settings):
        calls.append('check');assert settings.egress.proxy_id==proxy['id'] and settings.egress.httpx_proxy
        assert not store.get_account(aid)['enabled'];return {'status':'ready','balance':100}
    service=ProxyBindingService(store,workers,health,check)
    return SimpleNamespace(store=store,proxy=proxy,service=service,health=health,workers=workers,calls=calls)


def test_binding_orders_probe_freeze_stop_check_start(tmp_path):
    async def run():
        e=fixture(tmp_path);r=await e.service.bind('a',e.proxy['id'])
        assert r['proxy_changed'] and r['account']['enabled'] and r['account']['status']=='ready'
        assert e.calls==['probe','stop','check','start']
        assert not e.store.list_proxy_changes();await e.service.close();await e.health.close();await close_account_client_pool()
    asyncio.run(run())


def test_disabled_account_stays_disabled_and_is_not_started(tmp_path):
    async def run():
        e=fixture(tmp_path,enabled=False);await e.service.bind('a',e.proxy['id'])
        assert not e.store.get_account('a')['enabled'] and 'start' not in e.calls
    asyncio.run(run())


def test_prelogin_account_chooses_proxy_without_old_cookie_check(tmp_path):
    async def run():
        e=fixture(tmp_path,logged=False);r=await e.service.bind('a',e.proxy['id'])
        assert r['login_required'] and r['account']['status']=='login_required' and r['account']['enabled']
        assert 'check' not in e.calls and 'start' not in e.calls
        assert e.store.resolve_account_egress('a').proxy_id==e.proxy['id']
    asyncio.run(run())


def test_failed_account_check_keeps_new_proxy_disabled(tmp_path):
    async def run():
        e=fixture(tmp_path)
        async def check(*_):raise PlatformError('private-cookie=secret',code='invalid_session',status=401)
        e.service.account_check=check
        with pytest.raises(PlatformError) as error:await e.service.bind('a',e.proxy['id'])
        assert error.value.code=='account_proxy_check_failed' and 'private-cookie' not in str(error.value)
        account=e.store.get_account('a');assert account['proxy_id']==e.proxy['id'] and not account['enabled'] and account['status']=='error'
        assert 'start' not in e.calls and not e.store.list_proxy_changes()
    asyncio.run(run())


@pytest.mark.parametrize('code',['network_error','proxy_error','upstream_403','upstream_-6','upstream_500'])
def test_transient_verification_keeps_checking_then_recovers_same_proxy(tmp_path,code):
    async def run():
        e=fixture(tmp_path);clock=SimpleNamespace(now=1000.0);e.store.clock=lambda:clock.now
        attempts=[];original_check=e.service.account_check
        async def check(*args):
            attempts.append(args[1].egress.httpx_proxy)
            if len(attempts)==1:raise PlatformError('private-cookie=secret',code=code,status=503)
            return await original_check(*args)
        e.service.account_check=check
        result=await e.service.bind('a',e.proxy['id'])
        assert result['pending'] and not result['proxy_changed'] and result['state']=='checking'
        assert result['retry_at']==1005 and result['retry_count']==1 and 'private-cookie' not in json.dumps(result)
        account=e.store.get_account('a');assert account['status']=='unverified' and not account['enabled'] and account['secret_configured']
        assert account['proxy_change_state']=='checking' and account['proxy_retry_count']==1 and account['proxy_retry_at']==1005
        assert e.service._retries and not e.workers.running and e.calls.count('stop')==1
        await e.service.close()
        recovered=ProxyBindingService(e.store,e.workers,e.health,check)
        await recovered.recover();assert len(attempts)==1  # Durable backoff survives restart.
        clock.now=1006;await recovered.recover()
        assert len(attempts)==2 and attempts[0]==attempts[1] and e.calls.count('stop')==1
        assert not e.store.list_proxy_changes() and e.store.get_account('a')['enabled'] and e.workers.running
        await recovered.close();await e.health.close();await close_account_client_pool()
    asyncio.run(run())


def test_unknown_verification_failure_and_manual_disabled_state_remain_safe(tmp_path):
    async def run():
        e=fixture(tmp_path,enabled=False);clock=SimpleNamespace(now=1000.0);e.store.clock=lambda:clock.now
        async def unknown(*_):raise RuntimeError('sensitive exception must never be shown')
        e.service.account_check=unknown
        result=await e.service.bind('a',e.proxy['id']);assert result['pending'] and result['retry_count']==1
        clock.now=1006;result=await e.service.bind('a',e.proxy['id'])
        assert result['retry_count']==2 and result['retry_at']==1016
        clock.now=1017
        async def ready(*_):return {'status':'ready','balance':100}
        e.service.account_check=ready;await e.service.bind('a',e.proxy['id'])
        assert not e.store.get_account('a')['enabled'] and 'start' not in e.calls and not e.store.list_proxy_changes()
        await e.service.close();await e.health.close()
    asyncio.run(run())


def test_cancelled_caller_does_not_cancel_binding_transition(tmp_path):
    async def run():
        e=fixture(tmp_path);entered=asyncio.Event();release=asyncio.Event()
        async def check(*_):entered.set();await release.wait();return {'status':'ready','balance':100}
        e.service.account_check=check
        caller=asyncio.create_task(e.service.bind('a',e.proxy['id']));await entered.wait();caller.cancel()
        with pytest.raises(asyncio.CancelledError):await caller
        pending=e.store.pending_proxy_change('a');assert pending['state']=='checking' and not e.store.get_account('a')['enabled']
        release.set();await e.service._operations['a'][1]
        assert e.store.get_account('a')['enabled'] and not e.store.pending_proxy_change('a')
    asyncio.run(run())


def test_stopped_worker_failure_is_resumable_and_generic_account_edits_cannot_bypass(tmp_path):
    async def run():
        e=fixture(tmp_path);e.workers.stop_error=True
        # Fixture must use the exact public error contract.
        async def stopped(_):e.calls.append('stop');raise PlatformError('Owned worker has not exited.',code='worker_stop_pending',status=409)
        e.workers.stop=stopped
        with pytest.raises(PlatformError):await e.service.bind('a',e.proxy['id'])
        change=e.store.pending_proxy_change('a');assert change['state']=='pending' and change['previous_running']
        for op in (lambda:e.store.update_account('a',{'enabled':True}),lambda:e.store.begin_account_delete('a')):
            with pytest.raises(PlatformError) as error:op()
            assert error.value.code=='proxy_change_in_progress'
        async def stop(_):e.calls.append('stop');e.workers.running=False
        e.workers.stop=stop
        recovered=ProxyBindingService(e.store,e.workers,e.health,e.service.account_check)
        await recovered.recover()
        assert e.store.get_account('a')['enabled'] and e.workers.running and not e.store.list_proxy_changes()
    asyncio.run(run())


def test_generic_enable_during_session_check_is_rejected(tmp_path):
    async def run():
        e=fixture(tmp_path)
        async def check(aid,_):
            with pytest.raises(PlatformError) as error:e.store.update_account(aid,{'enabled':True})
            assert error.value.code=='proxy_change_in_progress'
            with pytest.raises(PlatformError) as login_error:e.store.begin_browser_login(aid)
            assert login_error.value.code=='proxy_change_in_progress'
            return {'status':'ready','balance':100}
        e.service.account_check=check;await e.service.bind('a',e.proxy['id'])
    asyncio.run(run())


def test_crash_after_verified_binding_before_worker_restart_is_resumable(tmp_path):
    async def run():
        e=fixture(tmp_path);attempts=[]
        async def failed_start(_):attempts.append('crashed-before-start');raise RuntimeError('Synthetic process exit before start')
        e.workers.start=failed_start
        with pytest.raises(RuntimeError):await e.service.bind('a',e.proxy['id'])
        pending=e.store.pending_proxy_change('a');assert pending['state']=='restarting' and pending['previous_running']
        assert e.store.get_account('a')['status']=='ready' and e.store.get_account('a')['enabled']
        with pytest.raises(PlatformError) as error:e.store.update_account('a',{'enabled':False})
        assert error.value.code=='proxy_change_in_progress'
        calls_before=list(e.calls)
        async def started(_):attempts.append('started');e.workers.running=True;return {'state':'running'}
        e.workers.start=started
        recovered=ProxyBindingService(e.store,e.workers,e.health,e.service.account_check)
        await recovered.recover()
        assert attempts==['crashed-before-start','started'] and e.calls==calls_before
        assert not e.store.pending_proxy_change('a') and e.workers.running
    asyncio.run(run())


def test_process_shutdown_during_check_leaves_durable_checking_state(tmp_path):
    async def run():
        e=fixture(tmp_path);entered=asyncio.Event()
        async def interrupted(*_):entered.set();await asyncio.Future()
        e.service.account_check=interrupted
        caller=asyncio.create_task(e.service.bind('a',e.proxy['id']));await entered.wait();await e.service.close()
        with pytest.raises(asyncio.CancelledError):await caller
        pending=e.store.pending_proxy_change('a');assert pending['state']=='checking' and not e.store.get_account('a')['enabled']
        async def checked(*_):return {'status':'ready','balance':100}
        recovered=ProxyBindingService(e.store,e.workers,e.health,checked);await recovered.recover()
        assert e.store.get_account('a')['enabled'] and e.workers.running and not e.store.list_proxy_changes()
    asyncio.run(run())


def test_binding_validation_happens_before_probe(tmp_path):
    async def run():
        e=fixture(tmp_path)
        for pid,kw in [('wrong',{}),(e.proxy['id'],{'confirm_capacity_override':'true'})]:
            with pytest.raises(PlatformError):await e.service.bind('a',pid,**kw)
        assert not e.calls
    asyncio.run(run())


def api_fixture(tmp_path):
    e=fixture(tmp_path);e.store.bootstrap_admin('admin','fixture-password')
    admin=create_admin_router(e.store);app=FastAPI();app.include_router(admin);app.include_router(create_proxy_router(e.store,e.service,admin))
    return e,TestClient(app)


def login(client):
    r=client.post('/api/admin/login',json={'username':'admin','password':'fixture-password'});assert r.status_code==200
    client.headers['X-CSRF-Token']=r.json()['csrf_token']


def test_proxy_admin_routes_auth_csrf_redaction_and_atomic_create(tmp_path):
    e,client=api_fixture(tmp_path)
    with client:
        assert client.get('/api/admin/proxies').status_code==401
        login(client)
        bad=client.post('/api/admin/proxies',json={'url':'http://x.example:80'},headers={'X-CSRF-Token':'bad'});assert bad.status_code==403
        r=client.post('/api/admin/proxies',json={'name':'New','url':'http://another-user:another-secret@new.example:80'});assert r.status_code==201
        pid=r.json()['id'];assert 'another-secret' not in r.text and 'another-user' not in r.text
        probe=client.post('/api/admin/proxies/'+pid+'/test');assert probe.status_code==200 and probe.json()['exit_ip']=='203.0.113.9'
        r=client.post('/api/admin/accounts',json={'id':'new','name':'new','proxy_id':pid});assert r.status_code==201
        assert r.json()['proxy_id']==pid
        listed=client.get('/api/admin/proxies');assert listed.status_code==200 and 'private-password' not in listed.text
        assert client.delete('/api/admin/proxies/'+pid).status_code==409
        assert client.patch('/api/admin/accounts/new',json={'proxy':'http://u:p@bad.example:80'}).status_code==400


def test_proxy_api_import_validation_and_binding_passthrough(tmp_path):
    e,client=api_fixture(tmp_path)
    with client:
        login(client)
        bad=client.post('/api/admin/proxies/batch',json={'lines':'http://user:secret@bad.example:no'})
        assert bad.status_code==400 and 'secret' not in bad.text
        imported=client.post('/api/admin/proxies/batch',json={'lines':'http://a.example:80\nhttp://a.example:80'});assert imported.status_code==200
        assert len({x['id'] for x in imported.json()['items']})==1
        bound=client.post('/api/admin/accounts/a/proxy',json={'proxy_id':e.proxy['id']});assert bound.status_code==200 and bound.json()['proxy_changed']
        invalid=client.post('/api/admin/accounts/a/proxy',json={'proxy_id':e.proxy['id'],'force_direct':True});assert invalid.status_code==400
