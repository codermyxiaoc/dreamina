"""Real bridge/store lifecycle: remove nodes without removing task evidence."""
import sqlite3
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from dreamina_api.chrome_bridge import BridgeStore,BridgeError,create_chrome_bridge_router
from dreamina_api.platform_store import PlatformStore
from dreamina_api.admin_api import create_admin_router

PAGE='https://dreamina.capcut.com/ai-tool/generate'
SPEC={'model':'seedance-2.0-mini','prompt':'Offline node lifecycle check','duration':4,'ratio':'16:9','resolution':'720p','mode':'omni'}

def fixture(tmp_path):
    clock=SimpleNamespace(now=1000.)
    store=BridgeStore(tmp_path/'bridge',clock=lambda:clock.now)
    return store,clock

def ready(store,index=1,account='one',worker=None,**changes):
    return store.heartbeat(account,worker or account+'-'+str(index),ready=True,page_url=PAGE,driver='native',protocol_version=6,
        slot_index=index,slot_count=max(2,index),owner_id=account+'-owner',**changes)

def rows(store):
    with sqlite3.connect(store.path) as db:return db.execute('SELECT worker_id FROM workers ORDER BY worker_id').fetchall()

def test_only_excess_offline_nodes_are_pruned_and_current_offline_node_remains(tmp_path):
    bridge,clock=fixture(tmp_path);ready(bridge);ready(bridge,2);ready(bridge,account='two')
    assert bridge.prune_workers('one',keep_slots=1)['removed']==[]
    clock.now+=91
    result=bridge.prune_workers('one',keep_slots=1)
    assert result=={'removed':['one-2'],'preserved':[]}
    assert rows(bridge)==[('one-1',),('two-1',)]

def test_stopped_process_proof_removes_fresh_idle_nodes_without_waiting_for_heartbeat_expiry(tmp_path):
    bridge,_=fixture(tmp_path);ready(bridge);ready(bridge,2)
    result=bridge.prune_workers('one',keep_slots=0,process_stopped=True)
    assert set(result['removed'])=={'one-1','one-2'} and rows(bridge)==[]

@pytest.mark.parametrize('status',['claimed','submitted','uncertain'])
def test_retirement_preserves_unfinished_claim_and_original_token(tmp_path,status):
    bridge,clock=fixture(tmp_path);ready(bridge,2)
    queued=bridge.enqueue(SPEC,account_id='one');job=bridge.claim('one','one-2',capacity=2,request_id=str(uuid.uuid4()))
    with sqlite3.connect(bridge.path) as db:
        db.execute('UPDATE jobs SET status=? WHERE id=?',(status,job['id']))
    clock.now+=91
    assert bridge.prune_workers('one',keep_slots=1,process_stopped=True)['preserved']==['one-2']
    assert bridge.retire_worker('one','one-2',keep_slots=1)=={'retired':False,'pending_jobs':1}
    assert bridge.verify_claim(job['id'],'one-2',job['claim_token'])['id']==queued['id']
    with pytest.raises(BridgeError):bridge.retire_account('one')

def test_completed_jobs_and_claim_fences_survive_worker_retirement(tmp_path):
    bridge,_=fixture(tmp_path);ready(bridge,2)
    queued=bridge.enqueue(SPEC,account_id='one');request_id=str(uuid.uuid4())
    job=bridge.claim('one','one-2',capacity=2,request_id=request_id)
    bridge.report(job['id'],'one-2',job['claim_token'],status='failed',generation_not_activated=True)
    with sqlite3.connect(bridge.path) as db:db.execute('INSERT INTO claim_fences VALUES(?,?,?)',('one-2',request_id,1000))
    assert bridge.retire_worker('one','one-2',keep_slots=1)['retired']
    assert rows(bridge)==[] and bridge.get(queued['id'])['status']=='failed'
    with sqlite3.connect(bridge.path) as db:assert db.execute('SELECT count(*) FROM claim_fences').fetchone()[0]==1

def test_late_excess_heartbeat_cannot_recreate_removed_slot_but_expansion_can_register(tmp_path):
    bridge,_=fixture(tmp_path);ready(bridge,2)
    assert ready(bridge,2,retire_above=1)['retired'] and rows(bridge)==[]
    assert ready(bridge,2,retire_above=1)['retired'] and rows(bridge)==[]
    ready(bridge,2,retire_above=2);assert rows(bridge)==[('one-2',)]

def test_deleted_account_node_fence_survives_restart_and_does_not_touch_other_account(tmp_path):
    bridge,_=fixture(tmp_path);ready(bridge);ready(bridge,2);ready(bridge,account='two')
    assert bridge.retire_account('one')['removed']==2
    again=BridgeStore(bridge.directory)
    with pytest.raises(BridgeError) as error:ready(again)
    assert error.value.status==410
    with pytest.raises(BridgeError):again.enqueue(SPEC,account_id='one')
    assert rows(again)==[('two-1',)] and again.retire_account('one')['removed']==0

def test_retirement_endpoint_is_authenticated_scoped_and_cannot_remove_current_slot(tmp_path):
    bridge,_=fixture(tmp_path);ready(bridge);ready(bridge,2)
    router=create_chrome_bridge_router(lambda:None,store=bridge,pairing_key_loader=lambda:'synthetic-worker-key-123456789',
        account_limits_loader=lambda _: {'enabled':True,'concurrency_limit':1})
    app=FastAPI();app.include_router(router);client=TestClient(app)
    url='/v1/chrome/workers/retire';headers={'X-Chrome-Bridge-Key':'synthetic-worker-key-123456789'}
    assert client.post(url,json={'account_id':'one','worker_id':'one-2'}).status_code==401
    assert client.post(url,json={'account_id':'two','worker_id':'one-2'},headers=headers).status_code==403
    assert client.post(url,json={'account_id':'one','worker_id':'one-1'},headers=headers).status_code==409
    assert client.post(url,json={'account_id':'one','worker_id':'one-2'},headers=headers).json()['retired']
    assert rows(bridge)==[('one-1',)]

def test_admin_delete_calls_node_removal_then_cleans_registration_and_preserves_task_history(tmp_path):
    bridge,clock=fixture(tmp_path);ready(bridge);ready(bridge,2)
    store=PlatformStore('sqlite:///'+str(tmp_path/'platform.sqlite3'),encryption_key=Fernet.generate_key(),worker_status=bridge.accounts)
    store.create_account({'id':'one','name':'One','session_id':'synthetic','capabilities':['seedance-2.0-mini']})
    store.bootstrap_admin('admin','synthetic-password')
    calls=[]
    class Manager:
        async def stop(self,aid):raise AssertionError('Deletion should use full node cleanup')
        async def remove_node(self,aid):calls.append(aid);assert store.get_account(aid)['status']=='deleting'
    app=FastAPI();app.include_router(create_admin_router(store,bridge,managed_workers=Manager()));client=TestClient(app)
    auth=client.post('/api/admin/login',json={'username':'admin','password':'synthetic-password'}).json()
    response=client.delete('/api/admin/accounts/one',headers={'X-CSRF-Token':auth['csrf_token']})
    assert response.status_code==200 and response.json()['nodes_cleanup']['removed']==2
    assert calls==['one'] and rows(bridge)==[] and store.get_account_limits('one')['deleted']
    assert client.delete('/api/admin/accounts/one',headers={'X-CSRF-Token':auth['csrf_token']}).status_code==200
    with pytest.raises(BridgeError):ready(bridge)


def test_account_patch_updates_claim_policy_requests_resize_and_prunes_old_slots(tmp_path):
    bridge,clock=fixture(tmp_path);ready(bridge);ready(bridge,2);clock.now+=91;ready(bridge)
    store=PlatformStore('sqlite:///'+str(tmp_path/'platform.sqlite3'),encryption_key=Fernet.generate_key(),worker_status=bridge.accounts)
    store.create_account({'id':'one','name':'One','session_id':'synthetic','max_concurrent':2})
    assert store.get_account_limits('one')['concurrency_limit']==2
    store.bootstrap_admin('admin','synthetic-password');calls=[]
    class Manager:
        async def reconfigure(self,aid):calls.append(aid);assert store.get_account(aid)['max_concurrent']==1
        def status(self,aid):return {'configuration_pending':True,'effective_concurrency':2,'target_concurrency':1}
    app=FastAPI();app.include_router(create_admin_router(store,bridge,managed_workers=Manager()));client=TestClient(app)
    auth=client.post('/api/admin/login',json={'username':'admin','password':'synthetic-password'}).json()
    response=client.patch('/api/admin/accounts/one',json={'max_concurrent':1},headers={'X-CSRF-Token':auth['csrf_token']})
    assert response.status_code==200 and calls==['one']
    assert response.json()['managed_worker']['configuration_pending']
    assert rows(bridge)==[('one-1',)] and store.get_account_limits('one')['concurrency_limit']==1
