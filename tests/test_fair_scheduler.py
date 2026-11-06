"""Fair reservations share durable turns and capacity across API processes."""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import event, select, update

from dreamina_api.platform_store import (
    PlatformError, PlatformStore, account_schedule, accounts,
)

MODEL='seedance-2.0-mini'
IMAGE='seedream-5.0-lite'
FIXED_TIME=1_000_000_000.0


def fixture(tmp_path, *, caps=(8,8,8), priorities=(1000,0,-1000)):
    url='sqlite:///'+(tmp_path/'fair.db').as_posix()
    secret=Fernet.generate_key()
    store=PlatformStore(url,encryption_key=secret,clock=lambda:FIXED_TIME)
    for ident,cap,priority in zip(('a','b','c'),caps,priorities):
        store.create_account({'id':ident,'name':ident,'capabilities':[MODEL,IMAGE],
                              'session_id':'synthetic-'+ident,'max_concurrent':cap,'max_inflight':cap,'priority':priority})
    created=store.create_key({'name':'synthetic','max_concurrent':100,'rate_limit_per_minute':1000})
    return store,store.authenticate_key(created['key']),url,secret,created['key']


def reserve(store,principal,**kwargs):
    return store.reserve_task(principal,'video',MODEL,{'prompt':'fixture'},**kwargs)


def turns(store):
    with store.engine.connect() as conn:
        return dict(conn.execute(select(account_schedule.c.account_id,account_schedule.c.last_sequence)).all())


def _process_reserve_batch(args):
    """Top-level spawn target: independent process locks and database connection."""
    url,secret,raw_key,count,idem=args
    store=PlatformStore(url,encryption_key=secret,clock=lambda:FIXED_TIME)
    try:
        principal=store.authenticate_key(raw_key);results=[]
        for _ in range(count):
            try:
                task=reserve(store,principal,idempotency_key=idem)
                results.append({'id':task['id'],'account':task['account_id'],'created':task['_created']})
            except PlatformError as exc:results.append({'error':exc.code})
        return results
    finally:store.engine.dispose()


def test_serial_completed_tasks_rotate_even_with_identical_timestamps_and_priority(tmp_path):
    store,p,url,secret,_=fixture(tmp_path)
    selected=[]
    for index in range(12):
        if index==5:
            store.engine.dispose()
            store=PlatformStore(url,encryption_key=secret,clock=lambda:FIXED_TIME)
        task=reserve(store,p);selected.append(task['account_id'])
        store.update_task(task['id'],status='completed')
    assert selected==['a','b','c']*4
    assert turns(store)=={'a':10,'b':11,'c':12}
    assert {a['last_used_at'] for a in store.list_accounts()}=={FIXED_TIME}


def test_smallest_combined_image_video_load_wins_over_turn_and_priority(tmp_path):
    store,p,*_=fixture(tmp_path)
    # Force existing load onto a low-priority account, then reserve normally.
    store.reserve_task(p,'image',IMAGE,{'prompt':'image'},account_id='c')
    reserve(store,p,account_id='a')
    chosen=reserve(store,p)
    assert chosen['account_id']=='b'
    assert Counter(t['account_id'] for t in store.list_tasks())=={'a':1,'b':1,'c':1}


def test_saturation_balances_counts_without_exceeding_different_account_limits(tmp_path):
    store,p,*_=fixture(tmp_path,caps=(1,3,3))
    chosen=[reserve(store,p)['account_id'] for _ in range(7)]
    assert chosen==['a','b','c','b','c','b','c']
    assert Counter(chosen)=={'a':1,'b':3,'c':3}
    before=turns(store)
    with pytest.raises(PlatformError) as error:reserve(store,p)
    assert error.value.code=='no_available_account' and turns(store)==before


def test_idempotency_replay_does_not_advance_rotation_or_count_new_task(tmp_path):
    store,p,*_=fixture(tmp_path)
    first=reserve(store,p,idempotency_key='one');store.update_task(first['id'],status='completed')
    second=reserve(store,p,idempotency_key='two');store.update_task(second['id'],status='completed')
    before=turns(store)
    assert reserve(store,p,idempotency_key='one')['id']==first['id']
    assert turns(store)==before
    assert reserve(store,p)['account_id']=='c'
    assert store.list_keys()[0]['used_requests']==3


def test_api_key_scope_offline_and_uncertain_protections_precede_fairness(tmp_path):
    store,p,*_=fixture(tmp_path)
    raw=store.create_key({'name':'limited','account_ids':['b','c'],'max_concurrent':10})
    limited=store.authenticate_key(raw['key'])
    blocked=set()
    store.worker_status=lambda:[
        {'account_id':'a','workers':[{'online':True,'ready':True}]},
        {'account_id':'b','workers':[{'online':False,'ready':True}]},
        {'account_id':'c','blocked_by_uncertain':'c' in blocked,'workers':[{'online':True,'ready':True}]},
    ]
    task=reserve(store,limited);assert task['account_id']=='c'
    store.update_task(task['id'],status='uncertain')
    blocked.add('c')  # Legacy/unidentified worker protection remains authoritative.
    with pytest.raises(PlatformError) as error:reserve(store,limited)
    assert error.value.code=='no_available_account'
    assert reserve(store,p)['account_id']=='a'


@pytest.mark.parametrize('status',['login_required','login_pending','verification_required','expired','error','disabled'])
def test_unavailable_account_status_never_receives_a_fair_turn(tmp_path,status):
    store,p,*_=fixture(tmp_path)
    # Login supervisors/imported databases may persist a more specific state
    # than the narrower HTTP account-health update vocabulary accepts.
    with store.transaction() as conn:
        conn.execute(update(accounts).where(accounts.c.id=='a').values(status=status,balance=100))
    assert reserve(store,p)['account_id']=='b'
    assert 'a' not in turns(store)


def test_model_cooldown_and_key_model_scope_remain_enforced(tmp_path):
    store,p,*_=fixture(tmp_path)
    store.block_account_model('a',MODEL)
    assert reserve(store,p)['account_id']=='b'
    assert store.reserve_task(p,'image',IMAGE,{'prompt':'image'})['account_id']=='a'
    raw=store.create_key({'name':'images-only','allowed_models':[IMAGE]})
    before=turns(store)
    with pytest.raises(PlatformError) as error:reserve(store,store.authenticate_key(raw['key']))
    assert error.value.code=='model_not_allowed' and turns(store)==before


def test_database_upgrade_adds_turn_table_without_rewriting_old_tasks(tmp_path):
    store,p,url,secret,_=fixture(tmp_path)
    old=reserve(store,p,account_id='a')
    account_schedule.drop(store.engine)  # Simulate an existing pre-upgrade schema.
    store.engine.dispose()
    reopened=PlatformStore(url,encryption_key=secret,clock=lambda:FIXED_TIME)
    assert reopened.get_task(old['id'])['account_id']=='a'
    assert turns(reopened)=={}
    assert reserve(reopened,p)['account_id']=='b'  # Existing a load is preserved.
    before=turns(reopened)
    again=PlatformStore(url,encryption_key=secret,clock=lambda:FIXED_TIME)
    assert turns(again)==before


def test_reservation_failure_rolls_back_task_quota_and_turn_atomically(tmp_path):
    store,p,*_=fixture(tmp_path)
    def reject_turn(conn,clause,multiparams,params,execution_options):
        if getattr(getattr(clause,'table',None),'name',None)=='platform_account_schedule' and getattr(clause,'is_insert',False):
            raise RuntimeError('Synthetic commit failure')
    event.listen(store.engine,'before_execute',reject_turn)
    try:
        with pytest.raises(RuntimeError,match='Synthetic'):reserve(store,p)
    finally:event.remove(store.engine,'before_execute',reject_turn)
    assert store.list_tasks()==[] and turns(store)=={}
    assert store.list_keys()[0]['used_requests']==0
    assert reserve(store,p)['account_id']=='a'


def test_threaded_burst_has_at_most_one_task_difference(tmp_path):
    store,p,*_=fixture(tmp_path,caps=(8,8,8))
    with ThreadPoolExecutor(max_workers=8) as pool:
        allocated=list(pool.map(lambda _:reserve(store,p),range(23)))
    counts=Counter(t['account_id'] for t in allocated)
    assert sorted(counts.values())==[7,8,8]
    assert len({t['id'] for t in allocated})==23


def test_cross_process_burst_never_overfills_and_balances_capacity(tmp_path):
    store,_,url,secret,raw=fixture(tmp_path,caps=(4,4,4))
    with ProcessPoolExecutor(max_workers=4,mp_context=get_context('spawn')) as pool:
        batches=list(pool.map(_process_reserve_batch,[(url,secret,raw,6,None)]*4))
    results=[r for batch in batches for r in batch]
    accepted=[r for r in results if 'id' in r]
    assert len(accepted)==12 and len({r['id'] for r in accepted})==12
    assert Counter(r['account'] for r in accepted)=={'a':4,'b':4,'c':4}
    assert Counter(r.get('error') for r in results if 'error' in r)=={'no_available_account':12}
    assert store.list_keys()[0]['used_requests']==12
    assert max(turns(store).values())==12


def test_cross_process_idempotent_replay_uses_one_quota_and_one_turn(tmp_path):
    store,_,url,secret,raw=fixture(tmp_path)
    with ProcessPoolExecutor(max_workers=3,mp_context=get_context('spawn')) as pool:
        batches=list(pool.map(_process_reserve_batch,[(url,secret,raw,3,'shared-idempotency')]*3))
    results=[r for batch in batches for r in batch]
    assert len({r['id'] for r in results})==1
    assert sum(r['created'] for r in results)==1
    assert store.list_keys()[0]['used_requests']==1 and turns(store)=={'a':1}
