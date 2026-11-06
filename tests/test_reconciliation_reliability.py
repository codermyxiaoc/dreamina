"""Status-query recovery is read-only, bounded, and independent of browser retries."""
import asyncio
from copy import deepcopy
import json
import time
import uuid

from cryptography.fernet import Fernet
import httpx
import pytest

from dreamina_api.chrome_bridge import BridgeStore
from dreamina_api.client import DreaminaError, HISTORY_PATH
from dreamina_api.payloads import build_generation
from dreamina_api.platform_runtime import PlatformRuntime
from dreamina_api.platform_store import PlatformStore
import dreamina_api.platform_runtime as runtime_module


class Clock:
    def __init__(self):self.value=1000.0
    def __call__(self):return self.value
    def advance(self,amount):self.value+=amount


def fixture(tmp_path,*,with_id=True,uncertain=False):
    store=PlatformStore('sqlite:///:memory:',encryption_key=Fernet.generate_key())
    store.create_account({'id':'one','name':'Synthetic account','region':'US','session_id':'synthetic-session',
                          'capabilities':['seedance-2.0-mini'],'max_concurrent':8,'max_inflight':8})
    key=store.create_key({'name':'Synthetic key','max_concurrent':100,'rate_limit_per_minute':10000})
    principal=store.authenticate_key(key['key'])
    bridge=BridgeStore(tmp_path/'bridge')
    spec={'model':'seedance-2.0-mini','prompt':'Synthetic ceramic cube','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni','references':[]}
    task=store.reserve_task(principal,'video',spec['model'],spec)
    job=bridge.enqueue(spec,account_id='one')
    bridge.heartbeat('one','fixture-worker',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate')
    claim=bridge.claim('one','fixture-worker')
    sid=str(uuid.uuid4()) if with_id else None
    if with_id:bridge.report(job['id'],'fixture-worker',claim['claim_token'],status='uncertain' if uncertain else 'submitted',submit_id=sid)
    task=store.update_task(task['id'],bridge_job_id=job['id'],status='uncertain' if uncertain else 'submitted' if with_id else 'claimed')
    record={'submit_id':sid,'created_time':time.time()+1,'workspace_id':'123456',
            'draft_content':build_generation(spec,workspace_id='123456')['body']['draft_content'],'status':20,'item_list':[]}
    return store,bridge,task,claim,record


def complete(record):
    result=deepcopy(record);result['status']=50
    result['item_list']=[{'video':{'duration_ms':4000,'transcoded_video':{'origin':{
        'video_url':'https://cdn.example.test/synthetic.mp4','width':1280,'height':720,'format':'mp4'}}}}]
    return result


def client_script(monkeypatch,outcomes):
    calls=[]
    class Client:
        api_base='https://dreamina-api.us.capcut.com'
        def __init__(self,settings):self.settings=settings
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def request(self,path,body):
            calls.append((path,deepcopy(body),self.settings.session_id))
            assert path==HISTORY_PATH, 'No generation or ledger endpoint may be used'
            item=outcomes.pop(0)
            if isinstance(item,BaseException):raise item
            if callable(item):return await item()
            return deepcopy(item)
    monkeypatch.setattr(runtime_module,'DreaminaClient',Client)
    return calls


def status_error(status):
    request=httpx.Request('POST','https://upstream.example.test/history?private-token=never-log')
    response=httpx.Response(status,request=request)
    return httpx.HTTPStatusError('private-cookie=never-log',request=request,response=response)


def test_known_id_uncertain_recovers_after_network_failure_without_resubmitting(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path,uncertain=True)
        clock=Clock();runtime=PlatformRuntime(store,bridge,clock=clock)
        calls=client_script(monkeypatch,[DreaminaError('private-cookie=never-log',code='network_error'),{record['submit_id']:complete(record)}])
        first=await runtime.sync_task(task)
        assert first['status']=='uncertain' and first['submit_id']==record['submit_id']
        for _ in range(25):await runtime.sync_task(task)
        assert len(calls)==1
        clock.advance(runtime.poll_interval)
        recovered=await runtime.sync_task(task)
        assert recovered['status']=='completed' and recovered['submit_id']==record['submit_id']
        assert len(calls)==2 and all(c[1]=={'submit_ids':[record['submit_id']]} for c in calls)
        assert all(c[2]=='synthetic-session' for c in calls)
        assert len(bridge.list_jobs(account_id='one'))==1
        events=store.list_events(task['id'])
        assert any(e['stage']=='upstream_query_retry' for e in events)
        assert any(e['stage']=='upstream_query_recovered' for e in events)
        assert 'private-cookie' not in json.dumps(events)
    asyncio.run(run())


@pytest.mark.parametrize(('error','reason','first_delay'),[
    (status_error(429),'rate_limited',15),
    (status_error(503),'upstream_unavailable',3),
    (status_error(401),'auth_required',60),
    (status_error(403),'auth_required',60),
    (DreaminaError('private-cookie',code='upstream_429'),'rate_limited',15),
    (DreaminaError('private-cookie',code='upstream_-6'),'query_rejected',60),
    (httpx.ReadTimeout('private-token=never-log'),'network_unavailable',3),
])
def test_query_errors_preserve_task_and_apply_bounded_sanitized_backoff(tmp_path,monkeypatch,error,reason,first_delay):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        clock=Clock();runtime=PlatformRuntime(store,bridge,clock=clock)
        calls=client_script(monkeypatch,[error]*10)
        for i in range(10):
            current=await runtime.sync_task(task)
            assert current['status']=='submitted' and current['submit_id']==record['submit_id']
            state=runtime._polls[task['id']]
            assert state['reason']==reason
            assert 0<state['delay']<=60
            if not i:assert state['delay']==first_delay
            count=len(calls)
            await runtime.sync_task(task);assert len(calls)==count
            clock.advance(state['delay'])
        events=[e for e in store.list_events(task['id']) if e['stage']=='upstream_query_retry']
        assert len(events)<=6  # Once capped, repeated errors do not fill the audit log.
        assert 'private-' not in json.dumps(events)
        assert 'never-log' not in json.dumps(current['error'])
        assert bridge.get(task['bridge_job_id'])['status']=='submitted'
    asyncio.run(run())


def test_healthy_processing_queries_are_rate_limited_even_from_many_gets(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        clock=Clock();runtime=PlatformRuntime(store,bridge,clock=clock)
        calls=client_script(monkeypatch,[{record['submit_id']:record}]*2)
        result=await runtime.sync_task(task)
        assert result['status']=='in_progress'
        await asyncio.gather(*(runtime.sync_task(task) for _ in range(50)))
        assert len(calls)==1
        clock.advance(max(5,runtime.poll_interval))
        await runtime.sync_task(task)
        assert len(calls)==2
    asyncio.run(run())


def test_concurrent_queries_return_snapshot_without_queueing_second_request(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        started=asyncio.Event();release=asyncio.Event()
        async def delayed():started.set();await release.wait();return {record['submit_id']:record}
        calls=client_script(monkeypatch,[delayed])
        runtime=PlatformRuntime(store,bridge,clock=Clock())
        first=asyncio.create_task(runtime.sync_task(task));await started.wait()
        snapshots=await asyncio.wait_for(asyncio.gather(*(runtime.sync_task(task) for _ in range(20))),.5)
        assert all(s['status']=='submitted' for s in snapshots) and len(calls)==1
        release.set();assert (await first)['status']=='in_progress'
    asyncio.run(run())


def test_process_restart_resumes_same_persisted_id_after_in_memory_backoff(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path,uncertain=True)
        calls=client_script(monkeypatch,[status_error(503),{record['submit_id']:complete(record)}])
        original=PlatformRuntime(store,bridge,clock=Clock())
        assert (await original.sync_task(task))['status']=='uncertain'
        restarted=PlatformRuntime(store,bridge,clock=Clock())
        assert (await restarted.sync_task(store.get_task(task['id'])))['status']=='completed'
        assert len(calls)==2 and calls[0][1]==calls[1][1]
    asyncio.run(run())


def test_no_id_uncertain_remains_uncertain_without_network_or_identity_guessing(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,_=fixture(tmp_path,with_id=False,uncertain=True)
        calls=client_script(monkeypatch,[])
        runtime=PlatformRuntime(store,bridge)
        for _ in range(5):
            result=await runtime.sync_task(task)
            assert result['status']=='uncertain' and result['submit_id'] is None
        assert calls==[]
    asyncio.run(run())


def test_retry_does_not_overwrite_observed_page_progress(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        task=store.update_task(task['id'],status='in_progress',progress=37,
                               progress_details={'source':'page','observed_at':time.time(),'stage':'generating'})
        before=task['progress_details'];clock=Clock()
        client_script(monkeypatch,[status_error(503)])
        result=await PlatformRuntime(store,bridge,clock=clock).sync_task(task)
        assert result['progress']==37 and result['progress_details']==before
        assert result['status']=='in_progress'
    asyncio.run(run())


@pytest.mark.parametrize('data',[None,[],{'not-a-submit-id':{}},'invalid'])
def test_malformed_or_missing_query_results_do_not_fail_a_generation(tmp_path,monkeypatch,data):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path,uncertain=True)
        calls=client_script(monkeypatch,[data])
        runtime=PlatformRuntime(store,bridge)
        result=await runtime.sync_task(task)
        assert result['status']=='uncertain' and result['submit_id']==record['submit_id']
        assert len(calls)==1
    asyncio.run(run())


def test_identity_mismatch_keeps_uncertain_and_cannot_be_cleared_by_network_error(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        invalid=dict(record,submit_id=str(uuid.uuid4()))
        calls=client_script(monkeypatch,[{record['submit_id']:invalid},status_error(503)])
        clock=Clock();runtime=PlatformRuntime(store,bridge,clock=clock)
        first=await runtime.sync_task(task)
        assert first['status']=='uncertain' and first['error']['code']=='identity_unconfirmed'
        clock.advance(60)
        second=await runtime.sync_task(task)
        assert second['status']=='uncertain' and second['error']==first['error']
        assert len(calls)==2
    asyncio.run(run())


def test_unknown_upstream_status_cannot_clear_uncertain_or_mark_progress(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path,uncertain=True)
        record['status']=999
        client_script(monkeypatch,[{record['submit_id']:record}])
        runtime=PlatformRuntime(store,bridge)
        result=await runtime.sync_task(task)
        assert result['status']=='uncertain'
        assert runtime._polls[task['id']]['reason']=='unrecognized_task_state'
        assert result['progress'] is None
    asyncio.run(run())


def test_history_query_timeout_is_bounded_and_does_not_discard_submit_id(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        async def timeout():await asyncio.sleep(5)
        calls=client_script(monkeypatch,[timeout])
        runtime=PlatformRuntime(store,bridge,query_timeout=1)
        result=await asyncio.wait_for(runtime.sync_task(task),timeout=2)
        assert result['status']=='submitted' and result['submit_id']==record['submit_id']
        assert runtime._polls[task['id']]['reason']=='network_unavailable'
        assert len(calls)==1
    asyncio.run(run())


def test_only_explicit_plan_gate_blocks_the_account_model_once(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,claim,_=fixture(tmp_path,with_id=False)
        blocked=[];store.block_account_model=lambda *args,**kwargs:blocked.append((args,kwargs))
        bridge.telemetry(claim['id'],'fixture-worker',claim['claim_token'],
                         {'stage':'account_plan_required','observed_at':time.time(),'detail':{'generation_request_observed':False,'model_permission_confirmed':True}})
        bridge.report(claim['id'],'fixture-worker',claim['claim_token'],status='failed',error='Synthetic plan gate')
        calls=client_script(monkeypatch,[]);runtime=PlatformRuntime(store,bridge)
        assert (await runtime.sync_task(task))['status']=='failed'
        await runtime.sync_task(task)
        assert len(blocked)==1 and blocked[0][0]==('one','seedance-2.0-mini')
        assert blocked[0][1]=={'reason':'account_plan_required','cooldown_seconds':900}
        assert not calls
    asyncio.run(run())


@pytest.mark.parametrize('stage,status',[('account_plan_required','failed'),('subscription_panel_unresolved','uncertain')])
def test_generic_panel_or_legacy_event_does_not_block_model(tmp_path,monkeypatch,stage,status):
    async def run():
        store,bridge,task,claim,_=fixture(tmp_path,with_id=False)
        blocked=[];store.block_account_model=lambda *args,**kwargs:blocked.append((args,kwargs))
        bridge.telemetry(claim['id'],'fixture-worker',claim['claim_token'],
                         {'stage':stage,'observed_at':time.time(),'detail':{'generation_request_observed':False}})
        bridge.report(claim['id'],'fixture-worker',claim['claim_token'],status=status,error='Subscription panel; permission unknown')
        calls=client_script(monkeypatch,[]);runtime=PlatformRuntime(store,bridge)
        assert (await runtime.sync_task(task))['status']==status
        assert not blocked and not calls
    asyncio.run(run())


def test_confirmed_success_clears_corresponding_account_model_block_once(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        cleared=[];store.clear_account_model_block=lambda *args:cleared.append(args)
        calls=client_script(monkeypatch,[{record['submit_id']:complete(record)}])
        runtime=PlatformRuntime(store,bridge)
        assert (await runtime.sync_task(task))['status']=='completed'
        await runtime.sync_task(task)
        assert cleared==[('one','seedance-2.0-mini')] and len(calls)==1
    asyncio.run(run())


def test_late_query_does_not_overwrite_a_task_closed_while_request_was_in_flight(tmp_path,monkeypatch):
    async def run():
        store,bridge,task,_,record=fixture(tmp_path)
        async def operator_closes():
            store.update_task(task['id'],status='cancelled',error={'code':'operator_closed'})
            return {record['submit_id']:complete(record)}
        calls=client_script(monkeypatch,[operator_closes])
        runtime=PlatformRuntime(store,bridge)
        result=await runtime.sync_task(task)
        assert result['status']=='cancelled' and result['error']['code']=='operator_closed'
        assert task['id'] not in runtime._polls and task['id'] not in runtime._locks
        assert len(calls)==1
        assert bridge.get(task['bridge_job_id'])['status']=='submitted'
    asyncio.run(run())
