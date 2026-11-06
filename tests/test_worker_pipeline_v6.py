"""Offline v6 browser lifecycle, pacing and authoritative receipt protection."""
import asyncio
import json
from types import SimpleNamespace
import uuid

import pytest

from dreamina_api.browser_worker import Worker,write_json
import dreamina_api.browser_worker as module
from dreamina_api.browser_driver import NativeUI,recognized_content_notice,CONTENT_CONFIRMATION_NOTICE
from dreamina_api.task_limits import TaskLimits
from test_worker_reliability import worker,job,approved_quote
from test_driver_reliability import browser_fixture,generation_route,html,fetch_body,success,SPEC
from dreamina_api.payloads import build_generation


def install(tmp_path,monkeypatch,*,after_activation=None):
    w=worker(tmp_path);w.config.update(preparation_timeout_seconds=.2,submission_timeout_seconds=1)
    j=job();j['claim_protocol_version']=6;calls=[];pages=[];submitted=[]
    class Page:
        closed=False
        def is_closed(self):return self.closed
        async def bring_to_front(self):pass
        async def goto(self,*_,**__):calls.append('goto')
        async def close(self):self.closed=True
    async def new_page():
        p=Page();pages.append(p);return p
    async def api(path,body=None,**_):
        calls.append(path)
        if path.endswith('/prepare'):return {'can_prepare':True,'preparation_started_at':1000}
        if path.endswith('/submission-turn'):return {'ready':True,'expires_at':2000}
        if path.endswith('/preflight'):return {'allowed':True}
        if path.endswith('/activate'):return {'can_activate':True}
        if path.endswith('/report'):
            submitted.append(body)
            return {'slot_released':True,'status':'submitted','submit_id':body.get('submit_id')}
        return {}
    class Driver:
        def __init__(self,page,*_,**__):
            self.page=page;self.clicked=False;self.request_observed=None;self.bootstrap={'fixture'}
        async def wait_ready(self):return {'ready':True}
        async def diagnostic(self,*_):pass
        async def dispose(self):pass
        async def submit(self,spec,files,before_click,on_request_observed=None,on_preflight=None):
            await on_preflight(approved_quote(spec));await self.before_activation_turn();await before_click();self.clicked=True
            if after_activation is not None:await after_activation()
            self.request_observed={'submit_id':str(uuid.uuid4()),'api_origin':'https://dreamina-api.us.capcut.com'}
            on_request_observed(self.request_observed)
            return dict(self.request_observed,status='submitted')
    w.context=SimpleNamespace(new_page=new_page);w.api=api;monkeypatch.setattr(module,'NativeUI',Driver)
    return w,j,calls,pages,submitted


def test_preparation_starts_after_lock_and_cannot_expire_response_capture(tmp_path,monkeypatch):
    async def run():
        async def respond():await asyncio.sleep(.08)
        w,j,calls,pages,reports=install(tmp_path,monkeypatch,after_activation=respond);w.config['preparation_timeout_seconds']=.03
        await w._preparation_lock.acquire();pending=asyncio.create_task(w.handle(j))
        try:
            await asyncio.sleep(.06)
            assert not any(isinstance(v,str) and v.endswith('/prepare') for v in calls) and not pages
            w._preparation_lock.release()
            assert await asyncio.wait_for(pending,1) is True
            assert len(reports)==1 and reports[0]['status']=='submitted'
            assert not pages[0].closed and json.loads(w.journal.read_text())['detached'] is True
            assert calls.index('/jobs/'+j['id']+'/submission-turn')>calls.index('/jobs/'+j['id']+'/preflight')
        finally:
            if w._preparation_lock.locked():w._preparation_lock.release()
            if not pending.done():pending.cancel()
            await asyncio.gather(pending,return_exceptions=True);await w.http.aclose()
    asyncio.run(run())


def test_cancellation_after_activation_waits_for_original_receipt_and_reports_once(tmp_path,monkeypatch):
    async def run():
        clicked=asyncio.Event();respond=asyncio.Event()
        async def later():clicked.set();await respond.wait()
        w,j,calls,pages,reports=install(tmp_path,monkeypatch,after_activation=later)
        pending=asyncio.create_task(w.handle(j))
        try:
            await clicked.wait();pending.cancel();await asyncio.sleep(.02)
            assert not pending.done() and not reports
            respond.set()
            with pytest.raises(asyncio.CancelledError):await pending
            saved=json.loads(w.journal.read_text())
            assert saved['phase']=='reported' and saved['confirmation']['submit_id']==reports[0]['submit_id']
            assert len(reports)==1 and sum(str(c).endswith('/activate') for c in calls)==1
        finally:respond.set();await asyncio.gather(pending,return_exceptions=True);await w.http.aclose()
    asyncio.run(run())


def test_inner_cancellation_after_activation_never_claims_no_generation(tmp_path,monkeypatch):
    async def run():
        async def interrupted():raise asyncio.CancelledError()
        w,j,calls,pages,reports=install(tmp_path,monkeypatch,after_activation=interrupted)
        try:
            with pytest.raises(asyncio.CancelledError):await w.handle(j)
            assert len(reports)==1 and reports[0]['status']=='uncertain'
            assert reports[0]['generation_not_activated'] is False
            assert reports[0]['submission_window_closed'] is True and pages[0].closed
            saved=json.loads(w.journal.read_text());assert saved['phase']=='reported' and saved['confirmation']['status']=='uncertain'
        finally:await w.http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('close_problem',['raises','still_open','entry_page'])
def test_unknown_close_proof_requires_its_own_page_confirmed_closed(tmp_path,close_problem):
    async def run():
        w=worker(tmp_path);j=job();j['claim_protocol_version']=6
        async def close():
            if close_problem=='raises':raise RuntimeError('Cannot close')
        page=SimpleNamespace(close=close,is_closed=lambda:False)
        if close_problem=='entry_page':w.entry=page
        result={'status':'uncertain','submit_id':str(uuid.uuid4())}
        try:
            value=await w._quiesce_unknown_page(j,result,page)
            assert 'submission_window_closed' not in value and result==value
        finally:await w.http.aclose()
    asyncio.run(run())


def test_cancellation_before_activation_does_not_submit_and_stops_prepare(tmp_path,monkeypatch):
    async def run():
        w,j,calls,pages,reports=install(tmp_path,monkeypatch)
        await w._preparation_lock.acquire();pending=asyncio.create_task(w.handle(j))
        try:
            await asyncio.sleep(.01);pending.cancel()
            with pytest.raises(asyncio.CancelledError):await pending
            assert len(reports)==1 and reports[0]['status']=='failed' and reports[0]['generation_not_activated'] is True
            assert not pages and not any(str(c).endswith('/activate') for c in calls)
            assert json.loads(w.journal.read_text())['phase']=='finished'
        finally:w._preparation_lock.release();await w.http.aclose()
    asyncio.run(run())


def test_same_slot_reuses_page_only_after_exact_central_release(tmp_path,monkeypatch):
    async def run():
        w,j,calls,pages,reports=install(tmp_path,monkeypatch)
        try:
            assert await w.handle(j)
            second=job();second['claim_protocol_version']=6;second['request']['prompt']='second'
            assert await w.handle(second)
            assert len(pages)==1 and calls.count('goto')==1 and not pages[0].closed
            assert len(reports)==2 and reports[0]['submit_id']!=reports[1]['submit_id']
            assert len(list((w.state_dir/'history').glob('*.json')))==2
        finally:await w.http.aclose()
    asyncio.run(run())


def test_raw_submit_id_does_not_release_without_central_confirmation(tmp_path,monkeypatch):
    async def run():
        w,j,calls,pages,reports=install(tmp_path,monkeypatch);original=w.api;polls=[]
        async def api(path,body=None,**kw):
            if path.endswith('/report'):
                await original(path,body,**kw);return {'status':'submitted','submit_id':body['submit_id'],'slot_released':False}
            if path.endswith('/status'):
                polls.append(True)
                assert not pages[0].closed and w._retained_page is None
                return {'status':'submitted','submit_id':reports[0]['submit_id'],'slot_released':True}
            return await original(path,body,**kw)
        w.api=api
        try:assert await w.handle(j);assert len(polls)==1 and w._retained_page is pages[0]
        finally:await w.http.aclose()
    asyncio.run(run())


def test_wrong_terminal_identity_cannot_release_v6_page(tmp_path):
    async def run():
        w=worker(tmp_path);j=job();j['claim_protocol_version']=6;closed=[]
        async def api(*_,**__):return {'status':'completed','submit_id':str(uuid.uuid4()),'slot_released':True}
        async def close():closed.append(True)
        w.api=api
        try:
            assert await w.wait_original(j,{'status':'submitted','submit_id':str(uuid.uuid4())},page=SimpleNamespace(close=close)) is False
            assert not closed and w._retained_page is None
        finally:await w.http.aclose()
    asyncio.run(run())


def test_entry_refresh_is_idle_only_and_login_failure_stops_advertising(tmp_path,monkeypatch):
    async def run():
        w=worker(tmp_path);w._entry_checked_at=0;w.config['entry_refresh_seconds']=30;seen=asyncio.Event();calls=[]
        async def reload(**kwargs):calls.append('refresh')
        w.entry=SimpleNamespace(reload=reload)
        class Driver:
            def __init__(self,*_,**__):pass
            async def check(self):seen.set();return {'ready':False,'reason':'Login required in this dedicated browser profile.'}
        monkeypatch.setattr(module,'NativeUI',Driver)
        control={'stop':asyncio.Event(),'readiness':{'ready':True}}
        task=asyncio.create_task(w._pool_monitor(control))
        try:
            await asyncio.wait_for(seen.wait(),1);await asyncio.sleep(0)
            assert calls==['refresh'] and control['readiness']['ready'] is False
            assert w._last_status_data['state']=='login_required'
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True);await w.http.aclose()
    asyncio.run(run())


def test_graceful_stop_of_legacy_unknown_recovery_preserves_journal_without_new_claim(tmp_path,monkeypatch):
    async def run():
        owner=worker(tmp_path);legacy=job();legacy['claim_protocol_version']=5;calls=[]
        confirmation={'status':'uncertain','error':'Legacy unknown after activation','generation_not_activated':False}
        write_json(owner.journal,{'phase':'reported','job':legacy,'result':confirmation,'confirmation':confirmation})
        owner._claim_reserved=False
        control={'stop':asyncio.Event(),'claim_lock':asyncio.Lock(),'pending_claims':0,'counted_job_ids':set(),'claimed':0}
        async def api(path,body=None,**_):
            calls.append(path)
            if path.endswith('/report'):control['stop'].set()
            return {'status':'uncertain','submit_id':None,'slot_released':False}
        async def short_sleep(_):pass
        owner.api=api;monkeypatch.setattr(module.asyncio,'sleep',short_sleep)
        try:
            assert await owner._await_slot_recovery(owner,control) is False
            saved=json.loads(owner.journal.read_text())
            assert saved['job']['id']==legacy['id'] and saved['phase']=='reported'
            assert saved['confirmation']['status']=='uncertain' and not saved['confirmation'].get('submit_id')
            assert not any(path=='/claim' or path.endswith('/activate') for path in calls)
        finally:await owner.http.aclose()
    asyncio.run(run())


def test_optional_telemetry_failure_does_not_discard_successful_native_receipt():
    async def run():
        prepared=build_generation(SPEC,workspace_id='123456')
        async def broken(_):raise RuntimeError('telemetry unavailable')
        async with browser_fixture() as ctx:
            await generation_route(ctx,html('await '+fetch_body(prepared['body'])+';'),success)
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            driver=NativeUI(page,broken,submission_timeout_ms=1000)
            async def no_op():pass
            result=await driver.submit(SPEC,[],no_op)
            assert result['status']=='submitted' and result['submit_id']==prepared['submit_id']
            assert driver.telemetry_failures>0 and await page.evaluate('window.clicks')==1
    asyncio.run(run())


def test_optional_cancelled_telemetry_child_cannot_discard_native_receipt():
    async def run():
        prepared=build_generation(SPEC,workspace_id='123456')
        async def cancelled(_):raise asyncio.CancelledError()
        async with browser_fixture() as ctx:
            await generation_route(ctx,html('await '+fetch_body(prepared['body'])+';'),success)
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            driver=NativeUI(page,cancelled,submission_timeout_ms=1000)
            async def noop():pass
            result=await driver.submit(SPEC,[],noop)
            assert result['status']=='submitted' and driver.response_observed['submit_id']==prepared['submit_id']
            assert driver.telemetry_failures>0
    asyncio.run(run())


def test_reused_page_verifies_reference_controls_cleared_before_next_submission():
    async def run():
        prepared=build_generation(SPEC,workspace_id='123456');document=html('await '+fetch_body(prepared['body'])+';')
        # Session reset clears both original prompt and visible reference cards.
        document=document.replace("window.sessions=(window.sessions||0)+1", "document.querySelectorAll('.old-ref').forEach(e=>e.remove());window.sessions=(window.sessions||0)+1")
        async with browser_fixture() as ctx:
            await generation_route(ctx,document,success)
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            async def noop(*_):pass
            first=NativeUI(page,noop,submission_timeout_ms=1000)
            assert (await first.submit(SPEC,[],noop))['status']=='submitted'
            await page.evaluate("document.body.insertAdjacentHTML('beforeend','<button class=old-ref>Remove · Reference</button>')")
            second=NativeUI(page,noop,submission_timeout_ms=1000)
            assert (await second.submit(SPEC,[],noop))['status']=='submitted'
            assert await page.locator('.old-ref').count()==0 and await page.evaluate('window.sessions')==2
            assert await page.evaluate('window.clicks')==2
    asyncio.run(run())


@pytest.mark.parametrize('text',[
    CONTENT_CONFIRMATION_NOTICE,
    'Please confirm that you have the necessary permissions for uploaded media. Do not infringe the rights of others or upload sensitive personal information.',
    'You acknowledge you own rights to reference content, will respect third-party rights and will not upload inappropriate content.',
])
def test_content_notice_accepts_bounded_semantic_wording(text):assert recognized_content_notice(text)


@pytest.mark.parametrize('text',[
    CONTENT_CONFIRMATION_NOTICE+' Upgrade now.',CONTENT_CONFIRMATION_NOTICE+' Verify your identity.',
    CONTENT_CONFIRMATION_NOTICE+' Pay 20 credits.',CONTENT_CONFIRMATION_NOTICE+' Enter password.',
    'Confirm uploaded content.', 'You confirm you own rights to content.',
])
def test_content_notice_does_not_guess_unrelated_or_purchase_confirmation(text):assert not recognized_content_notice(text)


def test_v6_limits_separate_queue_wait_from_preparation_without_resetting_total():
    limits=TaskLimits(queue=60,preparation=180,total=900)
    task={'status':'claimed','created_at':1000}
    job={'status':'claimed','created_at':1000,'claimed_at':1001,'claim_protocol_version':6}
    assert limits.expired(task,job,1059) is None
    assert limits.expired(task,job,1061)[0]=='queue_timeout'
    job['preparation_started_at']=1050
    assert limits.expired(task,job,1190) is None
    assert limits.expired(task,job,1230)[0]=='preparation_timeout'
    assert limits.expired(task,job,1900)[0]=='task_timeout'
