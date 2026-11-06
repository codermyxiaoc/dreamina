"""Central negative activation proof is atomic and never reverses activation."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import pytest

from dreamina_api.chrome_bridge import BridgeStore,BridgeError
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.upstream_errors import normalize_upstream_error

def fixture(tmp_path,version=5):
    clock=SimpleNamespace(now=1000.0)
    store=BridgeStore(tmp_path/'bridge',clock=lambda:clock.now,claim_timeout=600,heartbeat_timeout=300)
    kwargs={'slot_index':1,'slot_count':1,'owner_id':'owner'} if version>=5 else {}
    store.heartbeat('a','worker-a',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',protocol_version=version,driver='native',**kwargs)
    store.enqueue({'prompt':'Synthetic activation test'},account_id='a')
    job=store.claim('a','worker-a',request_id=str(uuid.uuid4()) if version>=5 else None)
    quote={'model':job['request']['model'],'source':'native_page','observed_at':clock.now,'required_credits':20,'request_fingerprint':quote_request_fingerprint(job['request'])}
    store.credit_preflight(job['id'],'worker-a',job['claim_token'],quote,100)
    return store,clock,job

def deny(store,job,**kwargs):
    args={'job_id':job['id'],'worker_id':job['worker_id'],'claim_token':job['claim_token'],'error_code':'api_key_revoked','message':'The API key is no longer active.'}
    args.update(kwargs);return store.deny_activation(**args)

def test_denial_fences_job_and_duplicate_returns_same_proof(tmp_path):
    store,clock,job=fixture(tmp_path)
    result=deny(store,job)
    assert result['can_activate'] is False and result['generation_not_activated'] is True and result['job_id']==job['id']
    after=store.get(job['id']);assert after['status']=='failed' and after['slot_released'] and after['activation_started_at'] is None
    clock.now+=1
    assert deny(store,job,message='Changed later policy message')==result
    with pytest.raises(BridgeError):store.activate(job['id'],'worker-a',job['claim_token'])
    reported=store.report(job['id'],'worker-a',job['claim_token'],status='failed',generation_not_activated=True,error_code=result['error_code'],error=result['message'])
    assert reported['status']=='failed' and reported['submit_id'] is None

def test_activated_job_never_gets_negative_proof(tmp_path):
    store,_,job=fixture(tmp_path);positive=store.activate(job['id'],'worker-a',job['claim_token'])
    with pytest.raises(BridgeError) as error:deny(store,job)
    assert error.value.status==409
    assert store.activate(job['id'],'worker-a',job['claim_token'])==positive
    assert store.get(job['id'])['status']=='claimed'


def test_failed_report_with_possible_acceptance_never_discards_original_task(tmp_path):
    store,_,job=fixture(tmp_path);store.activate(job['id'],'worker-a',job['claim_token'])
    sid=str(uuid.uuid4())
    evidence=normalize_upstream_error({'ret':'server_busy','task_accepted':False,
        'data':{'aigc_data':{'submit_id':sid}}},200)
    assert evidence['acceptance_conflict'] is True
    with pytest.raises(BridgeError):
        store.report(job['id'],'worker-a',job['claim_token'],status='failed',submit_id=sid,
            error_code='upstream_rejected',upstream_ret='server_busy',upstream_error=evidence)
    preserved=store.get(job['id'])
    assert preserved['status']=='claimed' and preserved['activation_started_at'] is not None

@pytest.mark.parametrize('column,value',[
    ('status','uncertain'),('status','submitted'),('status','completed'),('status','queued'),
    ('submit_id','00000000-0000-0000-0000-000000000001'),('confirmed_at',1000),('activation_started_at',1000),
])
def test_unknown_or_accepted_marker_blocks_negative_proof(tmp_path,column,value):
    store,_,job=fixture(tmp_path)
    with sqlite3.connect(store.path) as db:db.execute('UPDATE jobs SET '+column+'=? WHERE id=?',(value,job['id']))
    with pytest.raises(BridgeError) as error:deny(store,job)
    assert error.value.status==409

def test_legacy_job_without_marker_is_not_proof_of_no_activation(tmp_path):
    store,_,job=fixture(tmp_path,version=4)
    with pytest.raises(BridgeError):deny(store,job)
    assert store.get(job['id'])['status']=='claimed'

@pytest.mark.parametrize('change', [{'worker_id':'other-worker'},{'claim_token':'wrong-owner'}])
def test_wrong_owner_cannot_fence_claim(tmp_path,change):
    store,_,job=fixture(tmp_path)
    with pytest.raises(BridgeError) as error:deny(store,job,**change)
    assert error.value.status==403 and store.get(job['id'])['status']=='claimed'

def test_rotated_old_claim_token_cannot_fence_new_owner(tmp_path):
    store,_,job=fixture(tmp_path)
    current=store.recover_slot('a','worker-a',job['claim_request_id'])['job']
    with pytest.raises(BridgeError) as error:deny(store,job)
    assert error.value.status==403
    assert deny(store,current)['generation_not_activated']

def test_expired_v5_unactivated_claim_can_return_definitive_negative(tmp_path):
    store,clock,job=fixture(tmp_path);clock.now+=601
    result=deny(store,job)
    assert result['can_activate'] is False and result['error_code']=='worker_connection_lost'
    assert store.get(job['id'])['activation_started_at'] is None

@pytest.mark.parametrize('change',[
    {'claim_token':None},{'claim_token':''},{'claim_token':1},{'claim_token':'x'*1025},
    {'error_code':None},{'error_code':'Bad Code'},{'error_code':'x'*81},
    {'message':None},{'message':''},{'message':'x'*1001},{'job_id':'bad'},
])
def test_invalid_arguments_do_not_mutate_job(tmp_path,change):
    store,_,job=fixture(tmp_path)
    with pytest.raises(ValueError):deny(store,job,**change)
    assert store.get(job['id'])['status']=='claimed'

@pytest.mark.parametrize('invalid',[float('nan'),float('inf'),True,'1000'])
def test_invalid_clock_cannot_issue_proof(tmp_path,invalid):
    store,clock,job=fixture(tmp_path);clock.now=invalid
    with pytest.raises(ValueError):deny(store,job)
    clock.now=1000
    assert store.get(job['id'])['status']=='claimed'

def test_simultaneous_positive_and_negative_activation_are_mutually_exclusive(tmp_path):
    store,clock,job=fixture(tmp_path);other=BridgeStore(store.directory,clock=lambda:clock.now,heartbeat_timeout=300,claim_timeout=600)
    barrier=threading.Barrier(2)
    def run(positive):
        barrier.wait()
        try:return (store.activate(job['id'],'worker-a',job['claim_token']) if positive else deny(other,job))
        except BridgeError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[True,False]))
    assert sum(result is not None for result in results)==1
    final=store.get(job['id'])
    assert (final['status']=='failed' and final['activation_started_at'] is None) or (final['status']=='claimed' and final['activation_started_at']==clock.now)

def test_rotation_after_outer_claim_check_cannot_activate_with_old_token(tmp_path,monkeypatch):
    store,clock,job=fixture(tmp_path);original=store.verify_claim;rotated=[]
    def rotate_after_check(*args):
        checked=original(*args)
        if not rotated:rotated.append(store.recover_slot('a','worker-a',job['claim_request_id'])['job'])
        return checked
    monkeypatch.setattr(store,'verify_claim',rotate_after_check)
    with pytest.raises(BridgeError) as error:store.activate(job['id'],'worker-a',job['claim_token'])
    assert error.value.status==403 and store.get(job['id'])['activation_started_at'] is None
    current=rotated[0]
    quote={'model':job['request']['model'],'source':'native_page','observed_at':clock.now,'required_credits':20,'request_fingerprint':quote_request_fingerprint(job['request'])}
    store.credit_preflight(job['id'],'worker-a',current['claim_token'],quote,100)
    assert store.activate(job['id'],'worker-a',current['claim_token'])['can_activate']

@pytest.mark.parametrize('activated',[False,True])
def test_expired_claim_is_not_revived_or_reclassified_by_activation(tmp_path,activated):
    store,clock,job=fixture(tmp_path)
    if activated:store.activate(job['id'],'worker-a',job['claim_token'])
    clock.now+=601;store.expire_claims();before=store.get(job['id'])
    assert before['status']==('uncertain' if activated else 'failed')
    with pytest.raises(BridgeError):store.activate(job['id'],'worker-a',job['claim_token'])
    after=store.get(job['id'])
    assert after['status']==before['status'] and after['activation_started_at']==before['activation_started_at']
