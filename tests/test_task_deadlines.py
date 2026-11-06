"""Bounded foreground deadlines keep original identities; no real network/media."""
from copy import deepcopy
import asyncio
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
import pytest

from dreamina_api.task_limits import TaskLimits
from dreamina_api.chrome_bridge import BridgeStore,BridgeError
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.openai_api import video_object
from dreamina_api.platform_store import PlatformStore
from dreamina_api.platform_runtime import PlatformRuntime
from dreamina_api.payloads import build_generation
from dreamina_api.client import HISTORY_PATH
import dreamina_api.platform_runtime as runtime_module


MODEL='seedance-2.0-mini'
SPEC={'model':MODEL,'prompt':'Synthetic deadlines','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni'}


def fixture(tmp_path,protocol=5):
    clock=SimpleNamespace(now=1000.)
    bridge=BridgeStore(tmp_path/'bridge',clock=lambda:clock.now,heartbeat_timeout=10000,claim_timeout=10000)
    job=bridge.enqueue(SPEC,account_id='one')
    extra={'slot_index':1,'slot_count':2,'owner_id':'parent-one'} if protocol>=5 else {}
    bridge.heartbeat('one','worker-one',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',protocol_version=protocol,driver='native',**extra)
    return SimpleNamespace(clock=clock,bridge=bridge,job=job,protocol=protocol)


def claim(env):
    value=env.bridge.claim('one','worker-one',capacity=2,request_id=str(uuid.uuid4()) if env.protocol>=5 else None)
    assert value and value['id']==env.job['id']
    return value


def activate(env,claimed):
    quote={'required_credits':0,'source':'native_page','observed_at':env.clock.now,'model':MODEL,'request_fingerprint':quote_request_fingerprint(claimed['request'])}
    env.bridge.credit_preflight(claimed['id'],claimed['worker_id'],claimed['claim_token'],quote,100)
    return env.bridge.activate(claimed['id'],claimed['worker_id'],claimed['claim_token'])


def task(**changes):
    return {'id':'video_'+'a'*32,'model':MODEL,'created_at':1000.,'status':'queued','request':{'seconds':'4','resolution':'720p'},**changes}


def job(**changes):
    return {'id':str(uuid.uuid4()),'created_at':1000.,'status':'queued','claimed_at':None,'activation_started_at':None,'submit_id':None,**changes}


def test_timeout_defaults_and_bounded_environment_overrides():
    assert TaskLimits.from_env({})==TaskLimits(queue=60,preparation=180,submission=60,generation=600,total=900)
    assert TaskLimits.from_env({'TASK_QUEUE_TIMEOUT_SECONDS':'5','NATIVE_PREPARATION_TIMEOUT_SECONDS':30,'NATIVE_SUBMISSION_TIMEOUT_SECONDS':'15','NATIVE_GENERATION_TIMEOUT_SECONDS':'60','TASK_TOTAL_TIMEOUT_SECONDS':'7200'})==TaskLimits(5,30,15,60,7200)


@pytest.mark.parametrize('key,value',[
    ('TASK_QUEUE_TIMEOUT_SECONDS',True),('TASK_QUEUE_TIMEOUT_SECONDS',4),('TASK_QUEUE_TIMEOUT_SECONDS',601),
    ('NATIVE_PREPARATION_TIMEOUT_SECONDS',29),('NATIVE_PREPARATION_TIMEOUT_SECONDS',901),
    ('NATIVE_SUBMISSION_TIMEOUT_SECONDS',14),('NATIVE_SUBMISSION_TIMEOUT_SECONDS',181),
    ('NATIVE_GENERATION_TIMEOUT_SECONDS',59),('NATIVE_GENERATION_TIMEOUT_SECONDS',3601),
    ('TASK_TOTAL_TIMEOUT_SECONDS',59),('TASK_TOTAL_TIMEOUT_SECONDS',7201),
    ('TASK_QUEUE_TIMEOUT_SECONDS','60.0'),('TASK_QUEUE_TIMEOUT_SECONDS',float('nan')),('TASK_QUEUE_TIMEOUT_SECONDS','-1'),
])
def test_invalid_timeout_configuration_cannot_disable_bounds(key,value):
    with pytest.raises(ValueError,match=key):TaskLimits.from_env({key:value})


@pytest.mark.parametrize('name,record,elapsed,code',[
    ('queue',job(),60,'queue_timeout'),
    ('preparation',job(status='claimed',claimed_at=1000.),180,'preparation_timeout'),
    ('submission',job(status='claimed',claimed_at=990.,activation_started_at=1000.),60,'submission_unconfirmed'),
    ('generation',job(status='submitted',claimed_at=900.,activation_started_at=950.,submitted_at=1000.,submit_id='known'),600,'generation_timeout'),
])
def test_each_stage_expires_at_its_boundary_not_before(name,record,elapsed,code):
    limits=TaskLimits(total=7200);value=task(status='in_progress' if record.get('submit_id') else 'queued')
    assert limits.expired(value,record,1000+elapsed-.01) is None
    assert limits.expired(value,record,1000+elapsed)[0]==code


def test_total_limit_is_not_reset_by_new_job_or_late_stage_transition():
    limits=TaskLimits()
    current=job(created_at=1899.,status='submitted',submitted_at=1899.,submit_id='known')
    assert limits.expired(task(status='in_progress'),current,1900)[0]=='task_timeout'
    for state in ('completed','failed','cancelled'):
        assert limits.expired(task(status=state),current,99999) is None


def test_credit_failover_wait_is_bounded_without_a_new_generation():
    current=job(status='failed',credit_preflight={'allowed':False,'checked_at':1005.})
    assert TaskLimits().expired(task(),current,1064.99) is None
    assert TaskLimits().expired(task(),current,1065)[0]=='queue_timeout'


def test_queue_deadline_fails_without_claim_and_never_requeues(tmp_path):
    env=fixture(tmp_path)
    value=env.bridge.deadline(env.job['id'],'queue_timeout','No slot accepted the task')
    assert value['status']=='failed' and value['submit_id'] is None and value['slot_released']
    assert env.bridge.claim('one','worker-one',capacity=2,request_id=str(uuid.uuid4())) is None
    assert len(env.bridge.list_jobs())==1


@pytest.mark.parametrize('protocol,expected',[(4,'uncertain'),(5,'failed')])
def test_preparation_timeout_distinguishes_protocol_proof_from_legacy_unknown(protocol,expected,tmp_path):
    env=fixture(tmp_path,protocol);claimed=claim(env)
    expired=env.bridge.deadline(claimed['id'],'preparation_timeout','Preparation deadline')
    assert expired['status']==expected and expired['submit_id'] is None
    assert expired['slot_released']==(expected=='failed')


def test_activated_without_id_is_uncertain_and_late_id_stays_on_original_job(tmp_path):
    env=fixture(tmp_path);claimed=claim(env);activate(env,claimed)
    value=env.bridge.deadline(claimed['id'],'submission_unconfirmed','No trustworthy ID yet')
    assert value['status']=='uncertain' and value['submit_id'] is None and not value['slot_released']
    with pytest.raises(BridgeError):env.bridge.activate(claimed['id'],claimed['worker_id'],claimed['claim_token'])
    sid=str(uuid.uuid4())
    late=env.bridge.report(claimed['id'],claimed['worker_id'],claimed['claim_token'],status='submitted',submit_id=sid)
    assert late['id']==claimed['id'] and late['submit_id']==sid and len(env.bridge.list_jobs())==1


def test_known_id_generation_timeout_releases_foreground_slot_but_retains_original_receipt(tmp_path):
    env=fixture(tmp_path);claimed=claim(env);activate(env,claimed);sid=str(uuid.uuid4())
    env.bridge.report(claimed['id'],claimed['worker_id'],claimed['claim_token'],status='submitted',submit_id=sid)
    first=env.bridge.deadline(claimed['id'],'generation_timeout','Generation wait exceeded')
    assert first['status']=='uncertain' and first['submit_id']==sid and first['slot_released']
    env.clock.now+=10
    again=env.bridge.deadline(claimed['id'],'generation_timeout','Generation wait exceeded')
    assert again['updated_at']==first['updated_at']
    result=env.bridge.reconcile_submitted(claimed['id'],{'id':sid,'status':'completed','videos':[{'url':'https://example.test/fixture.mp4'}]})
    assert result['status']=='completed' and result['error'] is None and result['error_code'] is None
    assert len(env.bridge.list_jobs())==1


def test_public_unknown_outcome_returns_explicit_error_instead_of_endless_progress():
    result=video_object(task(status='uncertain',error={'code':'submission_unconfirmed','message':'Review original task'}))
    assert result['status']=='failed' and result['error']['code']=='submission_unconfirmed'
    assert result['metadata']['verification_required'] is True
    assert result.get('progress')!=100


@pytest.mark.parametrize('code',['generation_timeout','task_timeout'])
def test_public_known_id_timeout_reports_pending_original_and_can_later_complete(code):
    value=task(status='uncertain',submit_id='known-id',error={'code':code,'message':'Wait exceeded'},progress=99,progress_details={'stage':'generating','source':'page'})
    response=video_object(value)
    assert response['status']=='failed' and response['error']['code']==code
    assert response['metadata']['original_task_pending'] is True
    assert response.get('progress')!=100
    late=video_object({**value,'status':'completed','error':None,'completed_at':1800.})
    assert late['status']=='completed' and late['progress']==100 and 'error' not in late
    assert not late.get('metadata',{}).get('original_task_pending')


class RuntimeClock:
    def __init__(self):self.now=1000.
    def __call__(self):return self.now


def runtime_fixture(tmp_path,monkeypatch,*,heartbeat_timeout=10000,total=7200):
    clock=RuntimeClock();bridge=BridgeStore(tmp_path/'runtime-bridge',clock=clock,heartbeat_timeout=heartbeat_timeout,claim_timeout=10000)
    store=PlatformStore('sqlite:///:memory:',encryption_key=Fernet.generate_key(),clock=clock)
    store.create_account({'id':'one','name':'Synthetic deadline account','session_id':'synthetic-only','capabilities':[MODEL],'max_concurrent':2})
    raw=store.create_key({'name':'Synthetic caller','max_concurrent':10});principal=store.authenticate_key(raw['key'])
    value=store.reserve_task(principal,'video',MODEL,dict(SPEC,references=[]),idempotency_key='one-original-task')
    queued=bridge.enqueue(dict(SPEC,references=[]),account_id='one')
    value=store.update_task(value['id'],bridge_job_id=queued['id'])
    bridge.heartbeat('one','runtime-slot',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',driver='native',protocol_version=5,slot_index=1,slot_count=2,owner_id='runtime-browser')
    calls=[];outcomes=[]
    class Client:
        api_base='https://dreamina-api.us.capcut.com'
        def __init__(self,*_):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def credits(self):return {'credit':100}
        async def request(self,path,body):
            assert path==HISTORY_PATH,'Only read the original history; never regenerate'
            calls.append(deepcopy(body))
            assert outcomes,'Unexpected extra query'
            value=outcomes.pop(0)
            if isinstance(value,Exception):raise value
            return deepcopy(value)
    monkeypatch.setattr(runtime_module,'DreaminaClient',Client)
    runtime=PlatformRuntime(store,bridge,clock=clock,limits=TaskLimits(total=total))
    return SimpleNamespace(clock=clock,bridge=bridge,store=store,task=value,runtime=runtime,calls=calls,outcomes=outcomes)


async def approved_runtime_claim(env,*,activated=False,submitted=False):
    claimed=env.bridge.claim('one','runtime-slot',capacity=2,request_id=str(uuid.uuid4()));assert claimed
    env.store.update_task(env.task['id'],status='claimed')
    quote={'required_credits':20,'source':'native_page','observed_at':env.clock(),'model':MODEL,'request_fingerprint':quote_request_fingerprint(claimed['request'])}
    proof=await env.runtime.credit_preflight(claimed,claimed['worker_id'],claimed['claim_token'],quote)
    assert proof['allowed'] and env.store.get_account('one')['reserved_credits']==20
    if activated:env.bridge.activate(claimed['id'],claimed['worker_id'],claimed['claim_token'])
    if submitted:
        sid=str(uuid.uuid4())
        env.bridge.report(claimed['id'],claimed['worker_id'],claimed['claim_token'],status='submitted',submit_id=sid)
        return claimed,sid
    return claimed,None


def history(sid,*,completed=False):
    value={'submit_id':sid,'created_time':1001.,'workspace_id':'123456','status':50 if completed else 20,
           'draft_content':build_generation(dict(SPEC,references=[]),workspace_id='123456')['body']['draft_content'],'item_list':[]}
    if completed:value['item_list']=[{'video':{'duration_ms':4000,'transcoded_video':{'origin':{'video_url':'https://media.example.test/original.mp4','width':1280,'height':720,'format':'mp4'}}}}]
    return {sid:value}


def test_runtime_queue_deadline_fails_fast_without_history_or_generation(tmp_path,monkeypatch):
    async def run():
        env=runtime_fixture(tmp_path,monkeypatch);env.clock.now=1060
        value=await env.runtime.sync_task(env.task)
        assert value['status']=='failed' and value['error']['code']=='queue_timeout'
        assert env.bridge.get(env.task['bridge_job_id'])['status']=='failed' and not env.calls
        assert len(env.store.list_tasks())==len(env.bridge.list_jobs())==1
    asyncio.run(run())


def test_runtime_unactivated_preparation_timeout_releases_only_safe_credit_hold(tmp_path,monkeypatch):
    async def run():
        env=runtime_fixture(tmp_path,monkeypatch);await approved_runtime_claim(env)
        env.clock.now=1180;value=await env.runtime.sync_task(env.task)
        assert value['status']=='failed' and value['error']['code']=='preparation_timeout'
        assert env.store.get_account('one')['reserved_credits']==0 and not env.calls
    asyncio.run(run())


def test_runtime_unknown_submission_timeout_retains_hold_and_returns_explicit_public_error(tmp_path,monkeypatch):
    async def run():
        env=runtime_fixture(tmp_path,monkeypatch);await approved_runtime_claim(env,activated=True)
        env.clock.now=1060;value=await env.runtime.sync_task(env.task)
        assert value['status']=='uncertain' and value['error']['code']=='submission_unconfirmed'
        assert env.store.get_account('one')['reserved_credits']==20 and not env.calls
        public=video_object(value)
        assert public['status']=='failed' and public['metadata']['verification_required']
        assert len(env.bridge.list_jobs())==1
    asyncio.run(run())


def test_runtime_known_id_timeout_retains_hold_then_late_completion_resolves_original_task(tmp_path,monkeypatch):
    async def run():
        env=runtime_fixture(tmp_path,monkeypatch);_,sid=await approved_runtime_claim(env,activated=True,submitted=True)
        env.outcomes.extend([history(sid),history(sid,completed=True)])
        env.clock.now=1600;value=await env.runtime.sync_task(env.task)
        assert value['status']=='uncertain' and value['error']['code']=='generation_timeout'
        assert value['submit_id']==sid and env.store.get_account('one')['reserved_credits']==20
        public=video_object(value);assert public['status']=='failed' and public['metadata']['original_task_pending']
        env.clock.now+=max(5,env.runtime.poll_interval);late=await env.runtime.sync_task(env.task)
        assert late['status']=='completed' and late['id']==env.task['id'] and late['submit_id']==sid
        assert env.store.get_account('one')['reserved_credits']==0
        assert all(c=={'submit_ids':[sid]} for c in env.calls) and len(env.bridge.list_jobs())==1
    asyncio.run(run())


def test_runtime_lost_v5_worker_before_activation_returns_connection_error_and_releases_hold(tmp_path,monkeypatch):
    async def run():
        env=runtime_fixture(tmp_path,monkeypatch,heartbeat_timeout=90);await approved_runtime_claim(env)
        env.clock.now=1091;env.bridge.expire_claims();value=await env.runtime.sync_task(env.task)
        assert value['status']=='failed' and value['error']['code']=='worker_connection_lost'
        assert env.store.get_account('one')['reserved_credits']==0 and not env.calls
    asyncio.run(run())


def test_expired_known_task_does_not_wait_forever_for_a_query_slot(tmp_path,monkeypatch):
    async def run():
        env=runtime_fixture(tmp_path,monkeypatch);_,sid=await approved_runtime_claim(env,activated=True,submitted=True)
        env.clock.now=1600;env.runtime._query_slots=asyncio.Semaphore(0)
        value=await asyncio.wait_for(env.runtime.sync_task(env.task),timeout=.5)
        assert value['status']=='uncertain' and value['submit_id']==sid
        assert value['error']['code']=='generation_timeout' and not env.calls
    asyncio.run(run())
