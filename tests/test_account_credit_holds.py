"""Thresholds and conservative concurrent credit budgets; no upstream requests."""
from concurrent.futures import ProcessPoolExecutor,ThreadPoolExecutor
from multiprocessing import get_context
from types import SimpleNamespace
import uuid
import sqlite3

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import text,update

from dreamina_api.platform_store import PlatformStore,PlatformError,accounts,credit_holds

MODEL='seedance-2.0-mini'
FINGERPRINT='a'*64


def fixture(tmp_path,*,minimum=0,cap=8,limit=8):
    url='sqlite:///'+(tmp_path/'credits.sqlite3').as_posix();secret=Fernet.generate_key()
    store=PlatformStore(url,encryption_key=secret,clock=lambda:1000.,account_capacity_limit=limit,account_inflight_limit=limit)
    store.create_account({'id':'one','name':'Synthetic','session_id':'synthetic','capabilities':[MODEL],
                          'max_concurrent':cap,'max_inflight':cap,'min_credit_balance':minimum})
    key=store.create_key({'name':'Synthetic','max_concurrent':100,'rate_limit_per_minute':1000})
    return SimpleNamespace(store=store,key=key,principal=store.authenticate_key(key['key']),url=url,secret=secret)


def task(env,account_id='one'):
    value=env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'synthetic credit check'},account_id=account_id)
    return env.store.update_task(value['id'],bridge_job_id=str(uuid.uuid4()),status='claimed')


def hold(env,value,amount=20,balance=100,**kwargs):
    return env.store.reserve_task_credits(value['id'],value['bridge_job_id'],required_credits=amount,balance=balance,
                                         request_fingerprint=kwargs.get('request_fingerprint',FINGERPRINT))


def test_threshold_is_strict_and_refresh_resumes_without_changing_enabled(tmp_path):
    env=fixture(tmp_path,minimum=20)
    env.store.record_account_check('one',status='ready',balance=19)
    account=env.store.get_account('one')
    assert account['scheduling_paused'] and account['scheduling_pause_reason']=='below_credit_threshold'
    assert account['enabled'] is True
    with pytest.raises(PlatformError) as error:task(env)
    assert error.value.code=='no_available_account'
    env.store.record_account_check('one',status='ready',balance=20)
    assert not env.store.get_account('one')['scheduling_paused']
    assert task(env)['account_id']=='one'
    env.store.update_account('one',{'enabled':False})
    env.store.record_account_check('one',status='ready',balance=200)
    assert not env.store.get_account('one')['enabled']
    with pytest.raises(PlatformError):task(env)


def test_unknown_balance_admits_to_preflight_but_fresh_low_balance_blocks_activation(tmp_path):
    env=fixture(tmp_path,minimum=20);value=task(env)
    account=env.store.get_account('one')
    assert account['balance'] is None and account['available_credits'] is None and not account['scheduling_paused']
    decision=hold(env,value,amount=0,balance=19)
    assert decision['allowed'] is False and decision['reason']=='below_credit_threshold'
    assert env.store.active_credit_holds()==[] and env.store.get_account('one')['scheduling_paused']


def test_zero_threshold_disables_pause_and_free_quotes_reserve_zero(tmp_path):
    env=fixture(tmp_path);env.store.record_account_check('one',status='ready',balance=0)
    value=task(env);decision=hold(env,value,amount=0,balance=0)
    assert decision['allowed'] is True and decision['reason'] is None
    assert env.store.get_account('one')['reserved_credits']==0
    assert not env.store.get_account('one')['scheduling_paused']


@pytest.mark.parametrize('value',[-1,True,'20',float('inf'),float('nan'),1_000_000_001,10**400,None])
def test_invalid_threshold_does_not_change_existing_account(tmp_path,value):
    env=fixture(tmp_path,minimum=20)
    with pytest.raises(PlatformError):env.store.update_account('one',{'min_credit_balance':value})
    assert env.store.get_account('one')['min_credit_balance']==20


def test_threshold_edit_uses_latest_value_and_does_not_cancel_active_task(tmp_path):
    env=fixture(tmp_path);value=task(env)
    env.store.update_account('one',{'min_credit_balance':200})
    decision=hold(env,value,amount=20,balance=100)
    assert decision['reason']=='below_credit_threshold'
    assert env.store.get_task(value['id'])['status']=='claimed'
    env.store.update_account('one',{'min_credit_balance':0})
    assert hold(env,value,amount=20,balance=100)['allowed']


def test_account_capacity_is_bounded_without_rewriting_legacy_values(tmp_path):
    env=fixture(tmp_path,limit=2)
    with env.store.transaction() as conn:
        conn.execute(update(accounts).where(accounts.c.id=='one').values(max_concurrent=100))
    account=env.store.get_account('one');assert account['max_concurrent']==100 and account['effective_max_concurrent']==2
    for _ in range(2):task(env)
    with pytest.raises(PlatformError):task(env)
    with pytest.raises(PlatformError):env.store.update_account('one',{'max_concurrent':9})
    assert env.store.get_account('one')['max_concurrent']==100


@pytest.mark.parametrize('limit',[0,9,True,4.0,'4'])
def test_invalid_capacity_limit_is_rejected(tmp_path,limit):
    with pytest.raises(ValueError):fixture(tmp_path,limit=limit)


def test_concurrent_preflights_cannot_spend_the_same_credit_twice(tmp_path):
    env=fixture(tmp_path);values=[task(env) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        decisions=list(pool.map(lambda value:hold(env,value,60,100),values))
    assert sum(d['allowed'] for d in decisions)==1
    assert all(d['reason']=='credits_reserved' for d in decisions if not d['allowed'])
    assert sum(row['amount'] for row in env.store.active_credit_holds())==60
    assert env.store.get_account('one')['available_credits']==40


def test_own_hold_is_not_counted_twice_and_repricing_does_not_take_other_jobs_budget(tmp_path):
    env=fixture(tmp_path);first,second=task(env),task(env)
    assert hold(env,first,20,100)['allowed'];assert hold(env,second,60,100)['allowed']
    again=hold(env,first,20,100)
    assert again['allowed'] and again['held_credits']==60 and again['available_credits']==40
    denied=hold(env,first,50,100)
    assert not denied['allowed'] and denied['reason']=='credits_reserved'
    assert [row['job_id'] for row in env.store.active_credit_holds()]==[second['bridge_job_id']]


def test_new_upstream_balance_can_conservatively_double_count_an_unsettled_debit(tmp_path):
    env=fixture(tmp_path);first,second=task(env),task(env)
    assert hold(env,first,20,100)['allowed']
    decision=hold(env,second,70,80)
    assert not decision['allowed'] and decision['reason']=='credits_reserved'
    assert decision['balance']==80 and decision['held_credits']==20 and decision['available_credits']==60


@pytest.mark.parametrize('status',['completed','failed','cancelled'])
def test_terminal_task_releases_its_hold_once_and_keeps_other_holds(tmp_path,status):
    env=fixture(tmp_path);first,second=task(env),task(env)
    hold(env,first);hold(env,second)
    env.store.update_task(first['id'],status=status)
    assert [r['job_id'] for r in env.store.active_credit_holds()]==[second['bridge_job_id']]
    assert env.store.release_task_credits(first['id'])==0


def test_uncertain_charge_keeps_hold_across_time_and_restart(tmp_path):
    env=fixture(tmp_path);value=task(env);hold(env,value,60,100)
    env.store.update_task(value['id'],status='uncertain')
    with pytest.raises(PlatformError,match='retain'):env.store.release_task_credits(value['id'])
    env.store.engine.dispose();env.store=PlatformStore(env.url,encryption_key=env.secret,clock=lambda:9999999999.)
    assert env.store.active_credit_holds()[0]['amount']==60
    assert env.store.get_account('one')['reserved_credits']==60
    other=task(env)
    assert hold(env,other,50,100)['reason']=='credits_reserved'
    assert env.store.get_task(value['id'])['status']=='uncertain'


def test_explicit_no_submission_release_is_owned_and_idempotent(tmp_path):
    env=fixture(tmp_path);first,second=task(env),task(env);hold(env,first);hold(env,second)
    assert env.store.release_task_credits(first['id'],second['bridge_job_id'])==0
    assert env.store.release_task_credits(first['id'],first['bridge_job_id'])==1
    assert env.store.release_task_credits(first['id'],first['bridge_job_id'])==0
    assert len(env.store.active_credit_holds())==1


def test_conflicting_task_identity_or_activated_task_cannot_replace_hold(tmp_path):
    env=fixture(tmp_path);value=task(env);hold(env,value)
    with pytest.raises(PlatformError,match='different'):hold(env,value,request_fingerprint='b'*64)
    env.store.update_task(value['id'],submit_id=str(uuid.uuid4()),status='submitted')
    with pytest.raises(PlatformError,match='unsubmitted'):hold(env,value)
    with pytest.raises(PlatformError,match='retain'):env.store.release_task_credits(value['id'])


@pytest.mark.parametrize('value',[True,-1,float('nan'),float('inf'),'10',1_000_000_001,10**400])
@pytest.mark.parametrize('field',['amount','balance'])
def test_invalid_quote_or_balance_cannot_create_holds(tmp_path,value,field):
    env=fixture(tmp_path);current=task(env);kw={'amount':20,'balance':100};kw[field]=value
    with pytest.raises(PlatformError):hold(env,current,**kw)
    assert not env.store.active_credit_holds()


def test_upgrade_preserves_threshold_and_active_holds(tmp_path):
    env=fixture(tmp_path,minimum=5);value=task(env);hold(env,value)
    env.store.engine.dispose();env.store=PlatformStore(env.url,encryption_key=env.secret)
    assert env.store.get_account('one')['min_credit_balance']==5
    assert env.store.active_credit_holds()[0]['task_id']==value['id']
    env.store.engine.dispose()


def test_old_account_schema_gains_zero_threshold_without_rewriting_identity(tmp_path):
    env=fixture(tmp_path);env.store.engine.dispose()
    with sqlite3.connect(tmp_path/'credits.sqlite3') as db:
        db.execute('ALTER TABLE platform_accounts DROP COLUMN min_credit_balance')
    upgraded=PlatformStore(env.url,encryption_key=env.secret)
    account=upgraded.get_account('one')
    assert account['min_credit_balance']==0 and account['name']=='Synthetic' and account['secret_configured']
    upgraded.engine.dispose()


@pytest.mark.parametrize('reason',['below_credit_threshold','credits_reserved'])
def test_new_credit_denials_reassign_same_task_only_before_submission(tmp_path,reason):
    env=fixture(tmp_path,minimum=200 if reason=='below_credit_threshold' else 0)
    if reason=='credits_reserved':
        blocker=task(env);assert hold(env,blocker,80,100)['allowed']
    current=task(env)
    decision=hold(env,current,20 if reason=='below_credit_threshold' else 40,100)
    assert not decision['allowed'] and decision['reason']==reason
    env.store.create_account({'id':'two','name':'Other','session_id':'synthetic','capabilities':[MODEL]})
    job={'id':current['bridge_job_id'],'account_id':'one','status':'failed','submit_id':None,
         'credit_preflight':{**decision,'source':'native_page','generation_not_activated':True}}
    used=env.store.list_keys()[0]['used_requests']
    moved=env.store.reassign_insufficient_credits(current['id'],job)
    assert moved['id']==current['id'] and moved['account_id']=='two' and moved['status']=='queued'
    assert moved['request_detail']['credit_attempts'][0]['reason']==reason
    assert env.store.list_keys()[0]['used_requests']==used
    event=next(e for e in env.store.list_events(current['id']) if e['stage']=='account_reassigned')
    assert event['detail']['reason']==reason


def test_reserved_credit_exhaustion_reports_specific_error_without_releasing_other_job(tmp_path):
    env=fixture(tmp_path);blocker=task(env);hold(env,blocker,80,100)
    current=task(env);decision=hold(env,current,40,100)
    job={'id':current['bridge_job_id'],'account_id':'one','status':'failed','submit_id':None,
         'credit_preflight':{**decision,'source':'native_page','generation_not_activated':True}}
    stopped=env.store.reassign_insufficient_credits(current['id'],job)
    assert stopped['status']=='failed' and stopped['error']['code']=='credits_reserved'
    assert env.store.active_credit_holds()[0]['task_id']==blocker['id']


def test_all_other_threshold_paused_accounts_fail_without_waiting(tmp_path):
    env=fixture(tmp_path);current=task(env);decision=hold(env,current,20,1)
    env.store.create_account({'id':'two','name':'Paused','session_id':'synthetic','capabilities':[MODEL],'min_credit_balance':30})
    env.store.record_account_check('two',status='ready',balance=29)
    job={'id':current['bridge_job_id'],'account_id':'one','status':'failed','submit_id':None,
         'credit_preflight':{**decision,'source':'native_page','generation_not_activated':True}}
    stopped=env.store.reassign_insufficient_credits(current['id'],job)
    assert stopped['status']=='failed' and stopped['error']['code']=='insufficient_credits'


def _reserve_in_process(args):
    url,secret,task_id,job_id=args
    store=PlatformStore(url,encryption_key=secret)
    try:return store.reserve_task_credits(task_id,job_id,required_credits=20,balance=100,request_fingerprint=FINGERPRINT)['allowed']
    finally:store.engine.dispose()


def test_cross_process_credit_holds_serialize_eight_preflights(tmp_path):
    env=fixture(tmp_path);values=[task(env) for _ in range(8)]
    args=[(env.url,env.secret,value['id'],value['bridge_job_id']) for value in values]
    with ProcessPoolExecutor(max_workers=4,mp_context=get_context('spawn')) as pool:approved=list(pool.map(_reserve_in_process,args))
    assert sum(approved)==5
    assert sum(row['amount'] for row in env.store.active_credit_holds())==100
