"""Independent account slots, isolated receipts, and bounded worker waits."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

import pytest
import httpx
from playwright.async_api import async_playwright

from dreamina_api.browser_worker import Worker,write_json,safe_config
import dreamina_api.browser_worker as module
from dreamina_api.browser_driver import NativeUI
from dreamina_api.models import model_config
from dreamina_api.payloads import build_generation
from dreamina_api.platform_store import PlatformError
from test_worker_reliability import worker,job,approved_quote
from test_managed_workers import Harness,run


async def quiet(*_):pass


def test_slot_journals_are_independent_and_legacy_owner_is_retained(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=3;legacy=job()
        write_json(owner.journal,{'phase':'request_observed','job':legacy,'confirmation':{'submit_id':str(uuid.uuid4())}})
        slots=owner.prepare_slots()
        try:
            assert len(slots)==3 and len({s.worker_id for s in slots})==3
            assert slots[0].worker_id==owner.worker_id
            assert json.loads(slots[0].journal.read_text())['job']==legacy
            assert json.loads(owner.journal.read_text())['phase']=='migrated'
            write_json(slots[1].journal,{'phase':'preparing','job':job()})
            assert json.loads(slots[0].journal.read_text())['job']==legacy
            assert all(s.journal.parent==tmp_path/'slots'/str(i+1) for i,s in enumerate(slots))
            assert all(not s._owns_context for s in slots)
            assert all(s._preparation_lock is owner._preparation_lock for s in slots)
        finally:
            for s in slots:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_lower_slot_count_keeps_unfinished_retired_receipt_but_never_claims_it(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=3;slots=owner.prepare_slots()
        legacy=job();write_json(slots[2].journal,{'phase':'reported','job':legacy,'result':{'status':'submitted','submit_id':str(uuid.uuid4())}})
        for s in slots:await s.http.aclose()
        owner.config['concurrency']=1;reopened=owner.prepare_slots()
        try:
            assert [s.config['slot_index'] for s in reopened]==[1,3]
            assert reopened[1]._retired and json.loads(reopened[1].journal.read_text())['job']==legacy
            calls=[]
            async def api(path,body=None,**_):calls.append((path,body));return {'status':'completed'}
            retired=reopened[1];retired.api=api
            await owner._run_slot(retired,{'stop':asyncio.Event()})
            assert not any(p=='/claim' for p,b in calls)
            heartbeats=[b for p,b in calls if p=='/heartbeat']
            assert all(b['slot_index']==3 and b['slot_count']==3 and b['ready'] is False for b in heartbeats)
        finally:
            for s in reopened:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_migration_refuses_conflicting_first_slot_history(tmp_path):
    async def check():
        owner=worker(tmp_path)
        write_json(owner.journal,{'phase':'preparing','job':job()})
        write_json(tmp_path/'slots/1/current.json',{'phase':'preparing','job':job()})
        try:
            with pytest.raises(RuntimeError,match='Conflicting'):owner.prepare_slots()
        finally:await owner.http.aclose()
    asyncio.run(check())


def test_each_heartbeat_identifies_its_slot_without_account_cookie(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;slots=owner.prepare_slots();calls=[]
        try:
            async def api(path,body=None,**_):calls.append(body);return {}
            for slot in slots:slot.api=api;await slot.heartbeat(True)
            assert [x['slot_index'] for x in calls]==[1,2]
            assert all(x['slot_count']==2 and x['owner_id']==owner.worker_id and x['protocol_version']==8 for x in calls)
            assert 'cookie' not in json.dumps(calls).lower()
        finally:
            for slot in slots:await slot.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_slot_close_cannot_close_shared_profile_context(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;calls=[]
        async def close():calls.append(True)
        owner.context=SimpleNamespace(close=close)
        slots=owner.prepare_slots()
        try:
            for slot in slots:slot.heartbeat=quiet;await slot.close()
            assert not calls
            owner.heartbeat=quiet;await owner.close();assert calls==[True]
        finally:await owner.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('browser_failure,http_hangs', [('timeout',False),('error',True)])
def test_owned_shutdown_is_bounded_and_http_cleanup_runs_after_browser_failure(tmp_path,monkeypatch,browser_failure,http_hangs):
    async def check():
        owner=worker(tmp_path);await owner.http.aclose();owner.heartbeat=quiet
        write_json(owner.journal,{'phase':'reported','job':job(),'confirmation':{'submit_id':str(uuid.uuid4())}})
        original=owner.journal.read_bytes();calls=[];timeouts=[];actual_wait_for=asyncio.wait_for
        async def bounded(awaitable,timeout):
            timeouts.append(timeout);return await actual_wait_for(awaitable,min(timeout,.01))
        async def browser_close():
            calls.append('browser')
            if browser_failure=='error':raise RuntimeError('private diagnostic must not leak')
            await asyncio.Event().wait()
        async def http_close():
            calls.append('http')
            if http_hangs:await asyncio.Event().wait()
        owner.context=SimpleNamespace(close=browser_close);owner.http=SimpleNamespace(aclose=http_close)
        monkeypatch.setattr(module.asyncio,'wait_for',bounded)
        await actual_wait_for(owner.close(),.2)
        assert calls==['browser','http'] and 10 in timeouts and 5 in timeouts
        saved=json.loads((owner.state_dir/'shutdown.json').read_text())
        assert saved['errors']==(['browser_close_timeout'] if browser_failure=='timeout' else ['browser_close_failed','http_close_timeout'])
        assert 'private diagnostic' not in json.dumps(saved) and owner.journal.read_bytes()==original
    asyncio.run(check())


@pytest.mark.parametrize('field,value',[('concurrency',0),('concurrency',9),('concurrency',True),('preparation_timeout_seconds',0),('generation_timeout_seconds',3601),('submission_timeout_seconds',121)])
def test_slot_configuration_is_bounded(tmp_path,field,value):
    path=tmp_path/'worker.json';path.write_text(json.dumps({'server_url':'http://127.0.0.1:8787','worker_key':'x'*40,'accounts':[{'id':'one',field:value}]}))
    with pytest.raises(ValueError):safe_config(path,'one')


def test_managed_slots_are_capped_and_idle_updates_restart_normally(tmp_path):
    h=Harness(tmp_path,{'NATIVE_MAX_ACCOUNT_SLOTS':2});h.store.accounts['first']['max_concurrent']=8
    run(h.worker.start('first'));paths=h.worker.paths('first')
    cfg=safe_config(paths.config,'first');assert cfg['concurrency']==2
    h.report(effective_concurrency=2,active_slots=0,attention_slots=0,ready_slots=2,
             slots=[{'slot_index':1,'worker_state':'ready','claim_token':'MUST-NOT-EXPOSE'}])
    status=h.worker.status('first');assert status['server_slot_limit']==2 and status['configured_concurrency']==8
    assert status['effective_concurrency']==2 and 'claim_token' not in json.dumps(status)
    h.store.accounts['first']['max_concurrent']=1
    assert h.worker.status('first')['configuration_pending'] is True
    old=h.processes[1001]
    h.on_sleep=lambda:setattr(old,'returncode',0)
    run(h.worker.scan())
    assert len(h.calls)==2 and safe_config(paths.config,'first')['concurrency']==1


def test_busy_account_configuration_change_waits_without_stopping(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'))
    h.store.accounts['first'].update(max_concurrent=2,active_tasks=1)
    run(h.worker.scan())
    assert len(h.calls)==1 and not h.worker.paths('first').stop.exists()
    assert h.worker.status('first')['configuration_pending'] is True


def test_only_centrally_released_known_tasks_allow_graceful_stop_and_relogin(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'));process=h.processes[1001]
    h.store.accounts['first'].update(active_tasks=1,blocking_tasks=0)
    h.bridge.records=[{'account_id':'first','jobs':{'uncertain':1},'blocking_jobs':0}]
    h.on_sleep=lambda:setattr(process,'returncode',0)
    assert run(h.worker.stop('first'))['state']=='stopped'


@pytest.mark.parametrize('blocker',[None,-1,'0',True])
def test_malformed_safe_stop_evidence_cannot_bypass_active_task_guard(tmp_path,blocker):
    h=Harness(tmp_path);run(h.worker.start('first'));h.store.accounts['first'].update(active_tasks=1,blocking_tasks=blocker)
    with pytest.raises(PlatformError,match='unfinished tasks'):run(h.worker.stop('first'))
    assert not h.worker.paths('first').stop.exists()


def test_preparation_timeout_fails_before_activation_and_closes_only_its_page(tmp_path,monkeypatch):
    async def check():
        w=worker(tmp_path);w.config['preparation_timeout_seconds']=.02;calls=[];pages=[]
        class Page:
            closed=False
            async def bring_to_front(self):pass
            async def goto(self,*_,**__):await asyncio.sleep(5)
            async def close(self):self.closed=True
        async def new_page():p=Page();pages.append(p);return p
        async def api(path,body=None,**_):calls.append((path,body));return {'ok':True}
        class Driver:
            def __init__(self,*_,**__):self.clicked=False;self.request_observed=None
            async def diagnostic(self,*_):pass
        w.context=SimpleNamespace(new_page=new_page);w.api=api;monkeypatch.setattr(module,'NativeUI',Driver)
        try:
            assert await w.handle(job())
            report=next(body for path,body in calls if path.endswith('/report'))
            assert report['status']=='failed' and report['error_code']=='preparation_timeout' and report['generation_not_activated'] is True
            assert len(pages)==1 and pages[0].closed
            assert not any(path.endswith('/activate') for path,body in calls)
        finally:await w.http.aclose()
    asyncio.run(check())


def test_activation_response_loss_is_not_retried_as_a_new_submission(tmp_path,monkeypatch):
    async def check():
        w=worker(tmp_path);reports=[];activate=[];j=job()
        class Page:
            async def bring_to_front(self):pass
            async def goto(self,*_,**__):pass
        class Driver:
            def __init__(self,*_,**__):self.clicked=False;self.request_observed=None
            async def diagnostic(self,*_):pass
            async def wait_ready(self):return {'ready':True}
            async def submit(self,spec,files,before_click,**kwargs):
                await kwargs['on_preflight'](approved_quote(spec));await before_click()
                raise AssertionError('Lost activation acknowledgement must never click')
        async def api(path,body=None,**_):
            if path.endswith('/preflight'):return {'allowed':True}
            if path.endswith('/activate'):
                activate.append(body);assert json.loads(w.journal.read_text())['phase']=='activation_requested';raise RuntimeError('Worker API HTTP 503; request values omitted.')
            if path.endswith('/report'):reports.append(body)
            return {}
        async def page():return Page()
        async def files(_):return []
        w.context=SimpleNamespace(new_page=page);w.api=api;w.files=files;monkeypatch.setattr(module,'NativeUI',Driver)
        try:
            assert await w.handle(j) is False and len(activate)==1
            assert reports[0]['status']=='uncertain' and reports[0]['generation_not_activated'] is False
        finally:await w.http.aclose()
    asyncio.run(check())


def test_known_id_timeout_detaches_only_after_exact_central_release(tmp_path):
    async def check():
        w=worker(tmp_path);j=job();sid=str(uuid.uuid4());calls=[];closed=[]
        async def api(path,body=None,**_):
            calls.append((path,body))
            if path.endswith('/report'):return {}
            return {'status':'uncertain','submit_id':sid,'slot_released':any(p.endswith('/report') for p,b in calls),'error_code':'generation_timeout'}
        async def close():closed.append(True)
        w.api=api;page=SimpleNamespace(close=close)
        try:
            assert await w.wait_original(j,{'status':'submitted','submit_id':sid},page=page,timeout=.01)
            report=next(body for p,body in calls if p.endswith('/report'))
            assert report['error_code']=='generation_timeout' and report['submit_id']==sid
            assert closed and json.loads(w.journal.read_text())['detached'] is True
            assert json.loads((w.state_dir/'history'/(j['id']+'.json')).read_text())['confirmation']['submit_id']==sid
            assert not any(p=='/claim' for p,b in calls)
        finally:await w.http.aclose()
    asyncio.run(check())


def test_slot_failure_is_recovered_without_stopping_sibling_or_followup_task(tmp_path,monkeypatch):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;owner.entry=object();owner.context=object()
        assigned=[];completed=[];rejected=[];active=set();peak=0;both=asyncio.Event()
        jobs=[job() for _ in range(3)]
        class EntryDriver:
            def __init__(self,*_,**__):pass
            async def check(self):return {'ready':True,'reason':'Ready'}
        monkeypatch.setattr(module,'NativeUI',EntryDriver)
        async def handle(slot,value):
            nonlocal peak
            slot.active=value;write_json(slot.journal,{'phase':'preparing','job':value})
            active.add(value['id']);peak=max(peak,len(active))
            if len(active)==2:both.set()
            if value is jobs[0]:
                await both.wait();active.remove(value['id']);raise RuntimeError('Synthetic one-slot failure before activation')
            await asyncio.sleep(.1);active.remove(value['id']);completed.append(value['id'])
            write_json(slot.journal,{'phase':'finished','job_id':value['id']});slot.active=None;return True
        monkeypatch.setattr(Worker,'handle',handle)
        original=owner.prepare_slots
        def slots():
            values=original()
            for slot in values:
                async def api(path,body=None,_slot=slot,**_):
                    if path=='/heartbeat':return {}
                    if path=='/claim':
                        value=jobs[len(assigned)];assigned.append(value['id']);return {'job':value}
                    if path.endswith('/status'):return {'status':'claimed'}
                    if path.endswith('/report'):rejected.append(body);return {}
                    raise AssertionError(path)
                slot.api=api
            return values
        owner.prepare_slots=slots
        try:
            await asyncio.wait_for(owner.run_pool(max_jobs=3),5)
            assert peak==2 and len(assigned)==3
            assert set(completed)=={jobs[1]['id'],jobs[2]['id']}
            assert len(rejected)==1 and rejected[0]['status']=='failed' and rejected[0]['generation_not_activated'] is True
        finally:await owner.http.aclose()
    asyncio.run(check())


def test_tab_creation_focus_and_navigation_cannot_interrupt_another_preparation(tmp_path,monkeypatch):
    async def check():
        owner=worker(tmp_path);owner.config.update(concurrency=2,preparation_timeout_seconds=10,generation_timeout_seconds=10)
        first_entered=asyncio.Event();release_first=asyncio.Event();pages=[];operations=[];ui_active=set();reports={};files_seen=[]
        jobs=[job(),job()];ids={value['id']:str(uuid.uuid4()) for value in jobs};jobs[0]['request']['prompt']='first';jobs[1]['request']['prompt']='second'
        class Page:
            def __init__(self,index):self.index=index;self.closed=False
            async def bring_to_front(self):
                assert not ui_active,'A different page was brought forward during UI preparation'
                operations.append(('front',self.index))
            async def goto(self,*_,**__):
                assert not ui_active,'A different page navigated during UI preparation'
                operations.append(('goto',self.index))
            async def close(self):self.closed=True
        async def new_page():
            assert not ui_active,'A different page was created during UI preparation'
            p=Page(len(pages));pages.append(p);operations.append(('create',p.index));return p
        owner.context=SimpleNamespace(new_page=new_page)
        class Driver:
            def __init__(self,page,*_,**__):self.page=page;self.clicked=False;self.request_observed=None
            async def wait_ready(self):return {'ready':True}
            async def diagnostic(self,*_):pass
            async def progress(self,*_):return 50
            async def submit(self,spec,files,before_click,on_request_observed=None,on_preflight=None):
                assert not ui_active;ui_active.add(spec['prompt']);operations.append(('submit',self.page.index))
                if spec['prompt']=='first':first_entered.set();await release_first.wait()
                await on_preflight(approved_quote(spec));await before_click();self.clicked=True
                selected=next(value for value in jobs if value['request']['prompt']==spec['prompt'])
                self.request_observed={'submit_id':ids[selected['id']],'api_origin':'https://dreamina-api.us.capcut.com'}
                on_request_observed(self.request_observed);ui_active.remove(spec['prompt'])
                return dict(self.request_observed,status='submitted')
        monkeypatch.setattr(module,'NativeUI',Driver)
        slots=owner.prepare_slots()
        for slot in slots:
            async def files(value):files_seen.append(value['id']);return []
            async def api(path,body=None,**_):
                if path.endswith('/preflight'):return {'allowed':True}
                if path.endswith('/activate'):return {'can_activate':True}
                if path.endswith('/report'):reports[path.split('/')[2]]=body;return {}
                if path.endswith('/status'):
                    # The first task cannot finish until the second has also
                    # submitted: holding the gate for generation would deadlock.
                    return {'status':'completed' if len(reports)==2 else 'submitted'}
                if path.endswith('/telemetry'):return {}
                raise AssertionError(path)
            slot.files=files;slot.api=api
        first=asyncio.create_task(slots[0].handle(jobs[0]));await first_entered.wait()
        second=asyncio.create_task(slots[1].handle(jobs[1]))
        try:
            await asyncio.sleep(.03)
            assert len(files_seen)==2 and len(pages)==1
            assert operations==[('create',0),('front',0),('goto',0),('submit',0)]
            release_first.set()
            assert await asyncio.wait_for(asyncio.gather(first,second),6)==[True,True]
            assert len(reports)==2 and all(page.closed for page in pages)
            assert operations[4:]==[('create',1),('front',1),('goto',1),('submit',1)]
        finally:
            release_first.set()
            for task in (first,second):
                if not task.done():task.cancel()
            await asyncio.gather(first,second,return_exceptions=True)
            for slot in slots:await slot.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_lost_claim_quarantines_that_slot_without_reclaiming_or_stopping_other_slot(tmp_path,monkeypatch):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;owner.entry=object();owner.context=object()
        claims={1:0,2:0};completed=[]
        class EntryDriver:
            def __init__(self,*_,**__):pass
            async def check(self):return {'ready':True,'reason':'Ready'}
        monkeypatch.setattr(module,'NativeUI',EntryDriver)
        async def handle(slot,value):
            completed.append(value['id']);write_json(slot.journal,{'phase':'finished','job_id':value['id']});slot.active=None
            if len(completed)==2:write_json(owner.state_dir/'stop.request',{'stop':True})
            return True
        monkeypatch.setattr(Worker,'handle',handle)
        original=owner.prepare_slots
        def slots():
            values=original()
            for slot in values:
                async def api(path,body=None,_slot=slot,**_):
                    if path=='/heartbeat':return {}
                    if path=='/claim':
                        index=_slot.config['slot_index'];claims[index]+=1
                        if index==1:raise RuntimeError('Synthetic lost claim response')
                        return {'job':job()}
                    raise AssertionError(path)
                slot.api=api
            return values
        owner.prepare_slots=slots
        try:
            await asyncio.wait_for(owner.run_pool(max_jobs=3),8)
            assert claims=={1:1,2:2} and len(completed)==2
            assert json.loads(owner._slots[0].journal.read_text())['phase']=='claiming'
        finally:await owner.http.aclose()
    asyncio.run(check())


def test_lost_claim_recovers_same_owned_job_and_does_not_consume_extra_cli_budget(tmp_path,monkeypatch):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;owner.entry=object();owner.context=object()
        original_job=job();claims=[];recoveries=[];processed=[]
        class EntryDriver:
            def __init__(self,*_,**__):pass
            async def check(self):return {'ready':True,'reason':'Ready'}
        monkeypatch.setattr(module,'NativeUI',EntryDriver)
        async def handle(slot,value):
            assert value['id']==original_job['id']
            processed.append(value['id']);write_json(slot.journal,{'phase':'finished','job_id':value['id']});slot.active=None;return True
        monkeypatch.setattr(Worker,'handle',handle)
        original=owner.prepare_slots
        def slots():
            values=original()
            for slot in values:
                async def api(path,body=None,_slot=slot,**_):
                    if path=='/heartbeat':return {}
                    if path=='/claim':claims.append(body);raise RuntimeError('Synthetic lost claim acknowledgement')
                    if path=='/slots/recover':
                        recoveries.append(body)
                        assert body['claim_request_id']==claims[0]['claim_request_id']
                        return {'job':dict(original_job,status='claimed',worker_id=_slot.worker_id,claim_token='rotated-token',claim_request_id=body['claim_request_id']),'slot_released':False}
                    raise AssertionError(path)
                slot.api=api
            return values
        owner.prepare_slots=slots
        try:
            await asyncio.wait_for(owner.run_pool(max_jobs=1),5)
            assert len(claims)==1 and len(recoveries)==1 and processed==[original_job['id']]
        finally:await owner.http.aclose()
    asyncio.run(check())


def test_recovery_with_no_owned_claim_requires_explicit_central_release(tmp_path):
    async def check():
        w=worker(tmp_path);write_json(w.journal,{'phase':'claiming','claim_request_id':str(uuid.uuid4())});calls=[]
        async def api(path,body=None,**_):calls.append(path);return {'job':None,'slot_released':True}
        w.api=api
        try:
            assert await w.recover() is True and w.recovered_claim is None
            assert json.loads(w.journal.read_text())['phase']=='idle' and calls==['/slots/recover']
        finally:await w.http.aclose()
    asyncio.run(check())


def test_legacy_lost_claim_without_nonce_never_guesses_or_requests_recovery(tmp_path):
    async def check():
        w=worker(tmp_path);write_json(w.journal,{'phase':'claiming'});calls=[]
        async def api(path,body=None,**_):calls.append(path);return {'job':None,'slot_released':True}
        w.api=api
        try:
            assert await w.recover() is False and not calls
            assert json.loads(w.journal.read_text())['phase']=='claiming'
        finally:await w.http.aclose()
    asyncio.run(check())


def test_rejected_upstream_receipt_is_distinct_from_unconfirmed_failure(tmp_path):
    async def check():
        w=worker(tmp_path);j=job();sid=str(uuid.uuid4());calls=[]
        async def api(path,body=None,**_):calls.append(body);return {}
        w.api=api
        try:
            await w.report(j,{'status':'failed','submit_id':sid,'ret':'-6','error':'Rejected'})
            assert calls[-1]['upstream_ret']=='-6' and calls[-1]['error_code']=='upstream_rejected'
            await w.report(j,{'status':'failed','submit_id':sid,'error':'Unknown failure'})
            assert calls[-1]['status']=='uncertain' and calls[-1]['generation_not_activated'] is False
            assert 'upstream_ret' not in calls[-1]
            await w.report(j,{'status':'failed','generation_not_activated':False,'error':'A plan panel appeared after activation'})
            assert calls[-1]['status']=='uncertain' and calls[-1]['error_code']=='submission_unconfirmed'
        finally:await w.http.aclose()
    asyncio.run(check())


def test_known_preflight_errors_keep_safe_reason_without_headers_or_tokens(tmp_path,monkeypatch):
    async def check():
        w=worker(tmp_path);await w.http.aclose()
        def transport(request):return httpx.Response(503,json={'detail':'Current credit balance unavailable. token=synthetic-secret'})
        w.http=httpx.AsyncClient(base_url='https://fixture.test',transport=httpx.MockTransport(transport))
        async def no_wait(_):pass
        monkeypatch.setattr(module.asyncio,'sleep',no_wait)
        try:
            with pytest.raises(module.WorkerAPIError) as error:await w.api('/jobs/x/preflight')
            assert error.value.error_code=='credit_check_unavailable'
            assert 'Current credit balance unavailable' in error.value.safe_message
            assert 'synthetic-secret' not in str(error.value)
        finally:await w.http.aclose()
    asyncio.run(check())


def test_real_offline_browser_runs_two_task_tabs_without_receipt_mixup(tmp_path,monkeypatch):
    async def check():
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Chromium fixture runtime unavailable')
            browser=await pw.chromium.launch(headless=True);context=await browser.new_context(offline=True)
            owner=worker(tmp_path);owner.config.update(concurrency=2,preparation_timeout_seconds=15,generation_timeout_seconds=10)
            docs=[];outgoing=[];reports={};claimed=[];heartbeat=[];jobs=[]
            for n in range(2):
                spec={'model':'seedance-2.0-mini','prompt':'Synthetic independent cloud '+str(n),'duration':4,'resolution':'720p','ratio':'16:9','mode':'omni','references':[]}
                jobs.append({'id':str(uuid.uuid4()),'account_id':'one','claim_token':'fixture-'+str(n),'request':spec})
            async def route(route):
                req=route.request;path=urlsplit(req.url).path
                if req.resource_type=='document':
                    docs.append(req.frame.page);body=build_generation({**jobs[0]['request'],'prompt':'placeholder'},workspace_id=str(100+len(docs)))['body']
                    html='''<body><button aria-label="New session" onclick="document.querySelector('#prompt').textContent=''">New session</button>
                    <section role="region" aria-label="Generate"><div id="prompt" contenteditable="true" role="textbox" aria-label="Prompt"></div>
                    <div role="combobox" aria-label="Create">AI Video</div><div role="combobox" aria-label="Model">Dreamina Seedance 2.0 Mini</div><div role="combobox">Omni reference</div><button>16:9 720P</button><button>4s</button>
                    <div class="toolbar-actions-fixture"><span class="credit-price-display-fixture"><span class="actual-credits-fixture">20</span></span><button id="generate" aria-label="Generate" data-testid="dreamina.generator.submit">Generate</button></div></section>
                    <script>document.querySelector('#generate').onclick=async()=>{window.clicks=(window.clicks||0)+1;const body=PAYLOAD;body.submit_id=crypto.randomUUID();const draft=JSON.parse(body.draft_content);draft.component_list[0].abilities.gen_video.text_to_video_params.video_gen_inputs[0].prompt=document.querySelector('#prompt').innerText;body.draft_content=JSON.stringify(draft);await fetch('https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});};</script>'''.replace('PAYLOAD',json.dumps(body))
                    await route.fulfill(status=200,content_type='text/html',body=html);return
                if path.endswith('/aigc_draft/generate'):
                    headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type'}
                    if req.method=='OPTIONS':await route.fulfill(status=204,headers=headers);return
                    body=json.loads(req.post_data);outgoing.append(body)
                    await route.fulfill(status=200,headers=headers,content_type='application/json',body=json.dumps({'ret':'0','data':{'aigc_data':{'submit_id':body['submit_id'],'history_record_id':'123'}}}));return
                await route.abort()
            await context.route('**/*',route);entry=await context.new_page();await entry.goto('https://dreamina.capcut.com/ai-tool/generate')
            owner.context=context;owner.entry=entry;owner._owns_context=False
            class OfflineDriver(NativeUI):
                def __init__(self,*args,**kwargs):kwargs['require_bootstrap']=False;super().__init__(*args,**kwargs)
            monkeypatch.setattr(module,'NativeUI',OfflineDriver)
            async def api(slot,path,body=None,**_):
                if path=='/heartbeat':heartbeat.append(body);return {}
                if path=='/claim':
                    if len(claimed)==2:return {'job':None}
                    value=jobs[len(claimed)];claimed.append((slot.worker_id,value['id']));return {'job':value}
                if path.endswith('/preflight'):return {'allowed':True}
                if path.endswith('/activate'):
                    assert json.loads(slot.journal.read_text())['phase']=='activation_requested';return {'can_activate':True}
                if path.endswith('/report'):
                    jid=path.split('/')[2];reports[jid]=deepcopy(body);return {}
                if path.endswith('/status'):
                    jid=path.split('/')[2];return {'status':'completed' if len(reports)==2 else 'submitted','submit_id':reports.get(jid,{}).get('submit_id')}
                if path.endswith('/telemetry'):return {}
                raise AssertionError(path)
            original=owner.prepare_slots
            def slots():
                values=original()
                for slot in values:
                    async def call(path,body=None,_slot=slot,**kwargs):return await api(_slot,path,body,**kwargs)
                    slot.api=call
                return values
            owner.prepare_slots=slots
            try:
                await asyncio.wait_for(owner.run_pool(max_jobs=2),15)
                assert len(reports)==len(outgoing)==2 and len({sid for sid,_ in claimed})==2
                assert len({x['submit_id'] for x in outgoing})==2
                by_prompt={json.loads(x['draft_content'])['component_list'][0]['abilities']['gen_video']['text_to_video_params']['video_gen_inputs'][0]['prompt']:x['submit_id'] for x in outgoing}
                for value in jobs:assert reports[value['id']]['submit_id']==by_prompt[value['request']['prompt']]
                assert all(json.loads(slot.journal.read_text())['phase']=='finished' for slot in owner._slots)
                assert all(h['owner_id']==owner.worker_id and h['slot_count']==2 for h in heartbeat)
                assert not entry.is_closed() and len(context.pages)==1
            finally:await owner.http.aclose();await browser.close()
    asyncio.run(check())
