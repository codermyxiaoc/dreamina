"""Credits and account failover are synthetic; no browser or upstream generation."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
import pytest

from dreamina_api.chrome_bridge import BridgeStore,BridgeError
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.platform_runtime import PlatformRuntime
import dreamina_api.platform_runtime as runtime_module
from dreamina_api.platform_store import PlatformStore,PlatformError

MODEL='seedance-2.0-mini'
SPEC={'model':MODEL,'prompt':'synthetic cloud','duration':4,'seconds':'4','resolution':'720p','ratio':'16:9','mode':'omni','references':[]}

class Clock:
    def __init__(self):self.value=1800000000.0
    def __call__(self):return self.value
    def advance(self,seconds):self.value+=seconds

def fixture(tmp_path,monkeypatch,*,names=('a','b'),allowed=None,balances=None):
    clock=Clock();bridge=BridgeStore(tmp_path/'bridge',clock=clock,heartbeat_timeout=300,claim_timeout=600)
    store=PlatformStore('sqlite:///'+str(tmp_path/'platform.sqlite3'),encryption_key=Fernet.generate_key(),clock=clock,worker_status=bridge.accounts)
    for name in names:
        store.create_account({'id':name,'name':name,'session_id':'synthetic-'+name,'max_concurrent':1,'capabilities':[MODEL]})
        bridge.heartbeat(name,'worker-'+name,ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',driver='native')
    key=store.create_key({'name':'synthetic caller','request_limit':10,'max_concurrent':10,'account_ids':list(allowed or names)})
    principal=store.authenticate_key(key['key'])
    balance_values=dict(balances or {name:100 for name in names});reads=[]
    class Client:
        api_base='https://dreamina-api.us.capcut.com'
        def __init__(self,settings):self.account=settings.session_id.removeprefix('synthetic-')
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def credits(self):
            reads.append(('credits',self.account));value=balance_values[self.account]
            if isinstance(value,Exception):raise value
            return deepcopy(value) if isinstance(value,dict) else {'credit':value}
        async def request(self,path,body):
            assert path.endswith('/get_history_by_ids'),'Only history queries are allowed'
            reads.append(('history',self.account));return {}
    monkeypatch.setattr(runtime_module,'DreaminaClient',Client)
    runtime=PlatformRuntime(store,bridge,clock=clock)
    task=store.reserve_task(principal,'video',MODEL,deepcopy(SPEC),idempotency_key='one-logical-task')
    job_spec={k:SPEC[k] for k in ('model','prompt','duration','resolution','ratio','mode','references')}
    job_spec.update(new_session=True,asset_owner_task_id=task['id'])
    job=bridge.enqueue(job_spec,account_id=task['account_id'])
    task=store.update_task(task['id'],bridge_job_id=job['id'],request_detail={'method':'POST','parameters':SPEC})
    return SimpleNamespace(clock=clock,bridge=bridge,store=store,runtime=runtime,task=task,principal=principal,
                           key=key,balances=balance_values,reads=reads,names=names)

def claim_current(env):
    task=env.store.get_task(env.task['id']);account=task['account_id'];worker='worker-'+account
    env.bridge.heartbeat(account,worker,ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',driver='native')
    claimed=env.bridge.claim(account,worker)
    assert claimed and claimed['id']==task['bridge_job_id']
    return claimed

async def check(env,claimed,required=20,**changes):
    quote={'source':'native_page','model':claimed['request']['model'],'request_fingerprint':quote_request_fingerprint(claimed['request']),
           'required_credits':required,'observed_at':env.clock(),**changes}
    return await env.runtime.credit_preflight(claimed,claimed['worker_id'],claimed['claim_token'],quote)

def test_insufficient_account_switches_same_public_task_without_second_quota(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':5,'b':100});first=claim_current(env)
        proof=await check(env,first,20)
        assert proof['allowed'] is False and env.bridge.get(first['id'])['status']=='failed'
        switched=await env.runtime.sync_task(env.task)
        assert switched['id']==env.task['id'] and switched['account_id']=='b' and switched['status']=='queued'
        assert switched['bridge_job_id']!=first['id'] and switched['submit_id'] is None
        assert env.store.list_keys()[0]['used_requests']==1
        repeated=env.store.reserve_task(env.principal,'video',MODEL,deepcopy(SPEC),idempotency_key='one-logical-task')
        assert repeated['id']==switched['id'] and repeated['_created'] is False
        second=claim_current(env);proof2=await check(env,second,35)
        assert proof2['allowed'] is True and proof2['required_credits']==35
        assert len(env.bridge.list_jobs())==2 and env.reads==[('credits','a'),('credits','b')]
        events=env.store.list_events(switched['id'])
        assert sum(e['stage']=='account_reassigned' for e in events)==1
        assert env.store.get_account('a')['balance']==5
        assert env.store.get_account('b')['balance']==100
    asyncio.run(run())

def test_each_account_gets_its_own_quote_and_all_insufficient_stops_without_submission(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,names=('a','b','c'),balances={'a':1,'b':5,'c':10})
        for account,price in [('a',20),('b',12),('c',30)]:
            claim=claim_current(env);assert claim['account_id']==account
            assert (await check(env,claim,price))['allowed'] is False
            task=await env.runtime.sync_task(env.task)
        assert task['status']=='failed' and task['error']['code']=='insufficient_credits'
        assert len(env.bridge.list_jobs())==3
        assert all(j['status']=='failed' and j['submit_id'] is None for j in env.bridge.list_jobs())
        assert env.store.list_keys()[0]['used_requests']==1 and len(env.store.list_tasks())==1
        assert [a['required_credits'] for a in task['request_detail']['credit_attempts']]==[20,12,30]
    asyncio.run(run())

@pytest.mark.parametrize('after_enqueue',[False,True])
def test_dispatch_crash_recovers_one_deterministic_job_without_another_account_switch(tmp_path,monkeypatch,after_enqueue):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':1,'b':100});first=claim_current(env);await check(env,first)
        original=env.bridge.enqueue;failed=[False]
        def crash(*args,**kwargs):
            if not failed[0]:
                failed[0]=True
                if after_enqueue:original(*args,**kwargs)
                raise RuntimeError('Synthetic dispatch acknowledgment loss')
            return original(*args,**kwargs)
        monkeypatch.setattr(env.bridge,'enqueue',crash)
        with pytest.raises(RuntimeError,match='acknowledgment'):await env.runtime.sync_task(env.task)
        saved=env.store.get_task(env.task['id']);assert saved['account_id']=='b' and saved['request_detail']['credit_failover_pending']
        new_id=saved['bridge_job_id']
        recovered=PlatformRuntime(env.store,env.bridge,clock=env.clock)
        current=await recovered.sync_task(saved)
        assert current['bridge_job_id']==new_id and current['account_id']=='b'
        assert not current['request_detail'].get('credit_failover_pending')
        assert len(env.bridge.list_jobs(account_id='b'))==1
        for _ in range(5):await recovered.sync_task(env.task)
        assert len(env.bridge.list_jobs(account_id='b'))==1 and env.store.list_keys()[0]['used_requests']==1
        assert sum(e['stage']=='account_reassigned' for e in env.store.list_events(env.task['id']))==1
    asyncio.run(run())

def test_concurrent_reconciliations_do_not_create_duplicate_jobs(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':1,'b':100});claim=claim_current(env);await check(env,claim)
        other=PlatformRuntime(env.store,env.bridge,clock=env.clock)
        await asyncio.gather(env.runtime.sync_task(env.task),other.sync_task(env.task),env.runtime.sync_task(env.task))
        assert len(env.bridge.list_jobs(account_id='b'))==1 and len(env.store.list_tasks())==1
        assert env.store.list_keys()[0]['used_requests']==1
    asyncio.run(run())

@pytest.mark.parametrize('balance',[None,{},True,float('nan'),float('inf'),-1])
def test_unknown_balance_never_approves_or_switches_account(tmp_path,monkeypatch,balance):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':balance,'b':100});claimed=claim_current(env)
        with pytest.raises(BridgeError,match='balance') as error:await check(env,claimed)
        assert error.value.status==503
        assert env.bridge.get(claimed['id'])['status']=='claimed'
        assert env.bridge.get(claimed['id'])['credit_preflight'] is None
        assert env.store.get_task(env.task['id'])['account_id']=='a' and len(env.bridge.list_jobs())==1
    asyncio.run(run())

def test_zero_price_with_zero_balance_is_allowed_and_zero_balance_is_schedulable(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':0,'b':0});claim=claim_current(env)
        assert (await check(env,claim,0))['allowed'] is True
        assert env.bridge.get(claim['id'])['status']=='claimed'
    asyncio.run(run())

def test_second_accounts_own_discount_can_afford_request_below_first_accounts_quote(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':1,'b':7});claimed=claim_current(env)
        assert not (await check(env,claimed,20))['allowed']
        switched=await env.runtime.sync_task(env.task);assert switched['account_id']=='b'
        second=claim_current(env);allowed=await check(env,second,5)
        assert allowed['allowed'] and allowed['balance']==7 and allowed['required_credits']==5
        assert len(env.bridge.list_jobs())==2 and env.store.list_keys()[0]['used_requests']==1
    asyncio.run(run())

def test_busy_other_account_waits_then_switches_when_it_becomes_available(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':1,'b':100})
        busy=env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'existing'},account_id='b')
        claim=claim_current(env);await check(env,claim)
        waiting=await env.runtime.sync_task(env.task)
        assert waiting['status']=='queued' and waiting['account_id']=='a'
        assert waiting['progress_details']['stage']=='waiting_for_credit_account'
        assert len(env.bridge.list_jobs())==1
        env.store.update_task(busy['id'],status='completed')
        switched=await env.runtime.sync_task(waiting)
        assert switched['account_id']=='b' and len(env.bridge.list_jobs())==2
        assert len(switched['request_detail']['credit_attempts'])==1
    asyncio.run(run())

def test_account_scope_prevents_using_an_unassigned_funded_account(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,allowed=('a',),balances={'a':1,'b':100});claim=claim_current(env);await check(env,claim)
        result=await env.runtime.sync_task(env.task)
        assert result['status']=='failed' and result['error']['code']=='insufficient_credits'
        assert not env.bridge.list_jobs(account_id='b') and env.reads==[('credits','a')]
    asyncio.run(run())

def test_waiting_for_busy_account_is_bounded_without_retrying_failed_account(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':1,'b':100})
        env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'existing'},account_id='b')
        claim=claim_current(env);await check(env,claim)
        waiting=await env.runtime.sync_task(env.task);assert waiting['status']=='queued'
        env.clock.advance(61)
        failed=await env.runtime.sync_task(waiting)
        assert failed['status']=='failed' and failed['error']['code']=='queue_timeout'
        assert len(env.bridge.list_jobs())==1 and len(failed['request_detail']['credit_attempts'])==1
    asyncio.run(run())

@pytest.mark.parametrize('change',['deleted_key','revoked_key','deleted_target'])
def test_deletion_or_revocation_cannot_be_bypassed_by_failover(tmp_path,monkeypatch,change):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':1,'b':100});claim=claim_current(env);await check(env,claim)
        if change=='deleted_key':env.store.delete_key(env.key['item']['id'])
        elif change=='revoked_key':env.store.update_key(env.key['item']['id'],{'enabled':False})
        else:
            env.store.begin_account_delete('b');env.store.delete_account('b')
        current=await env.runtime.sync_task(env.task)
        assert current['status']=='failed' and len(env.bridge.list_jobs())==1
        assert current['error']['code']==('insufficient_credits' if change=='deleted_target' else 'api_key_revoked')
        if change!='deleted_target':
            with pytest.raises(PlatformError) as error:env.store.reserve_task(env.principal,'video',MODEL,SPEC)
            assert error.value.code=='invalid_api_key'
    asyncio.run(run())

def test_submitted_or_uncertain_bridge_job_cannot_supply_failover_proof(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch);claimed=claim_current(env);proof=await check(env,claimed,20)
        assert proof['allowed']
        sid=str(uuid.uuid4());env.bridge.report(claimed['id'],claimed['worker_id'],claimed['claim_token'],status='submitted',submit_id=sid)
        with pytest.raises(BridgeError):await check(env,claimed,200)
        with pytest.raises(PlatformError,match='Only a confirmed'):env.store.reassign_insufficient_credits(env.task['id'],env.bridge.get(claimed['id']))
        assert len(env.bridge.list_jobs())==1 and env.store.get_task(env.task['id'])['account_id']=='a'
    asyncio.run(run())

def test_uncertain_public_task_is_not_reassigned_even_with_prior_no_submission_proof(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch,balances={'a':1,'b':100});claimed=claim_current(env);await check(env,claimed)
        env.store.update_task(env.task['id'],status='uncertain',error={'code':'synthetic_ambiguity','message':'Review required'})
        with pytest.raises(PlatformError):env.store.reassign_insufficient_credits(env.task['id'],env.bridge.get(claimed['id']))
        unchanged=await env.runtime.sync_task(env.task)
        assert unchanged['status']=='uncertain' and unchanged['error']['code']=='synthetic_ambiguity'
        assert len(env.bridge.list_jobs())==1 and env.store.get_task(env.task['id'])['account_id']=='a'
    asyncio.run(run())

def test_wrong_quote_identity_and_stale_quotes_are_rejected_before_permission(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch);claimed=claim_current(env)
        with pytest.raises(ValueError):await check(env,claimed,request_fingerprint='mismatch')
        assert not env.reads
        with pytest.raises(ValueError):await check(env,claimed,observed_at=env.clock()-61)
        assert env.bridge.get(claimed['id'])['credit_preflight'] is None
    asyncio.run(run())

def test_uncertain_public_task_cannot_receive_a_new_preflight_approval(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch);claimed=claim_current(env)
        env.store.update_task(env.task['id'],status='uncertain',error={'code':'independent_review','message':'Review required'})
        with pytest.raises(BridgeError):await check(env,claimed,0)
        assert not env.reads and env.bridge.get(claimed['id'])['credit_preflight'] is None
        assert env.store.get_task(env.task['id'])['status']=='uncertain'
    asyncio.run(run())


def test_same_preflight_replay_is_serialized_and_cannot_overwrite_approval(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch);claimed=claim_current(env)
        results=await asyncio.gather(check(env,claimed),check(env,claimed))
        assert all(r['allowed'] for r in results) and env.reads==[('credits','a')]
        env.balances['a']=0
        repeated=await check(env,claimed)
        assert repeated==results[0] and env.bridge.get(claimed['id'])['status']=='claimed'
        env.clock.advance(1)
        fresh=await check(env,claimed)
        assert fresh['allowed'] is False and env.reads==[('credits','a'),('credits','a')]
    asyncio.run(run())


def test_task_becoming_uncertain_during_balance_request_is_not_approved(tmp_path,monkeypatch):
    async def run():
        env=fixture(tmp_path,monkeypatch);claimed=claim_current(env)
        async def changed(*_):
            env.store.update_task(env.task['id'],status='uncertain')
            return {'balance':100,'status':'ready'}
        env.runtime.check_account=changed
        with pytest.raises(BridgeError,match='state changed'):await check(env,claimed)
        assert env.bridge.get(claimed['id'])['credit_preflight'] is None
    asyncio.run(run())


@pytest.mark.parametrize('credit',[{'credit':True},{'credit':{'total_credit':float('nan')}},{'credit':{'gift_credit':True,'purchase_credit':100}}, {'credit':{'total_credit':-1,'purchase_credit':100}}, {'credit':{'purchase_credit':float('inf')}},{}])
def test_credit_balance_does_not_guess_from_invalid_buckets(credit):
    assert runtime_module.exact_credit_balance(credit) is None
