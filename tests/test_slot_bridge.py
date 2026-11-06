"""V5 independent tab claims, activation ownership and bounded failure handling."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from dreamina_api.chrome_bridge import BridgeStore,BridgeError
from dreamina_api.credit_quote import quote_request_fingerprint

PAGE='https://dreamina.capcut.com/ai-tool/generate'


def fixture(tmp_path,*,now=1000,heartbeat_timeout=90,claim_timeout=600):
    clock=SimpleNamespace(now=float(now))
    store=BridgeStore(tmp_path/'slots',clock=lambda:clock.now,heartbeat_timeout=heartbeat_timeout,claim_timeout=claim_timeout)
    return store,clock


def ready(store,index=1,*,account='one',count=2,worker=None,owner='owner-one',version=5,driver='native'):
    worker=worker or account+'-slot-'+str(index)
    kwargs={'slot_index':index,'slot_count':count,'owner_id':owner} if version>=5 else {}
    store.heartbeat(account,worker,ready=True,page_url=PAGE,protocol_version=version,driver=driver,**kwargs)
    return worker


def enqueue(store,count=1,account='one'):
    return [store.enqueue({'prompt':'Synthetic slot task '+str(n)},account_id=account) for n in range(count)]


def claim(store,worker='one-slot-1',account='one',capacity=2,request_id=None):
    return store.claim(account,worker,capacity=capacity,request_id=request_id or str(uuid.uuid4()))


def approve(store,clock,job,*,required=20,balance=100):
    quote={'model':job['request']['model'],'source':'native_page','observed_at':clock.now,
           'required_credits':required,'request_fingerprint':quote_request_fingerprint(job['request'])}
    store.credit_preflight(job['id'],job['worker_id'],job['claim_token'],quote,balance)
    return quote


def activate(store,job):
    return store.activate(job['id'],job['worker_id'],job['claim_token'])


def report(store,job,status,**kwargs):
    return store.report(job['id'],job['worker_id'],job['claim_token'],status=status,**kwargs)


def test_two_slots_claim_distinct_jobs_atomically_and_one_slot_cannot_claim_twice(tmp_path):
    store,_=fixture(tmp_path);ready(store,1);ready(store,2);jobs=enqueue(store,3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed=list(pool.map(lambda worker:claim(store,worker),['one-slot-1','one-slot-2']))
    assert {j['id'] for j in claimed}=={j['id'] for j in jobs[:2]}
    assert all(j['claim_protocol_version']==5 for j in claimed)
    assert claim(store,'one-slot-1') is None and claim(store,'one-slot-2') is None
    assert store.get(jobs[2]['id'])['status']=='queued'


def test_duplicate_claim_requests_from_one_slot_return_at_most_one_job(tmp_path):
    store,_=fixture(tmp_path);ready(store);enqueue(store,3)
    with ThreadPoolExecutor(max_workers=5) as pool:claims=list(pool.map(lambda _:claim(store),range(5)))
    assert sum(job is not None for job in claims)==1


def test_server_capacity_overrides_slot_advertisement_and_pause_does_not_consume_queue(tmp_path):
    store,_=fixture(tmp_path);ready(store,1,count=8);ready(store,2,count=8);jobs=enqueue(store,2)
    assert store.claim('one','one-slot-2',capacity=1,request_id=str(uuid.uuid4())) is None
    assert store.claim('one','one-slot-1',capacity=0,request_id=str(uuid.uuid4())) is None
    assert store.claim('one','one-slot-1',capacity=2,paused=True,request_id=str(uuid.uuid4())) is None
    assert all(store.get(j['id'])['status']=='queued' for j in jobs)
    assert store.claim('one','one-slot-1',capacity=1,request_id=str(uuid.uuid4()))['id']==jobs[0]['id']
    assert store.claim('one','one-slot-2',capacity=1,request_id=str(uuid.uuid4())) is None


@pytest.mark.parametrize('kwargs',[
    {'index':0},{'index':9},{'index':True},{'index':2,'count':1},{'count':9},{'count':True},
    {'owner':''},{'version':5,'driver':'extension'},
])
def test_invalid_v5_slot_metadata_is_rejected(tmp_path,kwargs):
    store,_=fixture(tmp_path)
    with pytest.raises((ValueError,TypeError)):ready(store,**kwargs)


def test_duplicate_live_owner_slot_is_rejected_but_stale_slot_can_be_replaced(tmp_path):
    store,clock=fixture(tmp_path);ready(store,worker='old-slot')
    with pytest.raises(BridgeError,match='owns'):ready(store,worker='new-slot')
    clock.now+=91;ready(store,worker='new-slot')
    rows=store.accounts()[0]['workers']
    assert next(w for w in rows if w['worker_id']=='new-slot')['online']
    assert not next(w for w in rows if w['worker_id']=='old-slot')['online']


def test_unknown_outcome_blocks_its_slot_but_not_another_v5_slot_or_account(tmp_path):
    store,clock=fixture(tmp_path);ready(store,1);ready(store,2);ready(store,1,account='two',count=1,owner='owner-two')
    enqueue(store,3);enqueue(store,1,'two')
    first=claim(store);approve(store,clock,first);activate(store,first)
    report(store,first,'uncertain',error_code='submission_unconfirmed',error='Synthetic missing receipt')
    assert claim(store,'one-slot-1') is None
    second=claim(store,'one-slot-2');assert second and second['id']!=first['id']
    other=claim(store,'two-slot-1','two',1);assert other
    account=next(a for a in store.accounts() if a['account_id']=='one')
    assert account['attention_tasks']==1 and account['blocked_by_uncertain'] is False


def test_legacy_workers_keep_account_wide_uncertainty_protection(tmp_path):
    store,_=fixture(tmp_path);ready(store,worker='old-a',version=4);ready(store,worker='old-b',version=4)
    enqueue(store,2);first=claim(store,'old-a')
    report(store,first,'uncertain',error='Legacy action outcome unknown')
    assert claim(store,'old-b',capacity=8) is None
    assert store.accounts()[0]['blocked_by_uncertain'] is True


def test_legacy_unknown_claim_keeps_account_guard_when_live_workers_upgrade(tmp_path):
    store,_=fixture(tmp_path);ready(store,worker='old-a',version=4);enqueue(store,2)
    first=claim(store,'old-a');report(store,first,'uncertain',error='Legacy activation unknown')
    ready(store,1,worker='old-a',version=5);ready(store,2,version=5)
    account=store.accounts()[0]
    assert all(worker['protocol_version']==5 for worker in account['workers'])
    assert account['blocked_by_uncertain'] is True
    assert claim(store,'one-slot-2',capacity=2) is None


def test_known_id_generation_timeout_releases_tab_but_still_counts_logical_capacity(tmp_path):
    store,clock=fixture(tmp_path);ready(store,1);ready(store,2);enqueue(store,3)
    first=claim(store);approve(store,clock,first);activate(store,first)
    sid=str(uuid.uuid4());report(store,first,'submitted',submit_id=sid)
    timed=store.deadline(first['id'],'generation_timeout','Synthetic upstream time limit')
    assert timed['status']=='uncertain' and timed['slot_released'] and timed['submit_id']==sid
    assert claim(store,capacity=1) is None
    second=claim(store,capacity=2);assert second and second['id']!=first['id']
    assert claim(store,'one-slot-2',capacity=2) is None
    done=store.reconcile_submitted(first['id'],{'id':sid,'status':'completed','videos':[{'url':'https://example.test/fixture.mp4'}]})
    assert done['status']=='completed' and done['error_code'] is None
    assert store.get(second['id'])['status']=='claimed'
    assert claim(store,'one-slot-2',capacity=2)


@pytest.mark.parametrize('version,activated,expected',[(5,False,'failed'),(5,True,'uncertain'),(4,False,'uncertain')])
def test_disconnect_uses_claim_protocol_and_activation_evidence(tmp_path,version,activated,expected):
    store,clock=fixture(tmp_path);worker=ready(store,version=version);enqueue(store)
    job=claim(store,worker)
    if activated:approve(store,clock,job);activate(store,job)
    reopened=BridgeStore(store.directory,clock=lambda:clock.now)
    assert reopened.get(job['id'])['claim_protocol_version']==version
    clock.now+=91
    reopened.expire_claims()
    outcome=reopened.get(job['id'])
    assert outcome['status']==expected and outcome['error_code']=='worker_connection_lost'
    assert outcome['slot_released']==(expected=='failed')


def test_migrated_legacy_claim_without_protocol_snapshot_never_becomes_safe_after_upgrade(tmp_path):
    store,clock=fixture(tmp_path);ready(store,worker='legacy',version=4);enqueue(store);job=claim(store,'legacy')
    with sqlite3.connect(store.path) as db:db.execute('ALTER TABLE jobs DROP COLUMN claim_protocol_version')
    upgraded=BridgeStore(store.directory,clock=lambda:clock.now)
    assert upgraded.get(job['id'])['claim_protocol_version'] is None
    ready(upgraded,worker='legacy',version=5)
    clock.now+=91
    upgraded.expire_claims()
    assert upgraded.get(job['id'])['status']=='uncertain'


def test_activation_requires_owned_fresh_approved_preflight_and_is_idempotent(tmp_path):
    store,clock=fixture(tmp_path);ready(store);enqueue(store);job=claim(store)
    with pytest.raises(BridgeError,match='preflight'):activate(store,job)
    quote=approve(store,clock,job)
    with pytest.raises(BridgeError):store.activate(job['id'],'wrong-worker',job['claim_token'])
    with pytest.raises(BridgeError):store.activate(job['id'],job['worker_id'],'wrong-token')
    first=activate(store,job);clock.now+=1;second=activate(store,job)
    assert first==second and first['can_activate']
    assert store.get(job['id'])['activation_started_at']==1000
    with pytest.raises(BridgeError):store.credit_preflight(job['id'],job['worker_id'],job['claim_token'],quote,100)


def test_stale_or_rejected_preflight_cannot_activate(tmp_path):
    store,clock=fixture(tmp_path);ready(store);enqueue(store,2);job=claim(store)
    approve(store,clock,job);clock.now+=61
    with pytest.raises(BridgeError,match='fresh'):activate(store,job)
    ready(store);report(store,job,'failed',generation_not_activated=True)
    other=claim(store);approve(store,clock,other,required=20,balance=1)
    with pytest.raises(BridgeError):activate(store,other)


@pytest.mark.parametrize('code',['preparation_timeout','submission_unconfirmed','task_timeout'])
@pytest.mark.parametrize('activated',[False,True])
def test_deadline_before_and_after_activation_never_confuses_safe_failure_with_unknown(tmp_path,code,activated):
    store,clock=fixture(tmp_path);ready(store);enqueue(store);job=claim(store)
    if activated:approve(store,clock,job);activate(store,job)
    state=store.deadline(job['id'],code,'Synthetic deadline')
    assert state['status']==('uncertain' if activated else 'failed')
    assert state['error_code']==code and state['slot_released']==(not activated)
    assert store.deadline(job['id'],code,'Unchanged')['status']==state['status']


def test_zero_timestamp_activation_is_still_a_real_marker(tmp_path):
    store,clock=fixture(tmp_path,now=0);ready(store);enqueue(store);job=claim(store)
    approve(store,clock,job);assert activate(store,job)['activation_started_at']==0
    with pytest.raises(BridgeError):report(store,job,'failed')
    assert store.deadline(job['id'],'task_timeout','Synthetic timeout')['status']=='uncertain'


def test_failure_after_marker_requires_explicit_proof_no_activation(tmp_path):
    store,clock=fixture(tmp_path);ready(store);enqueue(store);job=claim(store)
    approve(store,clock,job);activate(store,job)
    with pytest.raises(BridgeError):report(store,job,'failed',error_code='preparation_timeout')
    with pytest.raises(BridgeError):report(store,job,'failed',generation_not_activated=False)
    safe=report(store,job,'failed',generation_not_activated=True,error_code='preparation_timeout')
    assert safe['status']=='failed' and safe['slot_released']


@pytest.mark.parametrize('version',[4,5])
def test_known_submitted_id_cannot_be_discarded_by_generic_failed_report(tmp_path,version):
    store,clock=fixture(tmp_path);ready(store,version=version);enqueue(store);job=claim(store)
    if version==5:approve(store,clock,job);activate(store,job)
    sid=str(uuid.uuid4());report(store,job,'submitted',submit_id=sid)
    with pytest.raises(BridgeError):report(store,job,'failed',generation_not_activated=True)
    with pytest.raises(BridgeError):report(store,job,'failed',error_code='upstream_rejected',upstream_ret='-6',submit_id=sid)
    assert store.get(job['id'])['status']=='submitted' and store.get(job['id'])['submit_id']==sid


def test_initial_explicit_upstream_rejection_keeps_observed_id_as_failed_evidence(tmp_path):
    store,clock=fixture(tmp_path);ready(store);enqueue(store);job=claim(store)
    approve(store,clock,job);activate(store,job);sid=str(uuid.uuid4())
    result=report(store,job,'failed',submit_id=sid,error_code='upstream_rejected',upstream_ret='-6')
    assert result['status']=='failed' and result['submit_id']==sid and result['error_code']=='upstream_rejected'


@pytest.mark.parametrize('changes',[{'upstream_ret':'0'},{'upstream_ret':None},{'upstream_ret':True},
                                   {'upstream_ret':'-6','error_code':'preparation_failed'},
                                   {'upstream_ret':'-6\nInjected'}])
def test_upstream_rejection_exception_requires_bounded_nonzero_response_proof(tmp_path,changes):
    store,clock=fixture(tmp_path);ready(store);enqueue(store);job=claim(store)
    approve(store,clock,job);activate(store,job)
    fields={'submit_id':str(uuid.uuid4()),'error_code':'upstream_rejected','upstream_ret':'-6',**changes}
    with pytest.raises((BridgeError,ValueError)):report(store,job,'failed',**fields)
    assert store.get(job['id'])['status']=='claimed'


def test_confirmed_task_timeout_cannot_be_reclassified_as_initial_rejection(tmp_path):
    store,clock=fixture(tmp_path);ready(store);enqueue(store);job=claim(store)
    approve(store,clock,job);activate(store,job);sid=str(uuid.uuid4())
    report(store,job,'submitted',submit_id=sid)
    store.deadline(job['id'],'generation_timeout','Still pending')
    with pytest.raises(BridgeError):
        report(store,job,'failed',submit_id=sid,error_code='upstream_rejected',upstream_ret='-6')
    assert store.get(job['id'])['submit_id']==sid and store.get(job['id'])['status']=='uncertain'


def test_error_code_and_deadline_are_scoped_to_one_job(tmp_path):
    store,clock=fixture(tmp_path);ready(store,1);ready(store,2);enqueue(store,2)
    first,second=claim(store),claim(store,'one-slot-2')
    approve(store,clock,first);activate(store,first)
    report(store,first,'uncertain',error_code='submission_unconfirmed',error='First slot missing receipt')
    unchanged=store.get(second['id'])
    assert unchanged['status']=='claimed' and unchanged['error_code'] is None and unchanged['activation_started_at'] is None
    with pytest.raises(ValueError):report(store,second,'failed',error_code='Invalid Error')
    with pytest.raises(ValueError):store.deadline(second['id'],'made_up_timeout','bad')
    assert store.get(second['id'])['status']=='claimed'


def test_v5_claim_requires_nonce_while_legacy_claim_remains_compatible(tmp_path):
    store,_=fixture(tmp_path);ready(store);enqueue(store)
    with pytest.raises((BridgeError,ValueError)):store.claim('one','one-slot-1',capacity=2)
    with pytest.raises((BridgeError,ValueError)):store.claim('one','one-slot-1',capacity=2,request_id='not-a-uuid')
    ready(store,account='legacy',worker='legacy-worker',version=4);enqueue(store,account='legacy')
    assert store.claim('legacy','legacy-worker')


def test_empty_recovery_persists_fence_before_late_original_claim_can_arrive(tmp_path):
    store,clock=fixture(tmp_path);ready(store);nonce=str(uuid.uuid4())
    recovered=store.recover_slot('one','one-slot-1',request_id=nonce)
    assert recovered['job'] is None and recovered['slot_released'] is True
    queued=enqueue(store)[0]
    reopened=BridgeStore(store.directory,clock=lambda:clock.now)
    assert claim(reopened,request_id=nonce) is None
    assert reopened.get(queued['id'])['status']=='queued'
    assert claim(reopened,request_id=str(uuid.uuid4()))['id']==queued['id']


def test_matching_unactivated_claim_recovery_rotates_only_original_task_token(tmp_path):
    store,clock=fixture(tmp_path);ready(store);jobs=enqueue(store,2);nonce=str(uuid.uuid4())
    first=claim(store,request_id=nonce);approve(store,clock,first)
    recovered=store.recover_slot('one','one-slot-1',request_id=nonce)
    replacement=recovered['job']
    assert replacement['id']==first['id'] and recovered['slot_released'] is False
    assert replacement['claim_token']!=first['claim_token']
    assert replacement['credit_preflight'] is None
    with pytest.raises(BridgeError):store.verify_claim(first['id'],first['worker_id'],first['claim_token'])
    assert store.verify_claim(replacement['id'],replacement['worker_id'],replacement['claim_token'])['id']==first['id']
    assert claim(store,request_id=nonce) is None and store.get(jobs[1]['id'])['status']=='queued'
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT claim_request_id FROM jobs WHERE id=?',(first['id'],)).fetchone()[0]==nonce


def test_activation_marker_prevents_recovery_even_when_claim_nonce_matches(tmp_path):
    store,clock=fixture(tmp_path);ready(store);enqueue(store);nonce=str(uuid.uuid4())
    first=claim(store,request_id=nonce);approve(store,clock,first);activate(store,first)
    recovered=store.recover_slot('one','one-slot-1',request_id=nonce)
    assert recovered['job'] is None and recovered['slot_released'] is False
    assert store.verify_claim(first['id'],first['worker_id'],first['claim_token'])['activation_started_at']==clock.now


def test_wrong_or_old_nonce_never_recovers_a_new_claim_or_rotates_its_token(tmp_path):
    store,_=fixture(tmp_path);ready(store);old=str(uuid.uuid4());new=str(uuid.uuid4())
    assert store.recover_slot('one','one-slot-1',request_id=old)['slot_released'] is True
    enqueue(store,2);current=claim(store,request_id=new)
    wrong=store.recover_slot('one','one-slot-1',request_id=old)
    assert wrong['job'] is None and wrong['slot_released'] is False
    assert store.verify_claim(current['id'],current['worker_id'],current['claim_token'])['id']==current['id']
    assert claim(store,request_id=old) is None


def test_recovery_without_original_nonce_cannot_declare_slot_safe(tmp_path):
    store,_=fixture(tmp_path);ready(store);enqueue(store);current=claim(store)
    missing=store.recover_slot('one','one-slot-1',request_id=None)
    assert missing['job'] is None and missing['slot_released'] is False
    assert store.verify_claim(current['id'],current['worker_id'],current['claim_token'])['id']==current['id']


def test_nonce_fence_is_scoped_to_the_exact_registered_worker(tmp_path):
    store,_=fixture(tmp_path);ready(store,1);ready(store,2);nonce=str(uuid.uuid4())
    store.recover_slot('one','one-slot-1',request_id=nonce)
    queued=enqueue(store)[0]
    assert claim(store,'one-slot-2',request_id=nonce)['id']==queued['id']
    with pytest.raises(BridgeError):store.recover_slot('different-account','one-slot-2',request_id=nonce)
