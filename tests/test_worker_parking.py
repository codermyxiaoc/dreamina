"""Idle page disposal/wake tests: synthetic pages, no generation or external I/O."""
import asyncio,json,time,uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright
import dreamina_api.browser_worker as module
from dreamina_api.browser_worker import PAGE_URL,write_json
from test_worker_reliability import worker,job


class Page:
    def __init__(self,url=PAGE_URL):self.url=url;self.closed=False;self.navigations=[]
    async def goto(self,url,**_):self.navigations.append(url);self.url=url
    def is_closed(self):return self.closed
    async def close(self):self.closed=True
    async def bring_to_front(self):pass


def setup(tmp_path):
    owner=worker(tmp_path);owner.entry=Page();slots=owner.prepare_slots()
    control={'stop':asyncio.Event(),'claim_lock':asyncio.Lock(),'claimed':0,'pending_claims':0,'counted_job_ids':set(),'readiness':{'ready':True,'reason':'Ready'}}
    owner._pool_control=control;owner._entry_readiness={'ready':True,'checked_at':time.time()};owner._last_work_at=time.monotonic()-61
    for slot in slots:write_json(slot.journal,{'phase':'idle'})
    async def api(path,body=None,**_):
        if path=='/heartbeat':return {'ready':bool(body['ready'] or body['wakeable'])}
        return {}
    for slot in slots:slot.api=api
    return owner,slots,control


async def close(owner,slots):
    for slot in slots:await slot.http.aclose()
    await owner.http.aclose()


@pytest.mark.parametrize('guard',['active','claim','pending','recent','browser_failed'])
def test_parking_never_touches_active_unresolved_or_recent_work(tmp_path,guard):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0]
        if guard=='active':slot.active=job()
        if guard=='claim':slot._claim_reserved=True
        if guard=='pending':write_json(slot.journal,{'phase':'activation_requested','job':job()})
        if guard=='recent':owner._last_work_at=time.monotonic()
        if guard=='browser_failed':owner._browser_failed=True
        try:
            async with control['claim_lock'],owner._preparation_lock:
                assert await owner._park_idle_pages_locked(control) is False
            assert owner.entry.navigations==[] and not owner._entry_parked
        finally:await close(owner,slots)
    asyncio.run(check())


def test_parked_heartbeat_preserves_real_check_time_and_stop_does_not_offer_wake(tmp_path):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0];bodies=[];stamp=owner._entry_readiness['checked_at']
        async def api(path,body=None,**_):bodies.append(body);return {'ready':body['ready'] or body['wakeable'],'wake_required':False}
        slot.api=api
        try:
            async with control['claim_lock'],owner._preparation_lock:assert await owner._park_idle_pages_locked(control)
            await slot.heartbeat(True)
            body=bodies[-1];assert body['protocol_version']==8 and body['entry_parked'] and body['wakeable']
            assert body['entry_ready'] is False and body['ready'] is False and body['entry_checked_at']==stamp
            await slot.heartbeat(False,'Worker stopped.');assert not bodies[-1]['wakeable']
            owner._entry_readiness['checked_at']=time.time()-121
            await slot.heartbeat(True);assert not bodies[-1]['wakeable']
        finally:await close(owner,slots)
    asyncio.run(check())


def test_parked_slot_claims_on_server_lease_without_waiting_for_periodic_wake(tmp_path):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0];owner._entry_parked=True
        control['readiness']={'ready':False,'reason':'parked'};current=job();calls=[]
        async def api(path,body=None,**_):
            calls.append(path)
            if path=='/heartbeat':return {'ready':True}
            if path=='/claim':return {'job':current}
            return {}
        async def recovered(*_):return True
        async def handle(received):
            assert received is current and slot.active is current
            assert owner._idle_for_parking() is False
            control['stop'].set();slot.active=None;return True
        owner._await_slot_recovery=recovered;slot.api=api;slot.handle=handle
        try:
            await asyncio.wait_for(owner._run_slot(slot,control),2)
            assert calls.count('/claim')==1
        finally:await close(owner,slots)
    asyncio.run(check())


def test_stale_server_lease_requests_wake_without_claim(tmp_path,monkeypatch):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0];owner._entry_parked=True;calls=[]
        control['readiness']={'ready':False,'reason':'parked'}
        async def api(path,body=None,**_):calls.append(path);return {'ready':False,'wake_required':True}
        async def recovered(*_):return True
        async def stop(*_):control['stop'].set()
        owner._await_slot_recovery=recovered;slot.api=api;monkeypatch.setattr(module.asyncio,'sleep',stop)
        try:
            await owner._run_slot(slot,control)
            assert '/claim' not in calls and owner._entry_wake_requested
        finally:await close(owner,slots)
    asyncio.run(check())


def test_task_wakes_entry_after_prepare_marker_and_reloads_retained_page(tmp_path,monkeypatch):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0];events=[];owner._entry_parked=True
        entry=owner.entry;entry.url='about:blank';old_goto=entry.goto
        async def entry_goto(url,**kwargs):events.append('entry_navigation');await old_goto(url,**kwargs)
        entry.goto=entry_goto
        retained=Page('about:blank');slot._retained_page=retained;slot._retained_kind='video';slot._retained_parked=True;slot._retained_bootstrap={'old-record'}
        current=job();current['claim_protocol_version']=8
        class Driver:
            def __init__(self,page,*_,**__):self.page=page;self.bootstrap=set();self.clicked=False;self.request_observed=None
            async def wait_ready(self,**_):events.append('entry_verified' if self.page is entry else 'task_verified');return {'ready':True,'reason':'Ready'}
            async def submit(self,*_,**__):
                assert self.page is retained and self.page.url.startswith(PAGE_URL)
                assert not self.bootstrap;events.append('submitted_fixture');return {'status':'failed','generation_not_activated':True}
            async def diagnostic(self,*_):pass
        async def api(path,body=None,**_):
            if path.endswith('/prepare'):events.append('prepare_marker');return {'can_prepare':True}
            if path.endswith('/report'):return {'status':'failed'}
            if path=='/heartbeat':return {'ready':True}
            raise AssertionError('Unexpected '+path)
        slot.api=api;monkeypatch.setattr(module,'NativeUI',Driver)
        try:
            assert await slot.handle(current)
            assert events.index('prepare_marker')<events.index('entry_navigation')<events.index('entry_verified')<events.index('task_verified')<events.index('submitted_fixture')
            assert retained.navigations and not owner._entry_parked
        finally:await close(owner,slots)
    asyncio.run(check())


def test_failed_wake_never_creates_generation_page_or_activates(tmp_path,monkeypatch):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0];owner._entry_parked=True;current=job();current['claim_protocol_version']=8;calls=[]
        class Driver:
            def __init__(self,*_,**__):pass
            async def wait_ready(self,**_):return {'ready':False,'reason':'Login required'}
        async def api(path,body=None,**_):
            calls.append((path,body))
            if path.endswith('/prepare'):return {'can_prepare':True}
            if path.endswith('/report'):return {'status':'failed'}
            return {'ready':False}
        slot.api=api;monkeypatch.setattr(module,'NativeUI',Driver)
        try:
            assert await slot.handle(current)
            assert not any(path.endswith('/activate') for path,_ in calls)
            reports=[body for path,body in calls if path.endswith('/report')]
            assert len(reports)==1 and reports[0]['status']=='failed'
            assert owner._entry_readiness['ready'] is False
        finally:await close(owner,slots)
    asyncio.run(check())


def test_monitor_parks_then_periodically_wakes_with_real_new_check(tmp_path,monkeypatch):
    async def check():
        owner,slots,control=setup(tmp_path);old_stamp=owner._entry_readiness['checked_at'];cycles=0;checks=[]
        class Driver:
            def __init__(self,page,*_,**__):self.page=page
            async def check(self):checks.append(self.page.url);return {'ready':True,'reason':'Ready'}
            async def wait_ready(self,**_):checks.append(self.page.url);return {'ready':True,'reason':'Ready'}
        async def step(*_):
            nonlocal cycles
            cycles+=1
            if cycles==1:
                assert owner._entry_parked;assert owner.entry.url=='about:blank'
                owner._entry_readiness['checked_at']=time.time()-46
            else:raise asyncio.CancelledError()
        monkeypatch.setattr(module,'NativeUI',Driver);monkeypatch.setattr(module.asyncio,'sleep',step)
        try:
            with pytest.raises(asyncio.CancelledError):await owner._pool_monitor(control)
            assert len(checks)==2 and all(url.startswith(PAGE_URL) for url in checks)
            assert owner._entry_parked and owner._entry_readiness['checked_at']>=old_stamp
        finally:await close(owner,slots)
    asyncio.run(check())


def test_claim_cannot_enter_between_idle_check_and_parking_navigation(tmp_path):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0];entered=asyncio.Event();release=asyncio.Event();events=[];current=job()
        old=owner.entry.goto
        async def goto(url,**kwargs):entered.set();await release.wait();await old(url,**kwargs)
        owner.entry.goto=goto
        async def api(path,body=None,**_):
            if path=='/heartbeat':return {'ready':True}
            if path=='/claim':events.append('claim');assert owner._entry_parked;return {'job':current}
            return {}
        async def recovered(*_):return True
        async def handle(_):events.append('handle');control['stop'].set();slot.active=None;return True
        slot.api=api;slot.handle=handle;owner._await_slot_recovery=recovered
        async def park():
            async with control['claim_lock'],owner._preparation_lock:
                assert await owner._park_idle_pages_locked(control)
        parking=asyncio.create_task(park());await asyncio.wait_for(entered.wait(),1)
        runner=asyncio.create_task(owner._run_slot(slot,control))
        try:
            await asyncio.sleep(.02);assert not events
            release.set();await asyncio.wait_for(asyncio.gather(parking,runner),2)
            assert events==['claim','handle']
        finally:
            release.set();parking.cancel();runner.cancel();await asyncio.gather(parking,runner,return_exceptions=True);await close(owner,slots)
    asyncio.run(check())


def test_wake_is_charged_to_existing_preparation_deadline_and_cannot_submit_on_timeout(tmp_path):
    async def check():
        owner,slots,control=setup(tmp_path);slot=slots[0];owner._entry_parked=True;slot.config['preparation_timeout_seconds']=.04
        current=job();current['claim_protocol_version']=8;calls=[]
        async def slow(*_,**__):await asyncio.Event().wait()
        async def api(path,body=None,**_):
            calls.append((path,body));return {'can_prepare':True} if path.endswith('/prepare') else {'status':'failed'}
        owner.entry.goto=slow;slot.api=api
        try:
            assert await asyncio.wait_for(slot.handle(current),.5)
            assert calls[0][0].endswith('/prepare') and not any(path.endswith('/activate') for path,_ in calls)
            report=next(body for path,body in calls if path.endswith('/report'))
            assert report['status']=='failed' and report['error_code']=='preparation_timeout'
            assert not owner._entry_waking and owner._entry_wake_requested
        finally:await close(owner,slots)
    asyncio.run(check())


def test_real_chromium_parking_disposes_idle_timers_and_wake_builds_fresh_document(tmp_path,monkeypatch):
    async def check():
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Offline Chromium unavailable')
            browser=await pw.chromium.launch(headless=True);context=await browser.new_context(offline=True);calls=[];ticks=[0,0]
            html='<div id="ready">ready</div><script>setInterval(()=>console.debug("fixture-tick"),20)</script>'
            async def route(request):
                assert request.request.url.startswith(PAGE_URL),'Unexpected external request'
                calls.append(request.request.url);await request.fulfill(status=200,content_type='text/html',body=html)
            await context.route('**/*',route)
            entry=await context.new_page();retained=await context.new_page()
            entry.on('console',lambda message:ticks.__setitem__(0,ticks[0]+1) if message.text=='fixture-tick' else None)
            retained.on('console',lambda message:ticks.__setitem__(1,ticks[1]+1) if message.text=='fixture-tick' else None)
            await entry.goto(PAGE_URL);await retained.goto(PAGE_URL)
            owner,slots,control=setup(tmp_path);owner.entry=entry;slots[0].entry=entry;slots[0]._retained_page=retained
            class Ready:
                def __init__(self,page,*_):self.page=page
                async def wait_ready(self,**_):return {'ready':await self.page.locator('#ready').count()==1,'reason':'Ready'}
            monkeypatch.setattr(module,'NativeUI',Ready)
            try:
                await asyncio.sleep(.15);assert min(ticks)>0
                async with control['claim_lock'],owner._preparation_lock:assert await owner._park_idle_pages_locked(control)
                await asyncio.sleep(.08);parked=list(ticks);await asyncio.sleep(.15)
                assert ticks==parked and entry.url==retained.url=='about:blank'
                async with owner._preparation_lock:assert (await owner._wake_entry_locked(control))['ready']
                await asyncio.sleep(.1)
                assert ticks[0]>parked[0] and ticks[1]==parked[1] and len(calls)==3
                assert retained.url=='about:blank' and slots[0]._retained_parked
            finally:await close(owner,slots);await browser.close()
    asyncio.run(check())
