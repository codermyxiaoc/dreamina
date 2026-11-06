"""Regression evidence for ISSUES_V013: no website calls or paid generation."""
import asyncio,json,sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event

from dreamina_api.chrome_bridge import BridgeError
from dreamina_api.client import DreaminaClient,DreaminaError
from dreamina_api.config import Settings
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.egress import direct_egress
from dreamina_api.maintenance import cleanup_profile_cache
from dreamina_api.platform_store import PlatformError
from test_pipeline_integration_v6 import Pipeline,MODEL
from test_managed_workers import Harness,run
from test_worker_supervision import crash,state
from test_retention_v013 import setup as retention_fixture


async def central_commit_only(p):
    task=p.enqueue();job=p.claim(task);await p.prepare(job)
    quote={'source':'native_page','model':MODEL,'observed_at':p.now,'required_credits':20,'request_fingerprint':quote_request_fingerprint(job['request'])}
    await p.runtime.credit_preflight(job,job['worker_id'],job['claim_token'],quote)
    assert p.store.commit_submission_turn(task['id'],job['id'])['ready']
    assert p.bridge.get(job['id'])['activation_started_at'] is None
    return task,job


@pytest.mark.parametrize('old_record',[False,True])
def test_h2_central_only_commit_fails_safely_releases_hold_and_never_replays(tmp_path,monkeypatch,old_record):
    async def check():
        p=Pipeline(tmp_path,monkeypatch);p.store.update_account('one',{'max_inflight':1})
        task,job=await central_commit_only(p)
        if old_record:
            with sqlite3.connect(p.bridge.path) as db:db.execute("UPDATE jobs SET status='uncertain' WHERE id=?",(job['id'],))
            p.store.update_task(task['id'],status='uncertain')
        else:
            receipt=p.bridge.report(job['id'],job['worker_id'],job['claim_token'],status='uncertain',submission_window_closed=True)
            assert receipt['status']=='failed' and receipt['generation_not_activated']
            await p.runtime.reported_submission(receipt)
        result=await p.runtime.sync_task(task)
        assert result['status']=='failed' and result['error']['code']=='activation_not_committed'
        assert p.store.active_credit_holds()==[] and p.http_calls==[] and len(p.bridge.list_jobs())==1
        with pytest.raises(BridgeError):p.bridge.activate(job['id'],job['worker_id'],job['claim_token'])
        assert p.enqueue()['id']!=task['id']  # max_inflight=1 is usable again.
    asyncio.run(check())


@pytest.mark.parametrize('reason',['activated','known_id','legacy'])
def test_h2_never_releases_a_possibly_submitted_original(tmp_path,monkeypatch,reason):
    async def check():
        p=Pipeline(tmp_path,monkeypatch);task,job=await central_commit_only(p)
        kwargs={}
        if reason=='activated':p.bridge.activate(job['id'],job['worker_id'],job['claim_token'])
        elif reason=='known_id':kwargs['submit_id']='12345678-1234-1234-1234-123456789abc'
        else:
            with sqlite3.connect(p.bridge.path) as db:db.execute('UPDATE jobs SET claim_protocol_version=4 WHERE id=?',(job['id'],))
        receipt=p.bridge.report(job['id'],job['worker_id'],job['claim_token'],status='uncertain',**kwargs)
        assert receipt['status']=='uncertain' and not receipt.get('generation_not_activated')
        assert p.bridge.resolve_unactivated_uncertain(job['id'])['status']=='uncertain'
        assert len(p.store.active_credit_holds())==1
    asyncio.run(check())


def test_h2_report_and_late_activation_are_atomically_fenced(tmp_path,monkeypatch):
    async def check():
        p=Pipeline(tmp_path,monkeypatch);_,job=await central_commit_only(p)
        def activate():
            try:return p.bridge.activate(job['id'],job['worker_id'],job['claim_token'])
            except BridgeError:return None
        def uncertain():return p.bridge.report(job['id'],job['worker_id'],job['claim_token'],status='uncertain')
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(activate);b=pool.submit(uncertain);a.result();b.result()
        current=p.bridge.get(job['id'])
        assert (current['status']=='failed' and current['activation_started_at'] is None) or (current['status']=='uncertain' and current['activation_started_at'] is not None)
    asyncio.run(check())


def test_m1_proxy_outage_never_opens_crash_circuit_and_recovers_automatically(tmp_path):
    h=Harness(tmp_path);h.worker.jitter=lambda:0;run(h.worker.start('first'));crash(h);run(h.worker.scan())
    healthy=False
    def egress(_):
        if not healthy:raise PlatformError('Proxy unavailable',code='proxy_unhealthy',status=503)
        return direct_egress()
    h.worker._account_egress=egress;h.time+=5;run(h.worker.scan())
    for _ in range(8):
        current=state(h);assert not current['circuit_open'];h.time=current['next_restart_at'];run(h.worker.scan())
    assert len(h.calls)==1
    healthy=True;h.time=state(h)['next_restart_at'];run(h.worker.scan());assert len(h.calls)==2 and not state(h)['circuit_open']


@pytest.mark.parametrize('stopped',[False,True])
def test_m1_old_proxy_circuit_is_recovered_but_manual_stop_is_preserved(tmp_path,stopped):
    h=Harness(tmp_path);run(h.worker.start('first'));crash(h)
    saved=state(h);saved.update(circuit_open=True,reason='proxy_unhealthy',desired_running=not stopped)
    h.worker._save_supervision('first',saved);run(h.worker.scan())
    assert len(h.calls)==(1 if stopped else 2)


def test_l1_fenced_browser_proxy_crashes_back_off_without_opening_circuit(tmp_path):
    from dreamina_api.browser_worker import write_json
    h=Harness(tmp_path);run(h.worker.start('first'))
    for index in range(7):
        record=h.worker._record('first')
        write_json(h.worker.paths('first').state/'proxy-failure.json',{'account_id':'first','pid':record['pid'],
            'instance_id':record['instance_id'],'observed_at':h.time,'error_code':'proxy_error'})
        crash(h);result=run(h.worker.scan());saved=state(h)
        assert not saved['circuit_open'] and saved['proxy_failures']==index+1
        assert next(n for n in result if n['account_id']=='first')['worker_error_code']=='proxy_error'
        h.time=saved['next_restart_at'];run(h.worker.scan())
    assert len(h.calls)==8


def test_m2_expired_cdn_url_is_refreshed_for_proxied_account_without_generation(tmp_path,monkeypatch):
    import dreamina_api.platform_runtime as runtime_module
    from test_delivery_reliability import Store
    async def check():
        counts={'history':0,'cdn':0};store=Store()
        store.settings_for_account=lambda *args,**kw:Settings(session_id='synthetic',proxy='http://u:p@127.0.0.1:9')
        def handler(request):
            if request.url.host=='dreamina-api.us.capcut.com':
                assert request.url.path.endswith('/get_history_by_ids');counts['history']+=1
                return httpx.Response(200,json={'ret':'0','data':{'original-upstream-id':{'status':50,'item_list':[{'video':{'transcoded_video':{'origin':{'video_url':'https://cdn.example.test/'+str(counts['history'])+'.mp4'}}}}]}}})
            assert 'cookie' not in request.headers;counts['cdn']+=1
            return httpx.Response(403 if counts['cdn']==1 else 200,content=b'fixture-video')
        async def fast_sleep(*_):pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            monkeypatch.setattr(runtime_module,'DreaminaClient',lambda settings:DreaminaClient(settings,http=http))
            monkeypatch.setattr(runtime_module,'ROOT',tmp_path);monkeypatch.setattr(runtime_module.asyncio,'sleep',fast_sleep)
            path=await runtime_module.PlatformRuntime(store,None).download_video(store.task)
        assert path.read_bytes()==b'fixture-video' and counts=={'history':2,'cdn':2}
    asyncio.run(check())


@pytest.mark.parametrize('failure',[403,404,503,'proxy','connect','read'])
def test_m2_download_classifies_origin_http_errors_separately_from_proxy_transport(tmp_path,failure):
    async def check():
        def handler(request):
            if request.url.host=='dreamina-api.us.capcut.com':
                return httpx.Response(200,json={'ret':'0','data':{'test':{'status':50,'item_list':[{'video':{'transcoded_video':{'origin':{'video_url':'https://cdn.example.test/test.mp4'}}}}]}}})
            if type(failure) is int:return httpx.Response(failure)
            raise {'proxy':httpx.ProxyError,'connect':httpx.ConnectError,'read':httpx.ReadError}[failure]('synthetic proxy-password must not leak',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=DreaminaClient(Settings(session_id='synthetic',proxy='http://user:private@127.0.0.1:9'),http=http)
            with pytest.raises(DreaminaError) as error:await client.download('test',tmp_path/'movie.mp4')
            assert error.value.code==('proxy_error' if failure in ('proxy','connect') else 'download_failed')
            assert 'private' not in str(error.value) and 'proxy-password' not in str(error.value)
    asyncio.run(check())


def test_l7_status_lookup_failure_is_retryable_and_does_not_consume_request_quota(tmp_path,monkeypatch):
    p=Pipeline(tmp_path,monkeypatch);before=p.store.list_keys()[0]['used_requests']
    def unavailable():raise sqlite3.OperationalError('database busy')
    p.store.worker_status=unavailable
    with pytest.raises(PlatformError) as error:p.enqueue()
    assert error.value.code=='worker_status_unavailable' and error.value.status==503
    assert p.store.list_keys()[0]['used_requests']==before and p.store.list_tasks()==[]


def test_m7_startup_cache_cleanup_only_removes_allowlisted_disposable_files(tmp_path):
    profile=tmp_path/'profile';cache=profile/'Default/Cache/a';cache.parent.mkdir(parents=True);cache.write_bytes(b'x'*30)
    credentials=[profile/'Default/Cookies',profile/'Default/Local Storage/auth',profile/'Local State',profile/'IndexedDB/site']
    for path in credentials:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'keep')
    result=cleanup_profile_cache(profile,owned_lock=True,threshold_bytes=10)
    assert result['bytes_reclaimed']==30 and not cache.exists() and all(p.read_bytes()==b'keep' for p in credentials)


@pytest.mark.parametrize('lock',['SingletonLock','SingletonSocket','SingletonCookie','.dreamina-worker.lock'])
def test_m7_unknown_browser_lock_prevents_cache_cleanup(tmp_path,lock):
    cache=tmp_path/'Default/Cache/a';cache.parent.mkdir(parents=True);cache.write_bytes(b'x'*30);(tmp_path/lock).write_text('owner')
    assert cleanup_profile_cache(tmp_path,threshold_bytes=10)['skipped'] and cache.exists()


def test_l8_new_active_reference_between_scan_and_lock_preserves_files(tmp_path):
    store,bridge,clean,add,collect,_=retention_fixture(tmp_path,{'MAINTENANCE_MEDIA_RETENTION_DAYS':30,'MAINTENANCE_INPUT_RETENTION_DAYS':30})
    source,paths=add();original=store.transaction;inserted=False
    @contextmanager
    def interleaved():
        nonlocal inserted
        if not inserted:
            inserted=True;add('queued',reference_to=source)
        with original() as conn:yield conn
    store.transaction=interleaved
    try:assert collect()['files_deleted']==0 and all(p.exists() for p in paths)
    finally:store.engine.dispose()


def test_l8_retention_does_not_scan_json_with_like_inside_global_transaction(tmp_path):
    store,bridge,clean,add,collect,_=retention_fixture(tmp_path,{'MAINTENANCE_MEDIA_RETENTION_DAYS':30})
    add();statements=[]
    event.listen(store.engine,'before_cursor_execute',lambda conn,cursor,statement,*args:statements.append(statement))
    try:
        assert collect()['files_deleted']==1
        assert not any('LIKE' in text.upper() for text in statements)
    finally:store.engine.dispose()
