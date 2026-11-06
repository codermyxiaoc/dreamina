"""Offline integration of native rejection evidence, durable retries and budgets."""
import asyncio
import copy
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
import pytest

from dreamina_api.browser_driver import generation_response
from dreamina_api.chrome_bridge import BridgeError, BridgeStore
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.openai_api import video_object
from dreamina_api.platform_runtime import PlatformRuntime
from dreamina_api.platform_store import PlatformError, PlatformStore
from dreamina_api.task_limits import TaskLimits
from dreamina_api.upstream_errors import normalize_upstream_error
import dreamina_api.platform_runtime as runtime_module


PAGE='https://dreamina.capcut.com/ai-tool/generate'
ORIGIN='https://dreamina-api.us.capcut.com'
BUSY={'ret':'server_busy','task_accepted':False,'errmsg':'server busy'}


class NoNetworkClient:
    def __init__(self,*args,**kwargs):
        raise AssertionError('This test must not use an upstream network client')


class RetryFixture:
    def __init__(self,tmp_path,monkeypatch,*,amount=20,max_attempts=2):
        monkeypatch.setattr(runtime_module,'DreaminaClient',NoNetworkClient)
        self.clock=SimpleNamespace(now=1_800_000_000.0)
        self.bridge=BridgeStore(tmp_path/'bridge',clock=lambda:self.clock.now)
        self.store=PlatformStore('sqlite:///:memory:',encryption_key=Fernet.generate_key(),
                                 clock=lambda:self.clock.now,worker_status=self.bridge.accounts)
        self.store.create_account({'id':'one','name':'Synthetic account','region':'US','session_id':'synthetic',
                                   'capabilities':['seedance-2.0-mini'],'max_concurrent':1})
        self.store.record_account_check('one',status='ready',balance=100)
        self.ready()
        self.key=self.store.create_key({'name':'Synthetic retry quota','request_limit':1,'max_concurrent':1,
                                        'account_ids':['one'],'allowed_models':['seedance-2.0-mini']})
        self.principal=self.store.authenticate_key(self.key['key'])
        self.request={'model':'seedance-2.0-mini','prompt':'Synthetic ceramic bird','duration':4,
                      'seconds':'4','resolution':'720p','ratio':'16:9','size':'1280x720','mode':'omni','references':[]}
        self.task=self.store.reserve_task(self.principal,'video','seedance-2.0-mini',self.request,idempotency_key='same-logical-request')
        self.runtime=self.runtime_instance(max_attempts=max_attempts)
        job=self.bridge.enqueue(self.runtime._dispatch_request(self.task),account_id='one')
        self.store.update_task(self.task['id'],bridge_job_id=job['id'],status='claimed',conversation_id='session-original')
        self.original=self.claim()
        self.amount=amount
        self.balance_checks=0
        asyncio.run(self.approve_and_activate(self.original,amount))

    def ready(self):
        self.bridge.heartbeat('one','one-slot-1',ready=True,page_url=PAGE,protocol_version=5,
                              driver='native',slot_index=1,slot_count=1,owner_id='one-owner')

    def runtime_instance(self,*,max_attempts=2):
        runtime=PlatformRuntime(self.store,self.bridge,clock=lambda:self.clock.now,submission_attempts=max_attempts,
                                retry_delay=lambda:10,limits=TaskLimits())
        async def check(*args):
            self.balance_checks+=1
            return {'status':'ready','balance':100}
        runtime.check_account=check
        return runtime

    def claim(self):
        claim=self.bridge.claim('one','one-slot-1',capacity=1,request_id=str(uuid.uuid4()))
        assert claim is not None
        return claim

    def quote(self,job,amount):
        return {'source':'native_page','model':job['request']['model'],'required_credits':amount,
                'observed_at':self.clock.now,'request_fingerprint':quote_request_fingerprint(job['request'])}

    async def approve_and_activate(self,job,amount):
        approved=await self.runtime.credit_preflight(job,job['worker_id'],job['claim_token'],self.quote(job,amount))
        assert approved['allowed'] is True
        activated=await self.runtime.activate_task(job,job['worker_id'],job['claim_token'])
        assert activated['activation_started_at']==self.clock.now

    def reject(self,job=None,data=None):
        job=job or self.original;data=BUSY if data is None else data
        sid=str(uuid.uuid4())
        receipt=generation_response({'submit_id':sid,'api_origin':ORIGIN,'conversation_id':'session-original'},200,data)
        assert receipt['status']=='failed'
        result=self.bridge.report(job['id'],job['worker_id'],job['claim_token'],status='failed',submit_id=sid,
                                  api_origin=ORIGIN,error=receipt['error'],error_code='upstream_rejected',
                                  upstream_ret=receipt['ret'],upstream_error=receipt['upstream_error'])
        assert result['confirmed_at'] is None
        return result

    async def schedule(self):
        self.reject()
        return await self.runtime.sync_task(self.task)

    async def dispatch(self,scheduled):
        self.clock.now=scheduled['request_detail']['submission_retry']['next_retry_at']
        current=await self.runtime.sync_task(self.task)
        assert current['request_detail']['submission_retry']['stage']=='preparing'
        return current,self.claim()


def test_explicit_transient_rejection_retries_same_public_id_after_delay_once(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    async def scenario():
        scheduled=await f.schedule();new_id=scheduled['bridge_job_id'];original_id=f.original['id']
        assert scheduled['id']==f.task['id'] and scheduled['status']=='queued'
        assert new_id!=original_id and scheduled['submit_id'] is None
        assert scheduled['created_at']==f.task['created_at']
        detail=scheduled['request_detail'];retry=detail['submission_retry'];archived=detail['submission_attempts']
        assert retry['attempt']==retry['max_attempts']==2 and retry['quote_ceiling']==20
        assert retry['stage']=='waiting' and retry['next_retry_at']==f.clock.now+10
        assert len(archived)==1 and archived[0]['bridge_job_id']==original_id and archived[0]['submit_id']
        assert archived[0]['conversation_id']=='session-original'
        assert not f.store.active_credit_holds('one')
        assert [j['id'] for j in f.bridge.list_jobs()]==[original_id]
        await f.runtime.sync_task(f.task)
        assert len(f.bridge.list_jobs())==1
        f.clock.now+=9.9
        await f.runtime.sync_task(f.task)
        assert len(f.bridge.list_jobs())==1
        current,claim=await f.dispatch(scheduled)
        assert current['bridge_job_id']==claim['id']==new_id
        assert claim['claim_token']!=f.original['claim_token']
        assert claim['request']['new_session'] is True and claim['request']['asset_owner_task_id']==f.task['id']
        assert len(f.bridge.list_jobs())==2 and f.store.list_keys()[0]['used_requests']==1
        same=f.store.reserve_task(f.principal,'video','seedance-2.0-mini',f.request,idempotency_key='same-logical-request')
        assert same['id']==f.task['id']
        public=video_object(current)
        assert public['id']==f.task['id'] and public['status']=='queued'
        assert set(public['metadata']['submission_retry'])=={'attempt','max_attempts','stage','next_retry_at','reason'}
        assert 'submission_attempts' not in public['metadata'] and 'quote_ceiling' not in public['metadata']['submission_retry']
    asyncio.run(scenario())


def test_retry_dispatch_recovers_crash_after_enqueue_without_another_job(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    async def scenario():
        scheduled=await f.schedule();next_id=scheduled['bridge_job_id']
        f.clock.now=scheduled['request_detail']['submission_retry']['next_retry_at']
        finish=f.store.finish_submission_retry_dispatch
        def crash(*args):raise RuntimeError('Synthetic crash after durable enqueue')
        monkeypatch.setattr(f.store,'finish_submission_retry_dispatch',crash)
        with pytest.raises(RuntimeError,match='Synthetic crash'):await f.runtime.sync_task(f.task)
        assert f.bridge.get(next_id)['status']=='queued'
        assert f.store.get_task(f.task['id'])['request_detail']['submission_retry_pending'] is True
        monkeypatch.setattr(f.store,'finish_submission_retry_dispatch',finish)
        restarted=f.runtime_instance()
        current=await restarted.sync_task(f.task)
        assert current['bridge_job_id']==next_id and current['request_detail']['submission_retry']['stage']=='preparing'
        assert len(f.bridge.list_jobs())==2
        for _ in range(3):await restarted.sync_task(f.task)
        assert len(f.bridge.list_jobs())==2 and f.store.list_keys()[0]['used_requests']==1
    asyncio.run(scenario())


@pytest.mark.parametrize('original_amount,new_amount,allowed',[(20,20,True),(20,10,True),(0,0,True),(20,21,False),(0,20,False)])
def test_retry_gets_fresh_balance_and_never_exceeds_original_quote(tmp_path,monkeypatch,original_amount,new_amount,allowed):
    f=RetryFixture(tmp_path,monkeypatch,amount=original_amount)
    async def scenario():
        scheduled=await f.schedule();_,claim=await f.dispatch(scheduled)
        prior_checks=f.balance_checks
        if allowed:
            await f.approve_and_activate(claim,new_amount)
            assert f.balance_checks==prior_checks+1
            holds=f.store.active_credit_holds('one')
            assert len(holds)==1 and holds[0]['amount']==new_amount and holds[0]['job_id']==claim['id']
        else:
            with pytest.raises(BridgeError,match='original approved credit budget'):
                await f.runtime.credit_preflight(claim,claim['worker_id'],claim['claim_token'],f.quote(claim,new_amount))
            assert f.balance_checks==prior_checks
            assert f.bridge.get(claim['id'])['activation_started_at'] is None
            assert not f.store.active_credit_holds('one')
    asyncio.run(scenario())


def test_revoking_key_after_retry_preflight_prevents_activation(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    async def scenario():
        scheduled=await f.schedule();_,claim=await f.dispatch(scheduled)
        approved=await f.runtime.credit_preflight(claim,claim['worker_id'],claim['claim_token'],f.quote(claim,20))
        assert approved['allowed']
        f.store.update_key(f.key['item']['id'],{'enabled':False})
        rejected=await f.runtime.activate_task(claim,claim['worker_id'],claim['claim_token'])
        assert rejected['can_activate'] is False and rejected['generation_not_activated'] is True
        assert rejected['job_id']==claim['id'] and rejected['error_code']=='api_key_revoked'
        stopped=f.bridge.get(claim['id'])
        assert stopped['activation_started_at'] is None and stopped['status']=='failed'
        finished=await f.runtime.sync_task(f.task)
        assert finished['status']=='failed' and finished['error']['code']=='api_key_revoked'
        assert not f.store.active_credit_holds('one')
    asyncio.run(scenario())


def test_revocation_after_activation_never_claims_the_generation_was_not_activated(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    f.store.update_key(f.key['item']['id'],{'enabled':False})
    with pytest.raises((BridgeError,PlatformError)):
        asyncio.run(f.runtime.activate_task(f.original,f.original['worker_id'],f.original['claim_token']))
    original=f.bridge.get(f.original['id'])
    assert original['activation_started_at'] is not None and original['status']!='failed'
    assert len(f.store.active_credit_holds('one'))==1


@pytest.mark.parametrize('data',[
    {'ret':-6,'task_accepted':False,'errmsg':'server busy'},
    {'ret':-6,'errmsg':'shark not pass reject'},
    {'ret':'server_busy','errmsg':'server busy'},
    {'ret':'server_busy','task_accepted':False,'errmsg':'captcha required'},
    {'ret':2052,'task_accepted':False,'errmsg':'ErrMessage_APP_OutputAudioRisk'},
    {'ret':'insufficient_credits','task_accepted':False,'errmsg':'insufficient credits'},
])
def test_rejections_without_safe_proof_never_create_a_second_job(tmp_path,monkeypatch,data):
    f=RetryFixture(tmp_path,monkeypatch);f.reject(data=data)
    result=asyncio.run(f.runtime.sync_task(f.task))
    assert result['status']=='failed' and result['bridge_job_id']==f.original['id']
    assert result['error']['code']=='upstream_rejected' and result['error']['upstream_error']['retryable'] is False
    assert 'submission_retry' not in (result.get('request_detail') or {})
    assert len(f.bridge.list_jobs())==1


def test_unknown_submission_without_receipt_never_creates_new_session(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    f.bridge.report(f.original['id'],f.original['worker_id'],f.original['claim_token'],status='uncertain',
                    error='Synthetic response lost after click',error_code='submission_unconfirmed')
    result=asyncio.run(f.runtime.sync_task(f.task))
    assert result['status']=='uncertain' and result['bridge_job_id']==f.original['id']
    assert len(f.bridge.list_jobs())==1 and len(f.store.active_credit_holds('one'))==1
    assert video_object(result)['metadata']['verification_required'] is True


def test_known_id_with_unknown_outcome_only_queries_the_original_submission(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch);sid=str(uuid.uuid4());queries=[]
    f.bridge.report(f.original['id'],f.original['worker_id'],f.original['claim_token'],status='uncertain',submit_id=sid,
                    api_origin=ORIGIN,error='Synthetic response lost',error_code='submission_unconfirmed')
    f.store.update_task(f.task['id'],status='uncertain',submit_id=sid,
                        error={'code':'submission_unconfirmed','message':'Synthetic response lost'})
    class MissingHistory:
        def __init__(self,*args):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,path,payload):queries.append((path,payload));return {}
    monkeypatch.setattr(runtime_module,'DreaminaClient',MissingHistory)
    result=asyncio.run(f.runtime.sync_task(f.task))
    assert result['status']=='uncertain' and result['submit_id']==sid
    assert queries==[(runtime_module.HISTORY_PATH,{'submit_ids':[sid]})]
    assert len(f.bridge.list_jobs())==1 and len(f.store.active_credit_holds('one'))==1
    assert video_object(result)['metadata']['original_task_pending'] is True


def test_accepted_task_with_final_audio_policy_failure_is_not_regenerated(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch);sid=str(uuid.uuid4());queries=[]
    f.bridge.report(f.original['id'],f.original['worker_id'],f.original['claim_token'],status='submitted',submit_id=sid,api_origin=ORIGIN)
    class FailedHistory:
        def __init__(self,*args):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,path,payload):queries.append((path,payload));return {sid:{'synthetic_record':True}}
    monkeypatch.setattr(runtime_module,'DreaminaClient',FailedHistory)
    monkeypatch.setattr(runtime_module,'verify_record',lambda job,record:None)
    monkeypatch.setattr(runtime_module,'normalize_record',lambda record,submit_id:{'id':submit_id,'status':'failed',
        'upstream_status':30,'history_record_id':'synthetic-history','videos':[],
        'failure':{'code':'2052','message':'ErrMessage_APP_OutputAudioRisk'}})
    result=asyncio.run(f.runtime.sync_task(f.task))
    assert result['status']=='failed' and result['error']['code']=='2052' and result['submit_id']==sid
    assert queries==[(runtime_module.HISTORY_PATH,{'submit_ids':[sid]})]
    assert len(f.bridge.list_jobs())==1 and not f.store.active_credit_holds('one')
    assert not (result.get('request_detail') or {}).get('submission_retry')


def test_submission_retry_can_be_disabled_without_replaying_the_rejection(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch,max_attempts=1)
    result=asyncio.run(f.schedule())
    assert result['status']=='failed' and result['error']['code']=='submission_retry_exhausted'
    assert result['request_detail']['submission_retry']['max_attempts']==1
    assert len(f.bridge.list_jobs())==1 and f.store.list_keys()[0]['used_requests']==1


def test_second_explicit_rejection_exhausts_without_a_third_generation(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    async def scenario():
        scheduled=await f.schedule();_,claim=await f.dispatch(scheduled)
        await f.approve_and_activate(claim,20);f.reject(job=claim)
        result=await f.runtime.sync_task(f.task)
        assert result['status']=='failed' and result['error']['code']=='submission_retry_exhausted'
        assert result['request_detail']['submission_retry']['stage']=='exhausted'
        assert len(result['request_detail']['submission_attempts'])==2
        assert not f.store.active_credit_holds('one')
        f.clock.now+=20
        again=await f.runtime.sync_task(f.task)
        assert again['id']==f.task['id'] and len(f.bridge.list_jobs())==2
        assert f.store.list_keys()[0]['used_requests']==1
    asyncio.run(scenario())


def test_original_total_deadline_is_not_reset_by_scheduled_retry(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    async def scenario():
        scheduled=await f.schedule()
        f.clock.now=f.task['created_at']+901;f.ready()
        result=await f.runtime.sync_task(scheduled)
        assert result['status']=='failed' and result['error']['code']=='task_timeout'
        assert result['created_at']==f.task['created_at'] and len(f.bridge.list_jobs())==1
        assert result['request_detail']['submission_retry']['next_retry_at'] is None
    asyncio.run(scenario())


def test_bridge_receipt_is_sanitized_and_cannot_be_replaced_after_rejection(tmp_path,monkeypatch):
    f=RetryFixture(tmp_path,monkeypatch)
    diagnostic=normalize_upstream_error(dict(BUSY,errmsg='server busy; Bearer private-secret; cookie=session-secret'),200)
    diagnostic.update(cookie='never-store',authorization='never-store')
    sid=str(uuid.uuid4())
    kwargs={'status':'failed','submit_id':sid,'error_code':'upstream_rejected','upstream_ret':'server_busy',
            'upstream_error':diagnostic,'error':'Synthetic rejection'}
    stored=f.bridge.report(f.original['id'],f.original['worker_id'],f.original['claim_token'],**kwargs)
    assert stored['upstream_error']['message']=='server busy'
    assert 'cookie' not in stored['upstream_error'] and 'authorization' not in stored['upstream_error']
    changed=copy.deepcopy(diagnostic);changed['ret']='service_unavailable';changed['error_code']='service_unavailable'
    with pytest.raises(BridgeError,match='original upstream diagnostic'):
        f.bridge.report(f.original['id'],f.original['worker_id'],f.original['claim_token'],**dict(kwargs,upstream_ret='service_unavailable',upstream_error=changed))
    assert f.bridge.get(f.original['id'])['upstream_error']==stored['upstream_error']
