"""Re-login releases browser ownership, never logical tasks or credit evidence."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
import pytest

from dreamina_api.account_login import AccountLoginManager
from dreamina_api.platform_store import PlatformStore,PlatformError

MODEL='seedance-2.0-mini'
IDENTITY='synthetic-website-account-one'


def fixture(tmp_path,*,identity=True,worker_status=None):
    store=PlatformStore('sqlite:///'+(tmp_path/'platform.db').as_posix(),encryption_key=Fernet.generate_key(),worker_status=worker_status,account_capacity_limit=4)
    store.create_account({'id':'one','name':'Synthetic','session_id':'synthetic-old','capabilities':[MODEL],'max_concurrent':4,'max_inflight':4})
    if identity:
        store.begin_browser_login('one')
        store.complete_browser_login('one',identity=IDENTITY,credentials={'session_id':'synthetic-old'},browser_profile='one',balance=100)
    principal=store.authenticate_key(store.create_key({'name':'Synthetic','max_concurrent':20})['key'])
    return SimpleNamespace(store=store,principal=principal)


def task(env,*,status='uncertain',known=True,code='generation_timeout'):
    value=env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'Synthetic'})
    value=env.store.update_task(value['id'],bridge_job_id=str(uuid.uuid4()),status='claimed')
    env.store.reserve_task_credits(value['id'],value['bridge_job_id'],required_credits=20,balance=100,request_fingerprint='a'*64)
    return env.store.update_task(value['id'],status=status,submit_id=str(uuid.uuid4()) if known else None,
                                 error={'code':code,'message':'Synthetic timeout'} if code else None)


@pytest.mark.parametrize('code',['generation_timeout','task_timeout'])
def test_known_background_timeout_can_relogin_same_identity_without_releasing_task_or_hold(tmp_path,code):
    env=fixture(tmp_path);original=task(env,code=code)
    before=deepcopy(env.store.get_task(original['id']));held=deepcopy(env.store.active_credit_holds())
    account=env.store.get_account('one');assert account['active_tasks']==1 and account['blocking_tasks']==0
    assert env.store.begin_browser_login('one')['status']=='login_pending'
    with pytest.raises(PlatformError):env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'No new login-time dispatch'})
    changed=env.store.complete_browser_login('one',identity=IDENTITY,credentials={'session_id':'synthetic-refreshed'},browser_profile='one',balance=75)
    assert changed['status']=='ready' and changed['active_tasks']==1 and changed['blocking_tasks']==0
    assert env.store.settings_for_account('one').session_id=='synthetic-refreshed'
    assert env.store.get_task(original['id'])==before and env.store.active_credit_holds()==held
    with pytest.raises(PlatformError) as error:env.store.begin_account_delete('one')
    assert error.value.code=='account_busy'


@pytest.mark.parametrize('status,known,code',[
    ('queued',False,None),('claimed',False,None),('submitted',True,None),('in_progress',True,None),
    ('uncertain',False,'generation_timeout'),('uncertain',True,'submission_unconfirmed'),
    ('uncertain',True,'upstream_confirmation_missing'),('uncertain',True,None),
])
def test_other_unfinished_tasks_still_block_login(tmp_path,status,known,code):
    env=fixture(tmp_path)
    if status=='queued':
        current=env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'Queued'})
    else:current=task(env,status=status,known=known,code=code)
    account=env.store.get_account('one');assert account['active_tasks']==1 and account['blocking_tasks']==1
    with pytest.raises(PlatformError) as error:env.store.begin_browser_login('one')
    assert error.value.code=='account_busy' and env.store.get_account('one')['status']=='ready'
    assert env.store.get_task(current['id'])['status']==status


def test_relogin_still_rejects_a_different_upstream_identity(tmp_path):
    env=fixture(tmp_path);current=task(env);env.store.begin_browser_login('one');persisted=[]
    with pytest.raises(PlatformError) as error:
        env.store.complete_browser_login('one',identity='different-account',credentials={'session_id':'wrong-account'},browser_profile='one',persist_state=lambda:persisted.append(True))
    assert error.value.code=='identity_mismatch' and not persisted
    assert env.store.settings_for_account('one').session_id=='synthetic-old'
    assert env.store.get_task(current['id'])['status']=='uncertain' and len(env.store.active_credit_holds())==1


def test_legacy_account_without_confirmed_identity_cannot_replace_credentials_for_pending_job(tmp_path):
    env=fixture(tmp_path,identity=False);current=task(env)
    assert env.store.get_account('one')['blocking_tasks']==0
    with pytest.raises(PlatformError) as error:env.store.begin_browser_login('one')
    assert error.value.code=='identity_unverified'
    assert env.store.get_task(current['id'])['status']=='uncertain'


def test_login_recomputes_blocking_tasks_inside_transaction_not_from_cached_account(tmp_path):
    env=fixture(tmp_path);task(env);cached=env.store.get_account('one');assert cached['blocking_tasks']==0
    env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'Arrived after account snapshot'})
    with pytest.raises(PlatformError) as error:env.store.begin_browser_login('one')
    assert error.value.code=='account_busy'


@pytest.mark.parametrize('blocking_jobs,expected',[(0,True),(1,False),(None,False)])
def test_login_manager_respects_bridge_blocking_jobs_with_legacy_safe_fallback(tmp_path,blocking_jobs,expected):
    async def run():
        env=fixture(tmp_path);task(env)
        entry={'account_id':'one','jobs':{'uncertain':1}}
        if blocking_jobs is not None:entry['blocking_jobs']=blocking_jobs
        bridge=SimpleNamespace(accounts=lambda:[entry])
        manager=AccountLoginManager(env.store,bridge,None,root=tmp_path/'managed')
        async def hold(_):await asyncio.Event().wait()
        manager._run=hold
        try:
            if expected:assert (await manager.create('one','owner'))['account_id']=='one'
            else:
                with pytest.raises(PlatformError) as error:await manager.create('one','owner')
                assert error.value.code=='account_busy' and env.store.get_account('one')['status']=='ready'
        finally:await manager.shutdown()
    asyncio.run(run())
