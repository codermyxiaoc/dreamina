"""Dispatch error fencing with real local stores, no external generation."""
import sqlite3,time,uuid
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dreamina_api.chrome_bridge import BridgeStore,BridgeError
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.openai_api import create_openai_router
from dreamina_api.platform_store import PlatformStore
import dreamina_api.video_inputs as inputs


SPEC={'model':'seedance-2.0-mini','prompt':'Synthetic original ceramic object','seconds':'4','resolution':'720p','ratio':'16:9'}


def fixture(tmp_path):
    store=PlatformStore('sqlite:///'+str(tmp_path/'platform.sqlite3'),encryption_key=Fernet.generate_key())
    store.create_account({'id':'one','name':'One','session_id':'fixture','max_concurrent':1,'max_inflight':1,'capabilities':[SPEC['model']]})
    key=store.create_key({'name':'fixture','max_concurrent':1,'request_limit':10})
    bridge=BridgeStore(tmp_path/'bridge')
    app=FastAPI();app.include_router(create_openai_router(store,bridge,media_directory=tmp_path/'media'))
    return SimpleNamespace(store=store,bridge=bridge,client=TestClient(app),key=key,headers={'Authorization':'Bearer '+key['key'],'Idempotency-Key':'logical-one'})


def post(e,**changes):return e.client.post('/v1/videos',headers=e.headers,json=SPEC|changes)


def bridge_request(task):
    spec=task['request'];request={k:spec[k] for k in ('model','prompt','duration','ratio','resolution','mode','references')}
    return request|{'asset_owner_task_id':task['id'],'new_session':True}


def test_reference_persistence_failure_closes_task_and_allows_next_logical_request(tmp_path,monkeypatch):
    e=fixture(tmp_path);original=inputs.save_references
    def failed(*args):raise OSError('Disk unavailable; cookie=never-output')
    monkeypatch.setattr(inputs,'save_references',failed)
    response=post(e);assert response.status_code==409
    body=response.json();assert body['error']['code']=='dispatch_failed' and 'never-output' not in response.text
    task=e.store.get_task(body['task_id']);assert task['status']=='failed' and task['bridge_job_id']==str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-video-dispatch:'+task['id']))
    assert e.store.get_account('one')['active_tasks']==0 and e.bridge.list_jobs()==[]
    assert e.store.list_keys()[0]['used_requests']==1
    again=post(e);assert again.status_code==200 and again.json()['id']==task['id'] and again.json()['status']=='failed'
    assert e.store.list_keys()[0]['used_requests']==1
    monkeypatch.setattr(inputs,'save_references',original);e.headers['Idempotency-Key']='logical-two'
    following=post(e);assert following.status_code==200 and following.json()['id']!=task['id']
    assert e.store.list_keys()[0]['used_requests']==2


def test_delayed_enqueue_after_missing_job_fence_cannot_be_claimed(tmp_path,monkeypatch):
    e=fixture(tmp_path)
    monkeypatch.setattr(inputs,'save_references',lambda *args:(_ for _ in ()).throw(OSError('before enqueue')))
    result=post(e);task=e.store.get_task(result.json()['task_id'])
    late=e.bridge.enqueue(bridge_request(task),account_id='one',job_id=task['bridge_job_id'])
    assert late['status']=='failed' and late['error_code']=='dispatch_failed'
    e.bridge.heartbeat('one','worker-one',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',protocol_version=5,driver='native',slot_index=1,slot_count=1,owner_id='browser-one')
    assert e.bridge.claim('one','worker-one',request_id=str(uuid.uuid4())) is None
    assert e.store.get_account('one')['active_tasks']==0


def test_enqueue_commits_then_raises_fences_existing_unactivated_job(tmp_path,monkeypatch):
    e=fixture(tmp_path);enqueue=e.bridge.enqueue;seen=[]
    def committed(request,*,account_id,job_id):
        parent=e.store.task_for_bridge(job_id);assert parent and parent['dispatch_claimed']
        seen.append(enqueue(request,account_id=account_id,job_id=job_id))
        raise OSError('reply lost after local enqueue')
    monkeypatch.setattr(e.bridge,'enqueue',committed)
    response=post(e);assert response.status_code==409 and response.json()['error']['code']=='dispatch_failed'
    task=e.store.get_task(response.json()['task_id']);assert task['status']=='failed'
    assert len(seen)==1 and e.bridge.get(seen[0]['id'])['status']=='failed'
    assert e.store.get_account('one')['active_tasks']==0


@pytest.mark.parametrize('marker',['activated','submit_id','uncertain'])
def test_dispatch_exception_after_possible_activation_preserves_uncertainty_and_hold(tmp_path,monkeypatch,marker):
    e=fixture(tmp_path);enqueue=e.bridge.enqueue
    def committed(request,*,account_id,job_id):
        parent=e.store.task_for_bridge(job_id);assert parent is not None
        enqueue(request,account_id=account_id,job_id=job_id)
        e.store.reserve_task_credits(parent['id'],job_id,required_credits=20,balance=100,request_fingerprint=quote_request_fingerprint(request))
        with sqlite3.connect(e.bridge.path) as db:
            if marker=='activated':db.execute("UPDATE jobs SET status='claimed',activation_started_at=? WHERE id=?",(time.time(),job_id))
            elif marker=='submit_id':db.execute("UPDATE jobs SET submit_id=? WHERE id=?",(str(uuid.uuid4()),job_id))
            else:db.execute("UPDATE jobs SET status='uncertain' WHERE id=?",(job_id,))
        raise OSError('ambiguous local dispatch')
    monkeypatch.setattr(e.bridge,'enqueue',committed)
    response=post(e);assert response.status_code==409 and response.json()['error']['code']=='dispatch_uncertain'
    task=e.store.get_task(response.json()['task_id']);assert task['status']=='uncertain'
    assert len(e.store.active_credit_holds())==1 and e.store.active_credit_holds()[0]['amount']==20
    assert e.store.get_account('one')['active_tasks']==1
    e.headers['Idempotency-Key']='logical-two';following=post(e)
    assert following.status_code==429 and e.store.list_keys()[0]['used_requests']==1


def test_parent_is_durably_linked_before_worker_can_observe_claim(tmp_path,monkeypatch):
    e=fixture(tmp_path);enqueue=e.bridge.enqueue;observed=[]
    e.bridge.heartbeat('one','worker-one',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',protocol_version=5,driver='native',slot_index=1,slot_count=1,owner_id='browser-one')
    def instant(request,*,account_id,job_id):
        parent=e.store.task_for_bridge(job_id);assert parent is not None and parent['dispatch_claimed']
        job=enqueue(request,account_id=account_id,job_id=job_id)
        claimed=e.bridge.claim('one','worker-one',request_id=str(uuid.uuid4()))
        assert claimed['id']==job_id and e.store.task_for_bridge(claimed['id'])['id']==parent['id']
        observed.append((parent['id'],job_id));return job
    monkeypatch.setattr(e.bridge,'enqueue',instant)
    response=post(e);assert response.status_code==200
    assert observed==[(response.json()['id'],str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-video-dispatch:'+response.json()['id'])))]


def test_failure_to_persist_parent_link_preserves_unknown_instead_of_falsely_freeing(tmp_path,monkeypatch):
    e=fixture(tmp_path);update=e.store.update_task;triggered=False
    def failing(task_id,**fields):
        nonlocal triggered
        if 'bridge_job_id' in fields and not triggered:triggered=True;raise OSError('DB write failed before link')
        return update(task_id,**fields)
    monkeypatch.setattr(e.store,'update_task',failing)
    response=post(e)
    # A missing-link CAS must not claim the parent was closed when no row changed.
    assert response.status_code==409
    task=e.store.get_task(response.json()['task_id'])
    assert not (response.json()['error']['code']=='dispatch_failed' and task['status']!='failed')


def test_legacy_claim_without_activation_marker_is_not_proof_of_no_submission(tmp_path,monkeypatch):
    e=fixture(tmp_path);enqueue=e.bridge.enqueue
    e.bridge.heartbeat('one','legacy-native',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',protocol_version=4,driver='native')
    def claimed_before_error(request,*,account_id,job_id):
        enqueue(request,account_id=account_id,job_id=job_id)
        claimed=e.bridge.claim('one','legacy-native');assert claimed['id']==job_id
        raise OSError('Legacy native may have clicked')
    monkeypatch.setattr(e.bridge,'enqueue',claimed_before_error)
    response=post(e);assert response.status_code==409 and response.json()['error']['code']=='dispatch_uncertain'
    assert e.store.get_task(response.json()['task_id'])['status']=='uncertain'
