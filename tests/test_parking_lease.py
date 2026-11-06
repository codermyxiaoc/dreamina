"""Parked browsers require a server-recorded, time-bounded real readiness check."""
from types import SimpleNamespace
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from dreamina_api.chrome_bridge import BridgeStore,BridgeError,create_chrome_bridge_router
from dreamina_api.platform_store import PlatformStore,PlatformError

PAGE='https://dreamina.capcut.com/ai-tool/generate'
SPEC={'model':'seedance-2.0-mini','prompt':'Offline fixture','duration':4,'ratio':'16:9','resolution':'720p','mode':'omni'}

def setup(tmp_path):
    now=SimpleNamespace(value=1000.)
    bridge=BridgeStore(tmp_path/'bridge',clock=lambda:now.value)
    def beat(parked=False,**kw):
        values=dict(ready=not parked,page_url=PAGE,protocol_version=8,driver='native',owner_id='owner',slot_index=1,slot_count=1,
            entry_ready=not parked,entry_checked_at=now.value,entry_parked=parked,wakeable=parked)
        values.update(kw);return bridge.heartbeat('one','slot',**values)
    def claim():return bridge.claim('one','slot',request_id=str(uuid.uuid4()))
    return bridge,now,beat,claim

def test_first_parked_heartbeat_cannot_create_readiness_proof(tmp_path):
    bridge,now,beat,claim=setup(tmp_path);bridge.enqueue(SPEC,account_id='one')
    result=beat(True);assert not result['ready'] and result['wake_required']
    assert not bridge.accounts()[0]['workers'][0]['wakeable']
    with pytest.raises(BridgeError):claim()

def test_recent_real_check_allows_parked_claim_without_forging_entry_ready(tmp_path):
    bridge,now,beat,claim=setup(tmp_path);beat();now.value+=60
    result=beat(True,entry_checked_at=1000)
    assert result['ready'] and not result['wake_required']
    node=bridge.accounts()[0]['workers'][0]
    assert node['ready'] and node['wakeable'] and node['entry_parked'] and not node['entry_ready']
    assert node['last_entry_ready_at']==node['entry_checked_at']==1000
    task=bridge.enqueue(SPEC,account_id='one');assert claim()['id']==task['id']

def test_parked_heartbeats_and_fabricated_timestamps_cannot_renew_lease(tmp_path):
    bridge,now,beat,claim=setup(tmp_path);beat();now.value=1100
    assert beat(True)['ready']
    now.value=1121
    result=beat(True,entry_checked_at=1121)
    assert not result['ready'] and result['wake_required']
    assert bridge.accounts()[0]['workers'][0]['last_entry_ready_at']==1000
    with pytest.raises(BridgeError):claim()
    assert beat()['ready'];now.value+=1;assert beat(True,entry_checked_at=1121)['ready']

def test_expiry_is_enforced_on_reads_and_claim_even_before_next_heartbeat(tmp_path):
    bridge,now,beat,claim=setup(tmp_path);beat();now.value=1100;beat(True,entry_checked_at=1000)
    now.value=1121;assert not bridge.accounts()[0]['workers'][0]['ready']
    with pytest.raises(BridgeError):claim()

def test_failed_real_check_invalidates_cached_wake_permission(tmp_path):
    bridge,now,beat,claim=setup(tmp_path);beat();now.value+=1
    beat(False,ready=False,entry_ready=False)
    assert not beat(True)['ready']

def test_disabled_account_cannot_use_wakeable_to_bypass_admission(tmp_path):
    bridge,now,beat,claim=setup(tmp_path);beat()
    app=FastAPI();router=create_chrome_bridge_router(lambda:None,store=bridge,pairing_key_loader=lambda:'synthetic-key-123456789012345',
        account_limits_loader=lambda _: {'enabled':False,'concurrency_limit':1})
    app.include_router(router);client=TestClient(app)
    body=dict(account_id='one',worker_id='slot',ready=False,page_url=PAGE,protocol_version=8,driver='native',owner_id='owner',slot_index=1,slot_count=1,
        entry_ready=False,entry_checked_at=1000,entry_parked=True,wakeable=True)
    response=client.post('/v1/chrome/heartbeat',json=body,headers={'X-Chrome-Bridge-Key':'synthetic-key-123456789012345'})
    assert response.status_code==200 and not response.json()['ready']
    with pytest.raises(BridgeError):claim()

def test_platform_can_schedule_a_verified_parked_account_but_not_expired_one(tmp_path):
    bridge,now,beat,claim=setup(tmp_path);beat()
    store=PlatformStore('sqlite:///'+str(tmp_path/'platform.sqlite3'),encryption_key=Fernet.generate_key(),clock=lambda:now.value,worker_status=bridge.accounts)
    store.create_account({'id':'one','name':'One','session_id':'synthetic','capabilities':['seedance-2.0-mini']})
    principal=store.authenticate_key(store.create_key({'name':'fixture'})['key'])
    now.value+=60;beat(True,entry_checked_at=1000)
    task=store.reserve_task(principal,'video',SPEC['model'],SPEC);store.update_task(task['id'],status='failed')
    now.value=1121
    with pytest.raises(PlatformError):store.reserve_task(principal,'video',SPEC['model'],SPEC)
