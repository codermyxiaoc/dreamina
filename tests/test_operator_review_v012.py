"""Authenticated operator review/monitoring routes without upstream requests."""
import json
import uuid
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dreamina_api.admin_api import create_admin_router
from dreamina_api.monitoring import monitoring_snapshot
from dreamina_api.platform_store import PlatformStore,PlatformError


def fixture(tmp_path,reconciler=None,monitor=None):
    store=PlatformStore('sqlite:///'+str(tmp_path/'operator.sqlite3'),encryption_key=Fernet.generate_key())
    store.bootstrap_admin('admin','synthetic-operator-password')
    store.create_account({'id':'one','name':'One','session_id':'never-render-session','capabilities':['seedance-2.0-mini']})
    key=store.create_key({'name':'test'});principal=store.authenticate_key(key['key'])
    task=store.reserve_task(principal,'video','seedance-2.0-mini',{'prompt':'Synthetic original task'})
    job=str(uuid.uuid4());store.update_task(task['id'],bridge_job_id=job,status='claimed')
    store.reserve_task_credits(task['id'],job,required_credits=20,balance=100,request_fingerprint='a'*64)
    task=store.update_task(task['id'],status='uncertain',error={'code':'submission_unconfirmed','message':'Unknown original outcome'})
    app=FastAPI();app.include_router(create_admin_router(store,task_reconciler=reconciler,monitor_loader=monitor))
    client=TestClient(app)
    return SimpleNamespace(store=store,key=key,task=task,client=client)


def login(e):
    r=e.client.post('/api/admin/login',json={'username':'admin','password':'synthetic-operator-password'})
    assert r.status_code==200
    return {'X-CSRF-Token':r.json()['csrf_token']}


def test_review_and_monitor_require_admin_session_and_mutation_csrf(tmp_path):
    calls=[]
    async def callback(*args,**kwargs):calls.append((args,kwargs));return {'status':'uncertain'}
    e=fixture(tmp_path,callback,lambda:{'metrics':{}});url='/api/admin/tasks/'+e.task['id']+'/reconcile'
    assert e.client.post(url,json={}).status_code==401
    assert e.client.get('/api/admin/monitoring').status_code==401
    headers=login(e)
    assert e.client.post(url,json={}).status_code==403
    assert e.client.post(url,json={},headers=headers|{'Origin':'https://evil.example'}).status_code==403
    assert calls==[] and len(e.store.active_credit_holds())==1
    assert e.client.get('/api/admin/monitoring').status_code==200


def test_review_passes_only_validated_original_uuid_and_note_once(tmp_path):
    calls=[]
    async def callback(task_id,**kwargs):calls.append((task_id,kwargs));return {'id':task_id,'status':'uncertain','verification_pending':True}
    e=fixture(tmp_path,callback);headers=login(e);sid=str(uuid.uuid4())
    r=e.client.post('/api/admin/tasks/'+e.task['id']+'/reconcile',json={'submit_id':sid,'note':'Original page checked'},headers=headers)
    assert r.status_code==200 and r.json()['status']=='uncertain'
    assert calls==[(e.task['id'],{'submit_id':sid,'note':'Original page checked'})]
    assert len(e.store.active_credit_holds())==1
    assert r.headers['cache-control']=='no-store'


@pytest.mark.parametrize('body',[
    {'force':True},{'release_credits':True},{'status':'failed'},{'submit_id':'not-uuid'},
    {'submit_id':12},{'submit_id':True},{'submit_id':[]},{'note':'x'*501},{'note':[]},[],None,
])
def test_invalid_or_force_release_input_cannot_invoke_review(tmp_path,body):
    calls=[]
    async def callback(*args,**kwargs):calls.append(args);return {}
    e=fixture(tmp_path,callback);headers=login(e)
    r=e.client.post('/api/admin/tasks/'+e.task['id']+'/reconcile',json=body,headers=headers)
    assert r.status_code==400 and calls==[]
    assert len(e.store.active_credit_holds())==1 and e.store.get_task(e.task['id'])['status']=='uncertain'


def test_review_conflict_or_network_error_does_not_clear_hold_or_leak_diagnostics(tmp_path):
    async def conflict(*args,**kwargs):raise PlatformError('Original outcome remains unknown.',code='review_pending',status=409)
    e=fixture(tmp_path,conflict);headers=login(e);url='/api/admin/tasks/'+e.task['id']+'/reconcile'
    r=e.client.post(url,json={},headers=headers)
    assert r.status_code==409 and r.json()['error']['code']=='review_pending' and len(e.store.active_credit_holds())==1
    assert e.client.delete(url,headers=headers).status_code==405


def test_unavailable_review_returns_bounded_error_without_internal_secrets(tmp_path):
    async def broken(*args,**kwargs):raise RuntimeError('Cookie=private-value Authorization=private-key')
    e=fixture(tmp_path,broken);headers=login(e)
    r=e.client.post('/api/admin/tasks/'+e.task['id']+'/reconcile',json={},headers=headers)
    assert r.status_code==503 and r.json()['error']['code']=='review_unavailable'
    assert 'private-value' not in r.text and 'private-key' not in r.text
    assert len(e.store.active_credit_holds())==1


def test_unconfigured_operations_fail_closed(tmp_path):
    e=fixture(tmp_path);headers=login(e)
    assert e.client.post('/api/admin/tasks/'+e.task['id']+'/reconcile',json={},headers=headers).status_code==503
    assert e.client.get('/api/admin/monitoring').status_code==503


def test_monitoring_snapshot_counts_pending_work_without_secrets_or_fabricated_progress(tmp_path,monkeypatch):
    monkeypatch.setattr('dreamina_api.monitoring.shutil.disk_usage',lambda _:SimpleNamespace(total=100,free=50))
    e=fixture(tmp_path)
    bridge=SimpleNamespace(accounts=lambda:[{'account_id':'one','workers':[{'online':True,'ready':True,'reason':'cookie-secret'}]}])
    workers=SimpleNamespace(status=lambda aid:{'account_id':aid,'state':'stopped','desired_running':True,'restart_circuit_open':False,'cookie':'hidden','reason':'private-session'})
    maintenance=SimpleNamespace(latest={'state':'completed','bytes_reclaimed':100})
    e.store.submission_gate_status=lambda:[]
    snapshot=monitoring_snapshot(e.store,bridge,workers,maintenance)
    assert snapshot['metrics']['uncertain_tasks']==1 and snapshot['metrics']['held_credits']==20
    assert snapshot['metrics']['ready_slots']==1 and snapshot['metrics']['desired_workers']==1 and snapshot['metrics']['crashed_workers']==1
    assert {a['code'] for a in snapshot['alerts']}=={'uncertain_task','worker_restarting'}
    encoded=json.dumps(snapshot)
    assert all(value not in encoded for value in ('never-render-session','cookie-secret','private-session','hidden'))
    assert 'progress' not in snapshot['metrics']


def test_monitor_loader_sync_and_async_are_supported(tmp_path):
    async def loader():return {'metrics':{'queued_tasks':1},'alerts':[]}
    e=fixture(tmp_path,monitor=loader);login(e)
    r=e.client.get('/api/admin/monitoring');assert r.status_code==200 and r.json()['metrics']['queued_tasks']==1
