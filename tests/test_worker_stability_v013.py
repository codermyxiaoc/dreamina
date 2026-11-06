"""Offline lifecycle and page reset regressions; no Dreamina or paid calls."""
import asyncio,json,os,time,uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright
from dreamina_api.browser_driver import NativeUI,ReferenceCleanupUnavailable
from dreamina_api.browser_worker import Worker,write_json,profile_lock
import dreamina_api.browser_worker as worker_module
from dreamina_api.account_login import AccountLoginManager,LoginSession
from dreamina_api.platform_store import PlatformError
from dreamina_api.egress import Egress,parse_proxy_url
from test_worker_reliability import worker,job
from test_managed_workers import Harness,run as run_managed
from test_account_login import make_store
from test_browser_launch import worker_config


async def noop(*_,**__):pass

def test_retained_reference_cards_are_removed_natively_in_same_page():
    async def run():
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Offline Chromium unavailable')
            browser=await pw.chromium.launch(headless=True)
            ctx=await browser.new_context(offline=True);page=await ctx.new_page()
            await page.set_content('''<button aria-label="New session" onclick="document.querySelector('#prompt').innerHTML='';document.querySelector('#heading').hidden=false">New</button>
                <h1 id=heading hidden>What are we creating today?</h1><div role=region aria-label=Generate>
                <div id=cards></div><div contenteditable=true role=textbox aria-label=Prompt id=prompt></div>
                <button aria-label=Generate onclick="window.generated=(window.generated||0)+1">Generate</button></div>
                <button id=outside aria-label="Remove · Reference" onclick="window.outsideRemoved=true">Outside</button>''')
            origin=await page.evaluate('performance.timeOrigin')
            try:
                for count in (1,2,0,3):
                    await page.locator('#prompt').fill('old draft')
                    await page.locator('#cards').evaluate('''(e,count)=>{e.innerHTML='';for(let i=0;i<count;i++){
                        const card=document.createElement('div');card.setAttribute('role','button');card.setAttribute('aria-label','Remove · Reference');
                        card.innerHTML='<img src="data:image/png;base64,AA==" style="width:10px;height:10px"><button aria-label="Remove · Reference">Remove</button>';
                        card.querySelector('button').onclick=event=>{event.stopPropagation();card.remove();window.removed=(window.removed||0)+1};e.append(card)}}''',count)
                    events=[]
                    async def telemetry(v):events.append(v)
                    await NativeUI(page,telemetry,timeout_ms=1000).new_session()
                    assert await page.locator('#cards').locator('button').count()==0
                    assert await page.locator('#prompt').text_content()==''
                    assert await page.evaluate('performance.timeOrigin')==origin
                    assert await page.evaluate('window.generated||0')==0
                    assert await page.evaluate('window.outsideRemoved||false') is False
                    assert events[-1]['detail']['existing_references']==0
                assert await page.evaluate('window.removed')==6
            finally:await browser.close()
    asyncio.run(run())


def test_reference_cleanup_without_progress_is_bounded_and_never_generates():
    async def run():
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Offline Chromium unavailable')
            browser=await pw.chromium.launch(headless=True);page=await browser.new_page()
            await page.set_content('''<h1>What are we creating today?</h1><button aria-label="New session">New</button>
                <div role=region aria-label=Generate><div role=textbox aria-label=Prompt contenteditable=true></div>
                <button aria-label="Remove · Reference" onclick="window.removes=(window.removes||0)+1">Remove</button>
                <button aria-label=Generate onclick="window.generated=1">Generate</button></div>''')
            try:
                with pytest.raises(ReferenceCleanupUnavailable):await asyncio.wait_for(NativeUI(page,noop).new_session(),3)
                assert await page.evaluate('window.removes')==1
                assert await page.evaluate('window.generated||0')==0
            finally:await browser.close()
    asyncio.run(run())


def test_raf_bootstrap_falls_back_when_frames_are_suspended():
    async def run():
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Offline Chromium unavailable')
            browser=await pw.chromium.launch(headless=True);page=await browser.new_page()
            await page.set_content('<p>fixture</p>');await page.evaluate('()=>{window.requestAnimationFrame=()=>0}')
            driver=NativeUI(page,noop,require_bootstrap=True);driver.bootstrap=set(driver.bootstrap_paths)
            try:await asyncio.wait_for(driver.video_bootstrap(),1.5)
            finally:await driver.dispose();await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase',['click_intent','activation_requested','request_observed','reported'])
def test_only_preexisting_v6_journal_with_fresh_context_proof_releases_recovery_window(tmp_path,phase):
    async def run():
        w=worker(tmp_path);j=job();j['claim_protocol_version']=6;reports=[]
        confirmation={'status':'uncertain','error':'Original response unknown'}
        value={'phase':phase,'job':j,'result':confirmation,'confirmation':confirmation}
        if phase=='request_observed':value['confirmation']['submit_id']=str(uuid.uuid4())
        write_json(w.journal,value);w._recovery_window_jobs={j['id']}
        async def api(path,body=None,**_):
            if path.endswith('/status'):return {'status':'uncertain','submit_id':None,'slot_released':False}
            reports.append(body);return {'status':'uncertain','slot_released':True,'submission_window_closed_at':time.time()}
        w.api=api
        try:
            assert await w.recover()
            assert reports[-1]['status']=='uncertain' and reports[-1]['submission_window_closed'] is True
            assert reports[-1].get('generation_not_activated') is not True
            assert not any('/activate' in str(x) for x in reports)
        finally:await w.http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('reason',['no_proof','legacy','active_page'])
def test_recovery_without_old_page_closure_proof_does_not_claim_it(tmp_path,reason):
    async def run():
        w=worker(tmp_path);j=job();j['claim_protocol_version']=5 if reason=='legacy' else 6
        if reason!='no_proof':w._recovery_window_jobs={j['id']}
        if reason=='active_page':w._active_page=object()
        try:assert 'submission_window_closed' not in w._closed_recovery_receipt(j,{'status':'uncertain'})
        finally:await w.http.aclose()
    asyncio.run(run())


def test_browser_crash_invalidates_all_shared_slots_but_normal_close_does_not(tmp_path):
    async def run():
        w=worker(tmp_path);w._entry_readiness={'ready':True,'checked_at':time.time()}
        control={'stop':asyncio.Event(),'readiness':{'ready':True}};w._pool_control=control
        try:
            w._browser_problem('browser_page_crashed')
            assert control['stop'].is_set() and control['readiness']['ready'] is False
            assert w._browser_failed and w._entry_readiness['ready'] is False
            w._closing_context=True;w._browser_failed=False;control['stop'].clear()
            w._browser_problem('normal_close');assert not w._browser_failed and not control['stop'].is_set()
        finally:await w.http.aclose()
    asyncio.run(run())


def test_heartbeat_requires_recent_entry_readiness(tmp_path):
    async def run():
        w=worker(tmp_path);calls=[]
        async def api(path,body=None,**_):calls.append(body);return {}
        w.api=api
        try:
            w._entry_readiness={'ready':True,'checked_at':time.time()};await w.heartbeat(True)
            assert calls[-1]['ready'] is True and calls[-1]['protocol_version']==8 and calls[-1]['entry_ready'] is True
            w._entry_readiness['checked_at']-=121;await w.heartbeat(True)
            assert calls[-1]['ready'] is False and calls[-1]['entry_ready'] is False
        finally:await w.http.aclose()
    asyncio.run(run())


def test_recovery_delay_is_bounded_and_grows(monkeypatch):
    monkeypatch.setattr(worker_module.secrets,'randbelow',lambda _:0)
    assert [Worker.recovery_delay(i) for i in (1,2,3,4,5,6,100)]==[2,4,8,16,32,60,60]


def test_three_unavailable_checks_make_pool_exit_for_supervisor(tmp_path,monkeypatch):
    async def run():
        w=worker(tmp_path);w._entry_checked_at=time.monotonic();calls=[];real_sleep=asyncio.sleep
        class UI:
            def __init__(self,*_):pass
            async def check(self):calls.append(1);return {'ready':False,'reason':'Entry unavailable'}
        async def fast_sleep(_):await real_sleep(0)
        monkeypatch.setattr(worker_module,'NativeUI',UI);monkeypatch.setattr(worker_module.asyncio,'sleep',fast_sleep)
        slot=SimpleNamespace(journal=tmp_path/'none.json',active=None,close=noop)
        w.prepare_slots=lambda:setattr(w,'_slots',[slot]);w._pool_summary=lambda *_:None
        async def run_slot(_,control,__):await control['stop'].wait()
        w._run_slot=run_slot
        try:
            with pytest.raises(RuntimeError,match='restart this worker'):
                await asyncio.wait_for(w.run_pool(readiness={'ready':True}),1)
            assert len(calls)==3 and w._browser_failed and not w._entry_readiness['ready']
        finally:await w.http.aclose()
    asyncio.run(run())


def test_crashed_browser_wait_returns_without_losing_original_journal(tmp_path):
    async def run():
        w=worker(tmp_path);j=job();sid=str(uuid.uuid4());w._browser_failed=True
        write_json(w.journal,{'phase':'reported','job':j,'confirmation':{'status':'submitted','submit_id':sid}})
        before=w.journal.read_bytes()
        async def forbidden(*_,**__):raise AssertionError('No new API mutation during crash handoff')
        w.api=forbidden
        try:
            assert await asyncio.wait_for(w.wait_original(j,{'status':'submitted','submit_id':sid}),.1) is False
            assert w.journal.read_bytes()==before
        finally:await w.http.aclose()
    asyncio.run(run())


def test_successful_fresh_context_under_profile_guard_proves_only_startup_jobs(tmp_path,monkeypatch):
    async def run():
        cfg=worker_module.safe_config(worker_config(tmp_path,[]),'one');w=Worker(cfg)
        old=job();old['claim_protocol_version']=6;write_json(w.journal,{'phase':'click_intent','job':old})
        closed=[]
        class Page:
            def __init__(self):self.closed=False
            async def goto(self,*_,**__):pass
            async def close(self):self.closed=True;closed.append(True)
            def is_closed(self):return self.closed
            def on(self,*_):pass
        entry,restored=Page(),Page();context=SimpleNamespace(pages=[entry,restored],close=noop,on=lambda *_:None)
        async def launch(*_,**__):return context
        try:
            async with profile_lock(cfg['profile_dir']) as guard:
                w.config['_profile_guard']=guard;await w.open(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch)))
                assert closed==[True] and w._recovery_window_jobs=={old['id']}
                new=job();new['claim_protocol_version']=6
                assert 'submission_window_closed' not in w._closed_recovery_receipt(new,{'status':'uncertain'})
                assert w._closed_recovery_receipt(old,{'status':'uncertain'})['submission_window_closed'] is True
        finally:await w.close()
    asyncio.run(run())


def test_worker_browser_uses_split_proxy_credentials_and_pinned_timezone(tmp_path,monkeypatch):
    async def run():
        cfg=worker_module.safe_config(worker_config(tmp_path,[]),'one')
        spec=parse_proxy_url('http://fixture:synthetic-secret@proxy.example:8080');pid='proxy_'+'b'*32
        e=Egress(pid,spec.canonical_url,spec.browser_proxy(),'proxy-'+pid,'Etc/UTC')
        cfg.update(proxy_id=pid,egress_managed=True);monkeypatch.setenv('DREAMINA_EGRESS',e.to_worker_json());calls=[]
        async def launch(*_,**options):calls.append(options);return SimpleNamespace(pages=[SimpleNamespace(goto=noop)],close=noop)
        w=Worker(cfg)
        try:
            await w.open(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch)))
            options=calls[0];assert options['proxy']=={'server':'http://proxy.example:8080','username':'fixture','password':'synthetic-secret'}
            assert options['timezone_id']=='Etc/UTC' and options['locale']=='en-US'
            assert '--force-webrtc-ip-handling-policy=disable_non_proxied_udp' in options['args']
            assert options['extra_http_headers']['Accept-Language'].startswith('en-US')
        finally:await w.close()
    asyncio.run(run())


def test_managed_worker_never_falls_back_direct_without_egress_environment(tmp_path,monkeypatch):
    async def run():
        cfg=worker_module.safe_config(worker_config(tmp_path,[]),'one');cfg['egress_managed']=True
        monkeypatch.delenv('DREAMINA_EGRESS',raising=False);w=Worker(cfg)
        try:
            with pytest.raises(ValueError,match='direct fallback'):await w.open(SimpleNamespace())
        finally:await w.close()
    asyncio.run(run())


def test_corrupt_other_registry_occupies_only_one_capacity(tmp_path):
    h=Harness(tmp_path,{'MANAGED_MAX_WORKERS':2});p=h.worker.paths('first').registry;p.parent.mkdir(parents=True);p.write_text('{broken')
    result=run_managed(h.worker.start('second'))
    assert result['state']=='starting' and len(h.calls)==1
    assert h.worker._active_process_count()==2


def test_managed_proxy_secrets_only_go_to_child_environment(tmp_path):
    h=Harness(tmp_path);spec=parse_proxy_url('http://fixture-user:fixture-password@proxy.example:8080')
    e=Egress('proxy_'+'a'*32,spec.canonical_url,spec.browser_proxy(),'proxy-proxy_'+'a'*32,'Etc/UTC')
    h.store.resolve_account_egress=lambda _:e
    result=run_managed(h.worker.start('first'));assert result['state']=='starting'
    raw=h.worker.paths('first').config.read_text();assert 'fixture-password' not in raw and 'fixture-user' not in raw and 'http://' not in json.dumps(json.loads(raw)['accounts'])
    env=h.calls[0][1]['env'];assert Egress.from_worker_json(env['DREAMINA_EGRESS']).playwright_proxy==spec.browser_proxy()
    assert json.loads(raw)['accounts'][0]['proxy_id']==e.proxy_id


def test_login_close_timeout_retains_guard_without_owned_process_proof(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path);m=AccountLoginManager(store,bridge,None,root=tmp_path/'managed');m.close_timeout=.02
        class Guard:
            released=False
            async def __aexit__(self,*_):self.released=True
        async def hangs():await asyncio.sleep(30)
        guard=Guard();s=LoginSession('fixture','one','owner',time.time(),time.time()+60,context=SimpleNamespace(close=hangs),guard=guard)
        try:
            with pytest.raises(PlatformError) as err:await asyncio.wait_for(m._close_context(s),.5)
            assert err.value.code=='login_cleanup_pending' and s.cleanup_blocked
            assert not guard.released and s.context is not None
            # Playwright may return immediately on a second close after its
            # first call timed out. That is not a close acknowledgement.
            s.context.close=noop
            with pytest.raises(PlatformError):await m._close_context(s)
            assert not guard.released and s.context is not None
            s.context_closed=True;await m._close_context(s)
            assert guard.released and s.context is None
            assert not m._terminate_owned_browser({'pid':1,'identity':'fake','profile':'/fake'})
        finally:store.engine.dispose()
    asyncio.run(run())


def test_login_verified_owned_termination_can_finish_timeout_cleanup(tmp_path,monkeypatch):
    async def run():
        store,bridge=make_store(tmp_path);m=AccountLoginManager(store,bridge,None,root=tmp_path/'managed');m.close_timeout=.02;released=[];terminations=[]
        async def hangs():await asyncio.sleep(30)
        async def release(*_):released.append(True)
        proof={'pid':123,'identity':'synthetic-proof','profile':'/fixture'}
        monkeypatch.setattr(m,'_terminate_owned_browser',lambda record:terminations.append(record) or record is proof)
        s=LoginSession('fixture','one','owner',time.time(),time.time()+60,context=SimpleNamespace(close=hangs),guard=SimpleNamespace(__aexit__=release),browser_process=proof)
        try:
            await asyncio.wait_for(m._close_context(s),.5)
            assert terminations==[proof] and released==[True] and s.context is None and s.guard is None and not s.cleanup_blocked
        finally:store.engine.dispose()
    asyncio.run(run())


def test_login_shutdown_continues_other_sessions_after_blocked_close(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path);m=AccountLoginManager(store,bridge,None,root=tmp_path/'managed');m.close_timeout=.01;closed=[]
        async def hangs():await asyncio.sleep(30)
        async def close_other():closed.append(True)
        first=LoginSession('a','one','owner',time.time(),time.time()+60,context=SimpleNamespace(close=hangs))
        second=LoginSession('b','two','owner',time.time(),time.time()+60,context=SimpleNamespace(close=close_other))
        m.sessions={'a':first,'b':second}
        try:
            with pytest.raises(PlatformError,match='remain protected'):await asyncio.wait_for(m.shutdown(),1)
            assert first.cleanup_blocked and first.context is not None and second.context is None and closed==[True]
        finally:store.engine.dispose()
    asyncio.run(run())


def test_login_database_failure_does_not_skip_browser_close(tmp_path,monkeypatch):
    async def run():
        store,bridge=make_store(tmp_path);m=AccountLoginManager(store,bridge,None,root=tmp_path/'managed');closed=[]
        async def close_context():closed.append(True)
        async def unavailable(_):raise RuntimeError('fixture unavailable')
        def abort_fails(_):raise RuntimeError('fixture database busy')
        m._open=unavailable;monkeypatch.setattr(store,'abort_browser_login',abort_fails)
        s=LoginSession('a','one','owner',time.time(),time.time()+60,context=SimpleNamespace(close=close_context))
        try:
            with pytest.raises(RuntimeError,match='database busy'):await m._run(s)
            assert closed==[True] and s.context is None
        finally:store.engine.dispose()
    asyncio.run(run())
