"""Detached capacity and serialized submission turns; no network or browser."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from dreamina_api.platform_store import PlatformStore,PlatformError,submission_turns

MODEL='seedance-2.0-mini'

def fixture(tmp_path,*,workers=True,inflight=3,tabs=1,protocol=7,server_limit=100):
    clock=SimpleNamespace(now=1000.)
    entries=[{'account_id':aid,'workers':[{'worker_id':aid+'-1','driver':'native','owner_id':aid,'slot_index':1,'slot_count':1,
               'protocol_version':protocol,'online':True,'ready':True,'entry_ready':True,'entry_checked_at':1000.}]} for aid in ('a','b')]
    key=Fernet.generate_key();url='sqlite:///'+(tmp_path/'store.sqlite3').as_posix()
    store=PlatformStore(url,encryption_key=key,clock=lambda:clock.now,worker_status=(lambda:entries) if workers else None,account_inflight_limit=server_limit)
    for aid in ('a','b'):store.create_account({'id':aid,'name':aid,'session_id':'synthetic','capabilities':[MODEL],'max_concurrent':tabs,'max_inflight':inflight})
    principal=store.authenticate_key(store.create_key({'name':'synthetic','max_concurrent':100,'rate_limit_per_minute':10000})['key'])
    def new(aid='a'):
        task=store.reserve_task(principal,'video',MODEL,{'prompt':'synthetic'},account_id=aid)
        return store.update_task(task['id'],bridge_job_id=str(uuid.uuid4()),status='claimed')
    return SimpleNamespace(store=store,clock=clock,entries=entries,key=key,url=url,principal=principal,new=new)

def turn(env,task,**kwargs):return env.store.reserve_submission_turn(task['id'],task['bridge_job_id'],**kwargs)
def commit(env,task):return env.store.commit_submission_turn(task['id'],task['bridge_job_id'])
def finish(env,task):return env.store.finish_submission_turn(task['bridge_job_id'])

def test_v6_one_browser_slot_can_admit_multiple_upstream_jobs(tmp_path):
    e=fixture(tmp_path)
    first=e.new();e.store.update_task(first['id'],status='submitted',submit_id=str(uuid.uuid4()))
    e.entries[0]['workers'][0]['ready']=False
    second=e.new();third=e.new()
    assert len({first['id'],second['id'],third['id']})==3
    a=e.store.get_account('a');assert a['max_concurrent']==1 and a['current_slot_capacity']==1
    assert a['max_inflight']==3 and a['current_inflight_capacity']==3 and a['detached_generation_supported']
    with pytest.raises(PlatformError):e.new()

@pytest.mark.parametrize('change',[
    {'protocol_version':5},{'protocol_version':4},{'driver':'extension'},{'slot_index':True},{'owner_id':None},
])
def test_invalid_or_legacy_slots_do_not_enable_detached_admission(tmp_path,change):
    e=fixture(tmp_path);e.entries[0]['workers'][0].update(change)
    account=e.store.get_account('a');assert not account['detached_generation_supported'] and account['current_inflight_capacity']<=1
    if account['current_inflight_capacity']:e.new()
    with pytest.raises(PlatformError):e.new()

def test_mixed_worker_protocols_conservatively_keep_browser_capacity(tmp_path):
    e=fixture(tmp_path,tabs=2)
    old=dict(e.entries[0]['workers'][0],worker_id='legacy',protocol_version=5,slot_index=2)
    e.entries[0]['workers'].append(old)
    assert e.store.get_account('a')['current_inflight_capacity']==2
    e.new();e.new()
    with pytest.raises(PlatformError):e.new()

def test_offline_or_idle_unready_account_does_not_accept_queued_work(tmp_path):
    e=fixture(tmp_path);e.entries[0]['workers'][0]['ready']=False
    with pytest.raises(PlatformError):e.new()
    e.entries[0]['workers'][0].update(ready=True,online=False)
    with pytest.raises(PlatformError):e.new()

def test_default_one_and_independent_tab_and_inflight_settings(tmp_path):
    e=fixture(tmp_path)
    value=e.store.create_account({'id':'new','name':'new','max_concurrent':4})
    assert value['max_inflight']==1 and value['max_concurrent']==4
    e.store.update_account('new',{'max_inflight':7});e.store.update_account('new',{'max_concurrent':2})
    assert e.store.get_account('new')['max_inflight']==7

@pytest.mark.parametrize('value',[0,101,-1,True,1.5,'3',None])
def test_invalid_inflight_configuration_rejected(tmp_path,value):
    e=fixture(tmp_path)
    with pytest.raises(PlatformError):e.store.update_account('a',{'max_inflight':value})

def test_server_inflight_clamp_and_key_concurrency_still_apply(tmp_path):
    e=fixture(tmp_path,inflight=10,server_limit=2)
    assert e.store.get_account('a')['effective_max_inflight']==2
    e.new();e.new()
    with pytest.raises(PlatformError):e.new()

def test_legacy_database_migrates_limit_once_and_enables_wal_indexes(tmp_path):
    e=fixture(tmp_path,tabs=4);e.store.engine.dispose()
    with sqlite3.connect(tmp_path/'store.sqlite3') as db:
        db.execute('ALTER TABLE platform_accounts DROP COLUMN max_inflight')
    upgraded=PlatformStore(e.url,encryption_key=e.key)
    assert upgraded.get_account('a')['max_inflight']==4
    upgraded.update_account('a',{'max_inflight':2});upgraded.engine.dispose()
    again=PlatformStore(e.url,encryption_key=e.key)
    assert again.get_account('a')['max_inflight']==2
    with sqlite3.connect(tmp_path/'store.sqlite3') as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
        indices={r[1] for r in db.execute('PRAGMA index_list(platform_tasks)')}
        assert {'ix_tasks_account_status','ix_tasks_key_status','ix_tasks_key_created','ix_tasks_status_id','ix_tasks_bridge_job'}<=indices

def test_same_egress_is_released_at_activation_but_account_waits_for_receipt(tmp_path):
    e=fixture(tmp_path);a=e.new();b=e.new('b')
    first=turn(e,a);assert first['ready'] and first['expires_at']==1090
    assert turn(e,a)==first and not turn(e,b)['ready']
    assert commit(e,a)['ready'];e.clock.now=1002;assert turn(e,b)['wait_seconds']==1
    e.clock.now=1003;assert turn(e,b)['ready']
    finish(e,a)
    assert commit(e,b)['ready'];finish(e,b)
    e.clock.now=1010;later=e.new();assert turn(e,later)['ready']

def test_same_account_spacing_applies_across_egress_groups(tmp_path):
    e=fixture(tmp_path);a=e.new();b=e.new()
    assert turn(e,a,egress_group='proxy-a')['ready'];commit(e,a);finish(e,a)
    e.clock.now+=3;blocked=turn(e,b,egress_group='proxy-b')
    assert not blocked['ready'] and blocked['wait_seconds']==2
    e.clock.now+=2;assert turn(e,b,egress_group='proxy-b')['ready']

def test_commit_needs_current_unexpired_turn_and_does_not_grant_future_permission(tmp_path):
    e=fixture(tmp_path);a=e.new();b=e.new('b')
    assert commit(e,a)['reason']=='submission_turn_required'
    turn(e,a,lease_seconds=5);e.clock.now+=5
    assert commit(e,a)['reason']=='submission_turn_expired'
    assert turn(e,b)['ready'] and commit(e,a)['reason']=='submission_turn_lost'
    assert not turn(e,a)['ready']

def test_activated_unknown_turn_survives_expiry_and_store_restart(tmp_path):
    e=fixture(tmp_path);a=e.new();b=e.new('b');turn(e,a,lease_seconds=5);commit(e,a)
    e.clock.now+=600
    other=PlatformStore(e.url,encryption_key=e.key,clock=lambda:e.clock.now,worker_status=lambda:e.entries)
    assert other.reserve_submission_turn(b['id'],b['bridge_job_id'])['ready']
    held=[r for r in other.submission_gate_status() if r['job_id']]
    assert any(r['scope']=='account:a' and r['awaiting_receipt'] for r in held)
    assert not other.reserve_submission_turn(a['id'],a['bridge_job_id'])['ready']
    assert commit(e,a)['ready']  # Lost activation RPC acknowledgement remains same turn.

def test_finish_is_idempotent_and_cannot_clear_another_jobs_lease(tmp_path):
    e=fixture(tmp_path);a=e.new();b=e.new('b');turn(e,a,lease_seconds=5);e.clock.now+=6;turn(e,b)
    finish(e,a)
    assert commit(e,b)['ready']
    first=finish(e,b);e.clock.now+=1;assert finish(e,b)==first
    assert turn(e,b)['reason']=='already_finished'

def test_concurrent_processes_get_only_one_present_turn(tmp_path):
    e=fixture(tmp_path);a=e.new();b=e.new('b')
    other=PlatformStore(e.url,encryption_key=e.key,clock=lambda:e.clock.now,worker_status=lambda:e.entries)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls=[pool.submit(s.reserve_submission_turn,t['id'],t['bridge_job_id']) for s,t in ((e.store,a),(other,b))]
        results=[x.result() for x in calls]
    assert sum(r['ready'] for r in results)==1

@pytest.mark.parametrize('change',[{'egress_group':'https://proxy:secret@host'},{'ip_interval':0},{'account_interval':4},{'lease_seconds':91},{'ip_interval':float('nan')},{'lease_seconds':True}])
def test_invalid_pacing_policy_does_not_allocate_a_turn(tmp_path,change):
    e=fixture(tmp_path);a=e.new()
    with pytest.raises(PlatformError):turn(e,a,**change)
    assert e.store.submission_gate_status()==[]

def test_revoked_key_and_stale_parent_cannot_commit(tmp_path):
    e=fixture(tmp_path);a=e.new();turn(e,a)
    e.store.update_key(e.principal['id'],{'enabled':False})
    with pytest.raises(PlatformError) as error:commit(e,a)
    assert error.value.code=='api_key_revoked'
    with e.store.engine.connect() as conn:assert conn.execute(select(submission_turns.c.activated_at)).scalar_one() is None

def test_risk_cooldown_is_idempotent_exponential_and_does_not_change_task_or_balance(tmp_path):
    e=fixture(tmp_path);a=e.new();before=e.store.get_account('a')
    first=e.store.record_submission_risk('a',MODEL,'event-1');assert first['cooldown_seconds']==60
    repeat=e.store.record_submission_risk('a',MODEL,'event-1');assert repeat['recorded'] is False and repeat['blocked_until']==1060
    e.clock.now+=10;second=e.store.record_submission_risk('a',MODEL,'event-2');assert second['cooldown_seconds']==120
    assert e.store.get_task(a['id'])==a and e.store.get_account('a')['balance']==before['balance']
    with pytest.raises(PlatformError):e.new()
    e.clock.now=second['blocked_until']+1;assert e.new()

def test_activation_capacity_uses_inflight_not_physical_slots(tmp_path):
    e=fixture(tmp_path);a=e.new();e.new()
    assert e.store.validate_task_activation(a['id'],a['bridge_job_id'])['id']==a['id']
    e.store.update_account('a',{'max_inflight':1})
    with pytest.raises(PlatformError):e.store.validate_task_activation(a['id'],a['bridge_job_id'])


def test_account_risk_cooldown_blocks_other_models_and_new_keys_but_preserves_existing_work(tmp_path):
    e=fixture(tmp_path);other_model='seedance-2.5'
    e.store.update_account('a',{'capabilities':[MODEL,other_model]})
    prepared=e.store.reserve_task(e.principal,'video',other_model,{'prompt':'Synthetic alternate model'},account_id='a')
    prepared=e.store.update_task(prepared['id'],bridge_job_id=str(uuid.uuid4()),status='claimed')
    running=e.new();running=e.store.update_task(running['id'],status='in_progress',submit_id=str(uuid.uuid4()))
    risk=e.store.record_submission_risk('a',MODEL,'risk-other-model')
    account=e.store.get_account('a')
    assert account['risk_cooldown_until']==risk['blocked_until'] and account['scheduling_paused']
    assert account['scheduling_pause_reason']=='upstream_risk_cooldown'
    with pytest.raises(PlatformError) as rejected:e.store.validate_task_activation(prepared['id'],prepared['bridge_job_id'])
    assert rejected.value.code=='upstream_risk_cooldown'
    fresh=e.store.authenticate_key(e.store.create_key({'name':'new key','account_ids':['a'],'max_concurrent':5})['key'])
    with pytest.raises(PlatformError):e.store.reserve_task(fresh,'video',other_model,{'prompt':'new'},account_id='a')
    e.store.clear_account_model_block('a',MODEL)
    assert e.store.get_account('a')['scheduling_paused']
    with pytest.raises(PlatformError):e.store.reserve_task(fresh,'video',other_model,{'prompt':'new'},account_id='a')
    assert e.store.get_task(running['id'])==running  # Status queries do not consult admission cooldown.
    e.clock.now=risk['blocked_until']
    account=e.store.get_account('a');assert account['risk_cooldown_until'] is None and not account['scheduling_paused']
    assert e.store.validate_task_activation(prepared['id'],prepared['bridge_job_id'])['id']==prepared['id']
    assert e.store.reserve_task(fresh,'video',other_model,{'prompt':'new'},account_id='a')


def test_risk_exponent_is_account_wide_and_credit_threshold_reason_has_precedence(tmp_path):
    e=fixture(tmp_path)
    first=e.store.record_submission_risk('a',MODEL,'one')
    e.clock.now+=10
    second=e.store.record_submission_risk('a','seedance-2.5','two')
    assert first['cooldown_seconds']==60 and second['cooldown_seconds']==120
    e.store.update_account('a',{'min_credit_balance':50});e.store.record_account_check('a',status='ready',balance=10)
    account=e.store.get_account('a')
    assert account['scheduling_pause_reason']=='below_credit_threshold' and account['risk_cooldown_until']==1130
    e.store.record_account_check('a',status='ready',balance=100)
    assert e.store.get_account('a')['scheduling_pause_reason']=='upstream_risk_cooldown'
    e.clock.now=1130
    assert e.store.get_account('a')['scheduling_pause_reason'] is None
    e.clock.now+=86401
    third=e.store.record_submission_risk('a',MODEL,'three');assert third['cooldown_seconds']==60


def test_long_model_permission_block_does_not_extend_whole_account_risk_pause(tmp_path):
    e=fixture(tmp_path)
    e.store.block_account_model('a',MODEL,reason='account_plan_required',cooldown_seconds=1800)
    risk=e.store.record_submission_risk('a',MODEL,'risk-with-existing-permission-block')
    assert risk['cooldown_seconds']==60 and e.store.get_account('a')['risk_cooldown_until']==1060
    block=e.store.account_model_blocks()[0]
    assert block['reason']=='account_plan_required' and block['blocked_until']==2800
    e.clock.now=1060
    assert e.store.get_account('a')['risk_cooldown_until'] is None
