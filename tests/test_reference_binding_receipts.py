"""Observed reference identities persist only with their owned submission receipt."""
import asyncio
from copy import deepcopy
import json
import sqlite3
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dreamina_api.browser_worker import Worker,write_json
from dreamina_api.chrome_bridge import BridgeStore,BridgeError,create_chrome_bridge_router

IMAGE='asset_0_'+'a'*24
VIDEO='asset_1_'+'b'*24
BINDINGS={IMAGE:{'kind':'image','resource_id':'tos-useast1/i-abc123.jpg'},
          VIDEO:{'kind':'video','resource_id':'v0_synthetic-123'}}


def request():
    return {'model':'seedance-2.0-mini','prompt':'synthetic reference test','duration':4,
            'ratio':'16:9','resolution':'720p','mode':'omni','asset_owner_task_id':'video_'+'a'*32,
            'references':[
                {'asset_id':IMAGE,'kind':'image','width':512,'height':512,'format':'jpeg','size_bytes':100,'sha256':'a'*64},
                {'asset_id':VIDEO,'kind':'video','width':640,'height':640,'format':'mp4','size_bytes':100,'sha256':'b'*64,'duration':2000,'fps':24},
            ]}


def fixture(tmp_path):
    store=BridgeStore(tmp_path/'bridge',clock=lambda:1000)
    store.enqueue(request(),account_id='one')
    store.heartbeat('one','native-fixture',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',driver='native')
    job=store.claim('one','native-fixture')
    return store,job,str(uuid.uuid4())


def report(store,job,sid,*,bindings=BINDINGS,status='submitted'):
    return store.report(job['id'],job['worker_id'],job['claim_token'],status=status,
                        submit_id=sid,reference_bindings=bindings)


def test_binding_receipt_survives_restart_and_reconciliation(tmp_path):
    store,job,sid=fixture(tmp_path)
    saved=report(store,job,sid,status='uncertain')
    assert saved['reference_bindings']==BINDINGS and saved['submit_id']==sid
    reopened=BridgeStore(store.directory,clock=lambda:1000)
    assert reopened.get(job['id'])['reference_bindings']==BINDINGS
    assert report(reopened,job,sid)['reference_bindings']==BINDINGS
    # Omitting the optional map never erases an existing bound identity.
    assert report(reopened,job,sid,bindings=None)['reference_bindings']==BINDINGS
    complete=reopened.reconcile_submitted(job['id'],{'id':sid,'status':'completed','videos':[{'url':'https://media.example/video.mp4'}]})
    assert complete['reference_bindings']==BINDINGS


def test_binding_map_and_submit_id_are_immutable_after_first_observation(tmp_path):
    store,job,sid=fixture(tmp_path);report(store,job,sid)
    assert report(store,job,sid,bindings=dict(reversed(list(BINDINGS.items()))))['reference_bindings']==BINDINGS
    changed=deepcopy(BINDINGS);changed[IMAGE]['resource_id']='other-resource'
    with pytest.raises(BridgeError,match='reference bindings'):report(store,job,sid,bindings=changed)
    with pytest.raises(BridgeError,match='submit_id'):report(store,job,str(uuid.uuid4()))
    assert store.get(job['id'])['reference_bindings']==BINDINGS


@pytest.mark.parametrize('status,sid',[('failed',str(uuid.uuid4())),('uncertain',None),('submitted',None)])
def test_bindings_require_explicit_id_and_accepted_report_status(tmp_path,status,sid):
    store,job,_=fixture(tmp_path)
    with pytest.raises(ValueError,match='explicit submit_id'):report(store,job,sid,status=status)
    assert store.get(job['id'])['reference_bindings'] is None


@pytest.mark.parametrize('change',[
    lambda b:{},
    lambda b:{IMAGE:b[IMAGE]},
    lambda b:{**b,'asset_2_'+'c'*24:{'kind':'image','resource_id':'extra'}},
    lambda b:{**b,IMAGE:{'kind':'video','resource_id':'wrong-kind'}},
    lambda b:{**b,IMAGE:{'kind':'image','resource_id':'ok','cookie':'secret'}},
    lambda b:{**b,IMAGE:{'kind':'image','resource_id':'https://example.test/signed?token=secret'}},
    lambda b:{**b,IMAGE:{'kind':'image','resource_id':'sessionid=secret'}},
    lambda b:{**b,IMAGE:{'kind':'image','resource_id':'\nsecret'}},
    lambda b:{**b,IMAGE:{'kind':'image','resource_id':'x'*1025}},
    lambda b:{**b,IMAGE:{'kind':'image','resource_id':True}},
])
def test_malformed_wrong_job_or_credential_shaped_bindings_rejected(tmp_path,change):
    store,job,sid=fixture(tmp_path)
    with pytest.raises(ValueError):report(store,job,sid,bindings=change(deepcopy(BINDINGS)))
    row=store.get(job['id']);assert row['submit_id'] is None and row['reference_bindings'] is None


def test_terminal_receipt_cannot_gain_or_change_bindings(tmp_path):
    store,job,sid=fixture(tmp_path);report(store,job,sid,bindings=None)
    store.reconcile_submitted(job['id'],{'id':sid,'status':'failed'})
    with pytest.raises(BridgeError,match='terminal'):report(store,job,sid)
    assert store.get(job['id'])['reference_bindings'] is None
    # Legacy idempotent failure acknowledgement remains accepted unchanged.
    assert report(store,job,sid,status='failed',bindings=None)['status']=='failed'


def test_unowned_claim_and_queue_input_cannot_supply_bindings(tmp_path):
    store,job,sid=fixture(tmp_path)
    with pytest.raises(BridgeError) as error:
        store.report(job['id'],'other-worker',job['claim_token'],status='submitted',submit_id=sid,reference_bindings=BINDINGS)
    assert error.value.status==403
    with pytest.raises(ValueError):store.enqueue(request()|{'reference_bindings':BINDINGS})
    assert store.get(job['id'])['reference_bindings'] is None


def test_old_database_column_migrates_without_altering_existing_job(tmp_path):
    store,job,sid=fixture(tmp_path);report(store,job,sid,bindings=None)
    with sqlite3.connect(store.path) as db:db.execute('ALTER TABLE jobs DROP COLUMN reference_bindings_json')
    reopened=BridgeStore(store.directory,clock=lambda:1000)
    old=reopened.get(job['id'])
    assert old['status']=='submitted' and old['submit_id']==sid and old['reference_bindings'] is None
    assert report(reopened,job,sid)['reference_bindings']==BINDINGS
    assert BridgeStore(store.directory,clock=lambda:1000).get(job['id'])['reference_bindings']==BINDINGS


def test_report_http_and_owned_status_roundtrip_bindings(tmp_path):
    store,job,sid=fixture(tmp_path)
    bridge_key='fixture-key-'+'x'*32
    app=FastAPI();app.include_router(create_chrome_bridge_router(lambda:None,store=store,pairing_key_loader=lambda:bridge_key))
    body={'worker_id':job['worker_id'],'claim_token':job['claim_token'],'status':'submitted','submit_id':sid,'reference_bindings':BINDINGS}
    with TestClient(app) as client:
        headers={'X-Chrome-Bridge-Key':bridge_key}
        r=client.post('/v1/chrome/jobs/'+job['id']+'/report',json=body,headers=headers)
        assert r.status_code==200 and r.json()['reference_bindings']==BINDINGS
        r=client.post('/v1/chrome/jobs/'+job['id']+'/status',json={k:body[k] for k in ('worker_id','claim_token')},headers=headers)
        assert r.status_code==200 and r.json()['reference_bindings']==BINDINGS
        bad=deepcopy(body);bad['reference_bindings'][IMAGE]['resource_id']='x'*1025
        assert client.post('/v1/chrome/jobs/'+job['id']+'/report',json=bad,headers=headers).status_code==422


def worker(tmp_path):
    return Worker({'server_url':'https://fixture.test','worker_key':'fixture-key-'+'x'*32,
                   'account':'one','state_dir':tmp_path,'profile_dir':tmp_path/'profile','prepare_attempts':2})


@pytest.mark.parametrize('phase',['request_observed','reporting','reported'])
def test_worker_recovery_keeps_reference_binding_map_with_original_id(tmp_path,phase):
    async def run():
        w=worker(tmp_path);sid=str(uuid.uuid4());j={'id':str(uuid.uuid4()),'claim_token':'claim','request':request()};calls=[]
        confirmation={'status':'uncertain','submit_id':sid,'reference_bindings':BINDINGS}
        result={'worker_id':w.worker_id,'claim_token':'claim',**confirmation}
        write_json(w.journal,{'phase':phase,'job':j,'confirmation':confirmation,'result':result})
        async def api(path,body=None,**_):calls.append((path,body));return {'status':'uncertain'}
        w.api=api
        try:
            assert await w.recover() is True
            assert w.resume_job[1]['reference_bindings']==BINDINGS
            assert calls[-1][1]['reference_bindings']==BINDINGS
            assert json.loads(w.journal.read_text())['result']['reference_bindings']==BINDINGS
            assert all('/claim' not in path for path,_ in calls)
        finally:await w.http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('remote_binding',[BINDINGS,None,{IMAGE:{'kind':'image','resource_id':'wrong'}}])
@pytest.mark.parametrize('remote_status',['completed','submitted'])
def test_lost_ack_checks_both_original_id_and_persisted_binding(tmp_path,remote_binding,remote_status):
    async def run():
        w=worker(tmp_path);sid=str(uuid.uuid4());j={'id':str(uuid.uuid4()),'claim_token':'claim'}
        async def api(path,body=None,**_):
            if path.endswith('/report'):raise RuntimeError('Worker API HTTP 409; request values omitted.')
            return {'status':remote_status,'submit_id':sid,'reference_bindings':remote_binding}
        w.api=api
        try:
            if remote_binding==BINDINGS:await w.report(j,{'status':'uncertain','submit_id':sid,'reference_bindings':BINDINGS})
            else:
                with pytest.raises(RuntimeError,match='409'):await w.report(j,{'status':'uncertain','submit_id':sid,'reference_bindings':BINDINGS})
            assert json.loads(w.journal.read_text())['phase']==('reported' if remote_binding==BINDINGS else 'reporting')
        finally:await w.http.aclose()
    asyncio.run(run())


def test_submitted_recovery_cannot_ignore_missing_local_binding_map(tmp_path):
    async def run():
        w=worker(tmp_path);sid=str(uuid.uuid4());j={'id':str(uuid.uuid4()),'claim_token':'claim'}
        async def api(path,body=None,**_):
            if path.endswith('/report'):raise RuntimeError('Worker API HTTP 409; request values omitted.')
            return {'status':'submitted','submit_id':sid,'reference_bindings':BINDINGS}
        w.api=api
        try:
            with pytest.raises(RuntimeError,match='409'):await w.report(j,{'status':'uncertain','submit_id':sid})
            assert json.loads(w.journal.read_text())['phase']=='reporting'
        finally:await w.http.aclose()
    asyncio.run(run())


def test_ambiguous_receipt_does_not_recover_bindings_for_a_candidate_id(tmp_path):
    async def run():
        w=worker(tmp_path);j={'id':str(uuid.uuid4()),'claim_token':'claim'};calls=[]
        confirmation={'submit_id':str(uuid.uuid4()),'reference_bindings':BINDINGS,'observed_submit_ids':[str(uuid.uuid4()),str(uuid.uuid4())]}
        write_json(w.journal,{'phase':'submission_ambiguous','job':j,'confirmation':confirmation})
        async def api(path,body=None,**_):calls.append((path,body));return {'status':'uncertain'}
        w.api=api
        try:
            assert await w.recover() is False
            assert 'submit_id' not in calls[-1][1] and 'reference_bindings' not in calls[-1][1]
            assert w.resume_job is None
        finally:await w.http.aclose()
    asyncio.run(run())
