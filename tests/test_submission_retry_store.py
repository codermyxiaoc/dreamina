"""Transactional retry state tests; no browser, network, or paid generation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
import pytest

from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.platform_store import PlatformStore,PlatformError
from dreamina_api.upstream_errors import normalize_upstream_error

MODEL='seedance-2.0-mini'
SPEC={'model':MODEL,'prompt':'Synthetic ceramic bird','duration':4,'seconds':'4','resolution':'720p','ratio':'16:9','mode':'omni','references':[]}

def fixture(tmp_path,*,price=20,concurrency=1):
    clock=SimpleNamespace(now=1800000000.0);encryption=Fernet.generate_key();url='sqlite:///'+str(tmp_path/'platform.sqlite3')
    store=PlatformStore(url,encryption_key=encryption,clock=lambda:clock.now)
    for aid in ('a','b'):store.create_account({'id':aid,'name':aid,'session_id':'synthetic-'+aid,'max_concurrent':concurrency,'max_inflight':concurrency,'capabilities':[MODEL]})
    key=store.create_key({'name':'test','request_limit':1,'max_concurrent':concurrency,'account_ids':['a']})
    principal=store.authenticate_key(key['key']);task=store.reserve_task(principal,'video',MODEL,deepcopy(SPEC),idempotency_key='same-public-task')
    jid=str(uuid.uuid4());task=store.update_task(task['id'],status='claimed',bridge_job_id=jid,conversation_id='first-conversation',request_detail={'preserved':'original'})
    request=deepcopy(SPEC);request.update(new_session=True,asset_owner_task_id=task['id']);fingerprint=quote_request_fingerprint(request)
    proof=store.reserve_task_credits(task['id'],jid,required_credits=price,balance=100,request_fingerprint=fingerprint)
    proof.update(source='native_page',request_fingerprint=fingerprint)
    job={'id':jid,'account_id':'a','request':request,'status':'failed','error_code':'upstream_rejected','error':'Synthetic busy rejection',
         'submit_id':str(uuid.uuid4()),'confirmed_at':None,'activation_started_at':clock.now,'created_at':clock.now,
         'claim_protocol_version':5,'credit_preflight':proof,
         'upstream_error':normalize_upstream_error({'ret':'server_busy','task_accepted':False,'errmsg':'Server busy'},200)}
    return SimpleNamespace(store=store,clock=clock,url=url,encryption=encryption,key=key,principal=principal,task=task,job=job)

def schedule(env,**kwargs):return env.store.schedule_submission_retry(env.task['id'],env.job,delay_seconds=10,**kwargs)

def test_one_retry_preserves_parent_quota_deadline_and_reference_owner(tmp_path):
    e=fixture(tmp_path);first=schedule(e)
    assert first['id']==e.task['id'] and first['bridge_job_id']!=e.job['id']
    assert first['status']=='queued' and first['submit_id'] is None and first['conversation_id'] is None
    assert first['created_at']==e.task['created_at'] and first['dispatch_claimed'] is True
    detail=first['request_detail'];assert detail['preserved']=='original' and detail['submission_retry_pending']
    assert detail['submission_retry']['attempt']==2 and detail['submission_retry']['quote_ceiling']==20
    assert detail['submission_attempts'][0]['submit_id']==e.job['submit_id']
    assert e.store.list_keys()[0]['used_requests']==1 and not e.store.active_credit_holds()
    repeated=e.store.reserve_task(e.principal,'video',MODEL,deepcopy(SPEC),idempotency_key='same-public-task')
    assert repeated['id']==first['id'] and repeated['_created'] is False
    assert schedule(e)['bridge_job_id']==first['bridge_job_id']
    assert e.store.validate_submission_retry(first['id'])['status']=='queued'
    assert e.store.validate_task_activation(first['id'],first['bridge_job_id'])['id']==first['id']

def test_simultaneous_processes_schedule_one_deterministic_attempt(tmp_path):
    e=fixture(tmp_path)
    other=PlatformStore(e.url,encryption_key=e.encryption,clock=lambda:e.clock.now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        out=list(pool.map(lambda store:store.schedule_submission_retry(e.task['id'],e.job,delay_seconds=10),(e.store,other)))
    assert out[0]['bridge_job_id']==out[1]['bridge_job_id']
    assert len(e.store.get_task(e.task['id'])['request_detail']['submission_attempts'])==1
    assert sum(x['stage']=='submission_retry_scheduled' for x in e.store.list_events(e.task['id']))==1

@pytest.mark.parametrize('field,value',[
    ('claim_protocol_version',4),('claim_protocol_version',None),('confirmed_at',1800000000),
    ('activation_started_at',None),('status','uncertain'),('account_id','b'),('submit_id',None),
    ('upstream_error',None),('upstream_error',{'retryable':True}),
])
def test_invalid_submission_proof_never_releases_hold_or_changes_attempt(tmp_path,field,value):
    e=fixture(tmp_path);e.job[field]=value
    with pytest.raises(PlatformError,match='safe proof'):schedule(e)
    assert e.store.get_task(e.task['id'])['bridge_job_id']==e.task['bridge_job_id']
    assert len(e.store.active_credit_holds())==1

@pytest.mark.parametrize('ret,message',[('-6','shark not pass reject'),('2052','ErrMessage_APP_OutputAudioRisk'),('server_busy','session expired')])
def test_risk_policy_account_rejections_are_never_resubmitted(tmp_path,ret,message):
    e=fixture(tmp_path);e.job['upstream_error']=normalize_upstream_error({'ret':ret,'errmsg':message,'task_accepted':False},200)
    with pytest.raises(PlatformError):schedule(e)
    assert e.store.get_task(e.task['id'])['status']=='claimed'

@pytest.mark.parametrize('status',['uncertain','submitted','in_progress'])
def test_unknown_or_accepted_parent_cannot_retry(tmp_path,status):
    e=fixture(tmp_path);e.store.update_task(e.task['id'],status=status)
    with pytest.raises(PlatformError):schedule(e)

@pytest.mark.parametrize('change,code',[
    ('revoked','api_key_revoked'),('deleted','api_key_revoked'),('expired','api_key_revoked'),
    ('scope','account_not_allowed'),('model','model_not_allowed'),('account_disabled','account_unavailable'),('unsupported','model_not_allowed')])
def test_current_permissions_are_rechecked_before_retry_dispatch(tmp_path,change,code):
    e=fixture(tmp_path);first=schedule(e)
    if change=='revoked':e.store.update_key(e.key['item']['id'],{'enabled':False})
    elif change=='deleted':e.store.delete_key(e.key['item']['id'])
    elif change=='expired':e.store.update_key(e.key['item']['id'],{'expires_at':e.clock.now-1})
    elif change=='scope':e.store.update_key(e.key['item']['id'],{'account_ids':['b']})
    elif change=='model':e.store.update_key(e.key['item']['id'],{'allowed_models':['seedance-2.5']})
    elif change=='account_disabled':e.store.update_account('a',{'enabled':False})
    elif change=='unsupported':e.store.update_account('a',{'capabilities':[]})
    current=e.store.validate_submission_retry(first['id'])
    assert current['status']=='failed' and current['error']['code']==code
    assert current['request_detail']['submission_retry']['stage']=='exhausted'
    assert not current['request_detail'].get('submission_retry_pending')

def test_reduced_account_limit_respects_other_existing_parent(tmp_path):
    e=fixture(tmp_path,concurrency=2);first=schedule(e)
    otherkey=e.store.create_key({'name':'other','account_ids':['a']});principal=e.store.authenticate_key(otherkey['key'])
    e.store.reserve_task(principal,'video',MODEL,{'prompt':'other'},account_id='a')
    e.store.update_account('a',{'max_inflight':1})
    current=e.store.validate_submission_retry(first['id'])
    assert current['status']=='failed' and current['error']['code']=='retry_capacity_unavailable'

def test_retry_cannot_reset_original_deadline(tmp_path):
    e=fixture(tmp_path);first=schedule(e);e.clock.now+=901
    current=e.store.validate_submission_retry(first['id'])
    assert current['status']=='failed' and current['error']['code']=='task_timeout'
    assert current['created_at']==e.task['created_at']

def test_insufficient_remaining_total_time_prevents_scheduling(tmp_path):
    e=fixture(tmp_path);e.clock.now+=895;current=schedule(e)
    assert current['status']=='failed' and current['error']['code']=='task_timeout'
    assert current['bridge_job_id']==e.job['id'] and not e.store.active_credit_holds()

def test_dispatch_ack_recovery_is_idempotent_and_final_attempt_is_archived_once(tmp_path):
    e=fixture(tmp_path);first=schedule(e);jid=first['bridge_job_id']
    e.clock.now+=10
    ready=e.store.finish_submission_retry_dispatch(first['id'],jid)
    assert ready['request_detail']['submission_retry']['stage']=='preparing'
    assert not ready['request_detail'].get('submission_retry_pending')
    assert e.store.finish_submission_retry_dispatch(first['id'],jid)==ready
    e.store.update_task(first['id'],status='claimed',expected_bridge_job_id=jid)
    sid=str(uuid.uuid4());current=e.store.update_task(first['id'],status='completed',submit_id=sid,expected_bridge_job_id=jid)
    assert current['request_detail']['submission_retry']['stage']=='completed'
    assert current['request_detail']['submission_attempts'][-1]['submit_id']==sid
    again=e.store.update_task(first['id'],status='completed',expected_bridge_job_id=jid)
    assert len(again['request_detail']['submission_attempts'])==2

def test_second_explicit_rejection_stops_instead_of_third_attempt(tmp_path):
    e=fixture(tmp_path);first=schedule(e);jid=first['bridge_job_id'];e.clock.now+=10
    e.store.finish_submission_retry_dispatch(first['id'],jid);e.store.update_task(first['id'],status='claimed')
    second=deepcopy(e.job);second.update(id=jid,submit_id=str(uuid.uuid4()),created_at=e.clock.now)
    e.store.reserve_task_credits(first['id'],jid,required_credits=20,balance=100,request_fingerprint=second['credit_preflight']['request_fingerprint'])
    final=e.store.schedule_submission_retry(first['id'],second,delay_seconds=10)
    assert final['status']=='failed' and final['error']['code']=='submission_retry_exhausted'
    assert final['bridge_job_id']==jid and len(final['request_detail']['submission_attempts'])==2
    assert e.store.list_keys()[0]['used_requests']==1 and not e.store.active_credit_holds()

def test_zero_price_stays_a_zero_credit_retry_ceiling(tmp_path):
    e=fixture(tmp_path,price=0);current=schedule(e)
    assert current['request_detail']['submission_retry']['quote_ceiling']==0

def test_late_old_task_update_and_observation_cannot_corrupt_new_attempt(tmp_path):
    e=fixture(tmp_path);current=schedule(e);old=e.job['id']
    returned=e.store.update_task(current['id'],expected_bridge_job_id=old,status='completed',submit_id=e.job['submit_id'])
    assert returned==current
    assert e.store.append_event(current['id'],'late',{'old':True},expected_bridge_job_id=old) is None
    assert e.store.record_bridge_observation(old,{'stage':'generating','observed_at':e.clock.now,'progress':99,'conversation_id':'old'}) is None
    assert e.store.get_task(current['id'])==current
    observed=e.store.record_bridge_observation(current['bridge_job_id'],{'stage':'preparing','observed_at':e.clock.now,'progress':0,'conversation_id':'new'})
    assert observed['conversation_id']=='new' and observed['progress']==0

def test_revocation_after_dispatch_still_blocks_activation(tmp_path):
    e=fixture(tmp_path);current=schedule(e);jid=current['bridge_job_id']
    e.store.finish_submission_retry_dispatch(current['id'],jid)
    e.store.update_key(e.key['item']['id'],{'enabled':False})
    with pytest.raises(PlatformError) as error:e.store.validate_task_activation(current['id'],jid)
    assert error.value.code=='api_key_revoked'
