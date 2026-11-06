"""Logical deletion revokes scheduling/auth while retaining historical ownership."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from dreamina_api.admin_api import create_admin_router
from dreamina_api.config import Settings
from dreamina_api.platform_store import ACTIVE_STATES,PlatformStore,PlatformError,accounts,api_keys

MODEL='seedance-2.0-mini'

@pytest.fixture
def env(tmp_path):
    secret=Fernet.generate_key();path=tmp_path/'deletion.sqlite3'
    store=PlatformStore('sqlite:///'+path.as_posix(),encryption_key=secret,clock=lambda:1800000000)
    store.create_account({'id':'one','name':'One','session_id':'synthetic-session','capabilities':[MODEL],'max_concurrent':8,'max_inflight':8})
    created=store.create_key({'name':'Caller','max_concurrent':10,'account_ids':['one']})
    yield SimpleNamespace(store=store,key=created['key'],key_id=created['item']['id'],principal=store.authenticate_key(created['key']),secret=secret,path=path)
    store.engine.dispose()

def task(env,status='queued'):
    value=env.store.reserve_task(env.principal,'video',MODEL,{'prompt':'synthetic only'})
    if status!='queued':value=env.store.update_task(value['id'],status=status)
    return value

def delete_account(store,id='one'):
    store.begin_account_delete(id)
    return store.delete_account(id)

def test_account_disappears_but_history_and_download_credentials_remain(env):
    old=task(env,'completed');env.store.update_task(old['id'],output_path='/private/existing.mp4')
    with env.store.engine.connect() as c:before=c.execute(select(accounts.c.secret_encrypted)).scalar_one()
    result=delete_account(env.store)
    assert result['deleted'] is True and result['enabled'] is False and result['historical_data_retained'] is True
    assert env.store.list_accounts()==[] and env.store.dashboard()['summary']['accounts_total']==0
    with pytest.raises(PlatformError) as error:env.store.get_account('one')
    assert error.value.status==404
    assert env.store.get_account('one',include_deleted=True)['status']=='deleted'
    assert env.store.get_task(old['id'])['account_id']=='one'
    assert env.store.get_task(old['id'])['output_path']=='/private/existing.mp4'
    assert env.store.settings_for_account('one').session_id=='synthetic-session'
    with env.store.engine.connect() as c:assert c.execute(select(accounts.c.secret_encrypted)).scalar_one()==before
    assert env.store.delete_account('one')==result
    assert env.store.begin_account_delete('one')=={'already_deleted':True}

@pytest.mark.parametrize('operation',[
    lambda store:store.update_account('one',{'enabled':True}),
    lambda store:store.begin_browser_login('one'),
    lambda store:store.record_account_check('one',status='ready',balance=100),
    lambda store:store.clear_account_model_block('one',MODEL),
])
def test_deleted_account_cannot_be_reactivated_or_used_for_new_operations(env,operation):
    delete_account(env.store)
    with pytest.raises(PlatformError) as error:operation(env.store)
    assert error.value.status==404
    assert env.store.get_account('one',include_deleted=True)['enabled'] is False

@pytest.mark.parametrize('status',ACTIVE_STATES)
def test_account_with_any_unfinished_or_uncertain_task_cannot_be_deleted(env,status):
    task(env,status)
    with pytest.raises(PlatformError) as error:env.store.begin_account_delete('one')
    assert error.value.code=='account_busy'
    assert env.store.get_account('one')['enabled'] is True

def test_account_currently_logging_in_cannot_be_deleted(env):
    env.store.begin_browser_login('one')
    with pytest.raises(PlatformError) as error:env.store.begin_account_delete('one')
    assert error.value.code=='login_busy'

def test_pending_delete_blocks_new_reservations_login_updates_and_checks(env):
    before=env.store.get_account('one');previous=env.store.begin_account_delete('one')
    assert env.store.get_account('one')['status']=='deleting'
    with pytest.raises(PlatformError) as error:task(env)
    assert error.value.code=='no_available_account'
    for action in [lambda:env.store.begin_browser_login('one'),lambda:env.store.update_account('one',{'enabled':True}),lambda:env.store.record_account_check('one',status='ready')]:
        with pytest.raises(PlatformError) as error:action()
        assert error.value.code=='account_deleting'
    env.store.cancel_account_delete('one',previous)
    assert env.store.get_account('one')['status']==before['status'] and env.store.get_account('one')['enabled']==before['enabled']

def test_failed_retry_of_interrupted_delete_stays_disabled(env):
    env.store.begin_account_delete('one')
    repeated=env.store.begin_account_delete('one')
    env.store.cancel_account_delete('one',repeated)
    account=env.store.get_account('one');assert not account['enabled'] and account['status']=='disabled'

def test_account_identity_can_be_registered_again_under_a_new_id(env):
    store=env.store;store.begin_browser_login('one')
    store.complete_browser_login('one',credentials={'session_id':'fresh-synthetic'},identity='site-account-123',browser_profile='one')
    delete_account(store)
    store.create_account({'id':'two','name':'Replacement','capabilities':[MODEL]})
    store.begin_browser_login('two')
    replacement=store.complete_browser_login('two',credentials={'session_id':'new-synthetic'},identity='site-account-123',browser_profile='two')
    assert replacement['identity_hint'] and store.settings_for_account('one').session_id=='fresh-synthetic'

def test_deleted_default_is_not_reimported_at_startup(env):
    store=env.store;store.create_account({'id':'default','name':'Default'})
    delete_account(store,'default')
    assert store.import_default(Settings(session_id='must-not-reimport')) is None
    assert all(a['id']!='default' for a in store.list_accounts())

def test_key_delete_revokes_auth_and_retains_tasks_without_restoration(env):
    old=task(env,'completed');result=env.store.delete_key(env.key_id)
    assert result['deleted'] and not result['enabled'] and result['historical_data_retained']
    assert env.store.list_keys()==[] and env.store.dashboard()['summary']['keys_total']==0
    with pytest.raises(PlatformError) as error:env.store.authenticate_key(env.key)
    assert error.value.status==401
    with pytest.raises(PlatformError) as error:env.store.update_key(env.key_id,{'enabled':True})
    assert error.value.status==404
    with pytest.raises(PlatformError) as error:task(env)
    assert error.value.status==401
    assert env.store.get_task(old['id'])['key_id']==env.key_id
    assert env.store.delete_key(env.key_id)==result

def test_key_delete_does_not_cancel_an_already_allocated_task(env):
    old=task(env);env.store.delete_key(env.key_id)
    assert env.store.get_task(old['id'])['status']=='queued'

def test_deleted_account_pool_restriction_is_preserved_not_broadened(env):
    delete_account(env.store)
    env.store.create_account({'id':'two','name':'Two','session_id':'other','capabilities':[MODEL]})
    updated=env.store.update_key(env.key_id,{'name':'Still restricted','account_ids':['one']})
    assert updated['account_ids']==['one']
    with pytest.raises(PlatformError) as error:task(env)
    assert error.value.code=='no_available_account'
    with pytest.raises(PlatformError):env.store.create_key({'name':'New restriction','account_ids':['one']})
    other=env.store.create_key({'name':'Other','account_ids':['two']})
    with pytest.raises(PlatformError):env.store.update_key(other['item']['id'],{'account_ids':['one']})

def test_old_schema_migrates_deleted_fields_and_preserves_rows(env):
    store=env.store;old=task(env,'completed');store.engine.dispose()
    with sqlite3.connect(env.path) as db:
        db.execute('ALTER TABLE platform_accounts DROP COLUMN deleted_at')
        db.execute('ALTER TABLE platform_api_keys DROP COLUMN deleted_at')
    reopened=PlatformStore('sqlite:///'+env.path.as_posix(),encryption_key=env.secret)
    try:
        assert reopened.get_account('one')['deleted'] is False
        assert reopened.authenticate_key(env.key)['deleted'] is False
        assert reopened.get_task(old['id'])['status']=='completed'
        again=PlatformStore('sqlite:///'+env.path.as_posix(),encryption_key=env.secret);again.engine.dispose()
    finally:reopened.engine.dispose()

def admin(env,managed=None,bridge=None):
    env.store.bootstrap_admin('admin','synthetic-admin-password')
    app=FastAPI();app.include_router(create_admin_router(env.store,bridge,managed_workers=managed))
    client=TestClient(app);login=client.post('/api/admin/login',json={'username':'admin','password':'synthetic-admin-password'})
    assert login.status_code==200
    return client,{'X-CSRF-Token':login.json()['csrf_token']}

def test_http_account_deletion_gracefully_stops_then_hides_account(env):
    calls=[]
    class Managed:
        def status(self,id):return {'state':'running'}
        async def stop(self,id):
            calls.append(id);current=env.store.get_account(id)
            assert not current['enabled'] and current['status']=='deleting'
            with pytest.raises(PlatformError):task(env)
    client,headers=admin(env,Managed())
    response=client.delete('/api/admin/accounts/one',headers=headers)
    assert response.status_code==200 and response.json()['deleted'] and calls==['one']
    assert client.get('/api/admin/accounts').json()['items']==[]
    assert client.delete('/api/admin/accounts/one',headers=headers).status_code==200 and calls==['one']

def test_http_stop_failure_restores_account_and_does_not_force_terminate(env):
    class Managed:
        async def stop(self,id):raise PlatformError('Worker did not stop.',code='worker_stop_pending',status=409)
    client,headers=admin(env,Managed());before=env.store.get_account('one')
    response=client.delete('/api/admin/accounts/one',headers=headers)
    assert response.status_code==409 and response.json()['error']['code']=='worker_stop_pending'
    after=env.store.get_account('one');assert after['enabled']==before['enabled'] and after['status']==before['status']

def test_http_busy_bridge_job_blocks_deletion_even_without_supervisor(env):
    bridge=SimpleNamespace(accounts=lambda:[{'account_id':'one','jobs':{'uncertain':1}}])
    client,headers=admin(env,bridge=bridge)
    response=client.delete('/api/admin/accounts/one',headers=headers)
    assert response.status_code==409 and response.json()['error']['code']=='account_busy'
    assert env.store.get_account('one')['enabled']

def test_http_deletion_requires_admin_origin_and_csrf(env):
    client,headers=admin(env)
    assert client.delete('/api/admin/accounts/one').status_code==403
    assert client.delete('/api/admin/keys/'+env.key_id,headers={**headers,'Origin':'https://elsewhere.example'}).status_code==403
    assert env.store.get_account('one')['enabled'] and env.store.authenticate_key(env.key)['enabled']
    response=client.delete('/api/admin/keys/'+env.key_id,headers=headers)
    assert response.status_code==200 and response.json()['deleted']
    assert client.get('/api/admin/keys').json()['items']==[]
