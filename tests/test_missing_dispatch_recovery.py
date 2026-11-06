"""A missing deterministic queue write can fail safely after its deadline."""
import asyncio
from types import SimpleNamespace
import uuid
import threading

from cryptography.fernet import Fernet
import pytest
from fastapi import FastAPI
import httpx

from dreamina_api.chrome_bridge import BridgeStore,BridgeError
from dreamina_api.platform_runtime import PlatformRuntime
from dreamina_api.platform_store import PlatformStore
from dreamina_api.openai_api import create_openai_router

MODEL='seedance-2.0-mini'
SPEC={'model':MODEL,'prompt':'Synthetic dispatch','duration':4,'ratio':'16:9','resolution':'720p','mode':'omni','references':[]}

def fixture(tmp_path,kind='video'):
    clock=SimpleNamespace(now=1000.)
    store=PlatformStore('sqlite:///:memory:',encryption_key=Fernet.generate_key(),clock=lambda:clock.now)
    store.create_account({'id':'a','name':'a','session_id':'synthetic','capabilities':[MODEL]})
    principal=store.authenticate_key(store.create_key({'name':'synthetic'})['key'])
    task=store.reserve_task(principal,kind,MODEL,SPEC)
    job_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-'+kind+'-dispatch:'+task['id']))
    task=store.update_task(task['id'],bridge_job_id=job_id)
    bridge=BridgeStore(tmp_path/'bridge',clock=lambda:clock.now)
    runtime=PlatformRuntime(store,bridge)
    return SimpleNamespace(store=store,bridge=bridge,runtime=runtime,task=task,job_id=job_id,clock=clock,principal=principal)

@pytest.mark.parametrize('kind',['video','image'])
def test_missing_initial_dispatch_waits_original_deadline_then_fences_late_enqueue(tmp_path,kind):
    e=fixture(tmp_path,kind)
    assert asyncio.run(e.runtime.sync_task(e.task))['status']=='queued'
    e.clock.now=1059;assert asyncio.run(e.runtime.sync_task(e.task))['status']=='queued'
    e.clock.now=1060;failed=asyncio.run(e.runtime.sync_task(e.task))
    assert failed['status']=='failed' and failed['error']['code']=='dispatch_failed'
    assert failed['created_at']==e.task['created_at'] and e.store.get_account('a')['active_tasks']==0
    late=e.bridge.enqueue(SPEC,account_id='a',job_id=e.job_id)
    assert late['status']=='failed' and late['error_code']=='dispatch_failed'
    assert e.store.list_keys()[0]['used_requests']==1
    assert e.store.reserve_task(e.principal,'video',MODEL,SPEC)['id']!=e.task['id']

@pytest.mark.parametrize('status',['uncertain','submitted','in_progress'])
def test_unknown_or_accepted_missing_queue_never_releases_parent(tmp_path,status):
    e=fixture(tmp_path);e.store.update_task(e.task['id'],status=status);e.clock.now=2000
    result=asyncio.run(e.runtime.sync_task(e.task));assert result['status']==status
    assert result['error'] is None

def test_legacy_random_dispatch_missing_is_not_assumed_safe(tmp_path):
    e=fixture(tmp_path)
    # Build a separate task rather than replacing an immutable assigned ID.
    e.store.update_task(e.task['id'],status='failed');t=e.store.reserve_task(e.principal,'video',MODEL,SPEC)
    t=e.store.update_task(t['id'],bridge_job_id=str(uuid.uuid4()));e.clock.now=2000
    assert asyncio.run(e.runtime.sync_task(t))['status']=='queued'

def test_general_bridge_failure_is_not_misclassified_as_absence(tmp_path,monkeypatch):
    e=fixture(tmp_path);e.clock.now=2000
    def fail(_):raise BridgeError('Synthetic database unavailable',status=503)
    monkeypatch.setattr(e.bridge,'get',fail)
    with pytest.raises(BridgeError):asyncio.run(e.runtime.sync_task(e.task))
    assert e.store.get_task(e.task['id'])['status']=='queued'

def test_late_enqueue_accepted_before_fence_cannot_be_failed(tmp_path,monkeypatch):
    e=fixture(tmp_path);e.clock.now=1060
    e.bridge.heartbeat('a','synthetic',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate')
    def raced(_):
        e.bridge.enqueue(SPEC,account_id='a',job_id=e.job_id)
        job=e.bridge.claim('a','synthetic')
        e.bridge.report(job['id'],'synthetic',job['claim_token'],status='submitted',submit_id=str(uuid.uuid4()))
        raise BridgeError('A read preceded the racing enqueue',status=404)
    monkeypatch.setattr(e.bridge,'get',raced)
    result=asyncio.run(e.runtime.sync_task(e.task));assert result['status']=='queued'
    assert e.store.get_account('a')['active_tasks']==1


@pytest.mark.parametrize('claimed',[False,True])
def test_queued_admission_without_any_dispatch_id_is_scanned_and_closed(tmp_path,claimed):
    e=fixture(tmp_path);e.store.update_task(e.task['id'],status='failed')
    pending=e.store.reserve_task(e.principal,'video',MODEL,SPEC)
    if claimed:assert e.store.claim_dispatch(pending['id'])
    assert pending['id'] in [t['id'] for t in e.store.reconciliation_tasks()]
    e.clock.now=1059;assert asyncio.run(e.runtime.sync_task(pending))['status']=='queued'
    e.clock.now=1060;closed=asyncio.run(e.runtime.sync_task(pending))
    expected=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-video-dispatch:'+pending['id']))
    assert closed['status']=='failed' and closed['bridge_job_id']==expected and e.store.get_account('a')['active_tasks']==0
    assert e.store.claim_dispatch(pending['id']) is False
    # A handler that had already claimed before its crash can finish only as a
    # fenced failed job; it cannot enqueue an executable generation later.
    e.store.update_task(pending['id'],bridge_job_id=expected)
    assert e.bridge.enqueue(SPEC,account_id='a',job_id=expected)['status']=='failed'


def test_missing_id_compare_and_set_does_not_overwrite_racing_dispatch(tmp_path):
    e=fixture(tmp_path);e.store.update_task(e.task['id'],status='failed')
    pending=e.store.reserve_task(e.principal,'video',MODEL,SPEC)
    dispatch=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-video-dispatch:'+pending['id']))
    current=e.store.update_task(pending['id'],bridge_job_id=dispatch,status='claimed')
    assert e.store.fail_missing_initial_dispatch(pending['id'],dispatch)==current


def test_cancelled_create_after_reservation_thread_commits_cannot_leak_capacity(tmp_path,monkeypatch):
    async def run():
        e=fixture(tmp_path);e.store.update_task(e.task['id'],status='failed')
        raw=e.store.create_key({'name':'cancelled request caller','request_limit':1})
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        original=e.store.reserve_task
        def paused_reservation(*args,**kwargs):
            value=original(*args,**kwargs);entered.set()
            try:assert release.wait(2)
            finally:finished.set()
            return value
        monkeypatch.setattr(e.store,'reserve_task',paused_reservation)
        app=FastAPI();app.include_router(create_openai_router(e.store,e.bridge,sync_task=e.runtime.sync_task,media_directory=tmp_path/'media'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
            create=asyncio.create_task(api.post('/v1/videos',json={'model':MODEL,'prompt':'Synthetic cancellation'},headers={'Authorization':'Bearer '+raw['key']}))
            for _ in range(200):
                if entered.is_set():break
                await asyncio.sleep(.005)
            assert entered.is_set();create.cancel()
            with pytest.raises(asyncio.CancelledError):await create
            release.set()
            for _ in range(200):
                if finished.is_set():break
                await asyncio.sleep(.005)
            assert finished.is_set()
            pending=next(t for t in e.store.list_tasks() if t['key_id']==raw['item']['id'])
            assert pending['status']=='queued' and pending['bridge_job_id'] is None
            e.clock.now=1060
            closed=await e.runtime.sync_task(pending)
            assert closed['status']=='failed' and closed['error']['code']=='dispatch_failed'
            assert e.store.get_account('a')['active_tasks']==0
            assert next(k for k in e.store.list_keys() if k['id']==raw['item']['id'])['used_requests']==1
    asyncio.run(run())
