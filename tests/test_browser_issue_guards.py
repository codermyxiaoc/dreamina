"""No live website requests: late material races, proxy faults and owned cleanup."""
import asyncio,json,os,time,uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from playwright.async_api import Error as PlaywrightError,async_playwright

from dreamina_api.browser_driver import NativeUI,ReferenceCountMismatch,BrowserProxyUnavailable
from dreamina_api.browser_worker import Worker,write_json
from dreamina_api.browser_launch import browser_environment,IGNORED_DEFAULT_ARGS
import dreamina_api.browser_worker as worker_module
from dreamina_api.egress import direct_egress
from test_worker_reliability import worker,job
from test_native_promotions import fixture,SPEC
from test_credit_quote import quoted_document
from test_browser_launch import worker_config


async def noop(*_,**__):pass


CARD="<div role='button' aria-label='Remove · Reference'><img style='width:12px;height:12px' src='data:image/png;base64,AA=='><button aria-label='Remove · Reference'>Remove</button></div>"


@pytest.mark.parametrize('when',['preflight','turn','activation'])
def test_material_arriving_after_new_session_never_reaches_generate(when):
    async def check():
        async with fixture(quoted_document()) as (page,requests):
            driver=NativeUI(page,noop,timeout_ms=1500,submission_timeout_ms=300);intents=[]
            async def late():
                await page.evaluate("html=>document.querySelector('[role=region]').insertAdjacentHTML('beforeend',html)",CARD)
            async def preflight(_):
                if when=='preflight':await late()
            async def turn():
                if when=='turn':await late()
            async def activate():
                intents.append(True)
                if when=='activation':await late()
            driver.before_activation_turn=turn
            with pytest.raises(ReferenceCountMismatch):await driver.submit(SPEC,[],activate,on_preflight=preflight)
            assert not requests and not driver.clicked and await page.evaluate('window.generations||0')==0
            assert len(intents)==int(when=='activation')
    asyncio.run(check())


@pytest.mark.parametrize('markup',["<button aria-label='Remove · Reference'>Remove</button>","<video style='width:10px;height:10px'></video>"])
def test_reference_card_and_media_counts_must_both_match(markup):
    async def check():
        async with fixture(quoted_document()) as (page,_):
            await page.evaluate("html=>document.querySelector('[role=region]').insertAdjacentHTML('beforeend',html)",markup)
            with pytest.raises(ReferenceCountMismatch):await NativeUI(page,noop).verify_reference_count(SPEC|{'references':[{'kind':'image'}]})
    asyncio.run(check())


def test_reference_count_ignores_card_wrapper_and_prompt_mention_thumbnail():
    async def check():
        async with fixture(quoted_document()) as (page,_):
            await page.evaluate("html=>document.querySelector('[role=region]').insertAdjacentHTML('beforeend',html)",CARD)
            await page.evaluate("document.querySelector('#prompt').innerHTML='<span class=node-reference-mention-tag><img style=width:10px;height:10px src=data:image/png;base64,AA==>image1</span>'")
            assert await NativeUI(page,noop).verify_reference_count(SPEC|{'references':[{'kind':'image'}]})=={'buttons':1,'media':1}
    asyncio.run(check())


@pytest.mark.parametrize('code',['ERR_PROXY_CONNECTION_FAILED','ERR_TUNNEL_CONNECTION_FAILED','ERR_SOCKS_CONNECTION_FAILED'])
def test_driver_maps_explicit_browser_proxy_errors_without_raw_url(code):
    async def check():
        async with fixture(quoted_document()) as (page,_):
            driver=NativeUI(page,noop)
            async def unavailable():raise PlaywrightError('net::'+code+' at https://private.fixture/?token=private')
            driver.new_session=unavailable
            with pytest.raises(BrowserProxyUnavailable) as error:await driver.submit(SPEC,[],noop)
            assert error.value.error_code=='proxy_error' and 'private' not in str(error.value) and not driver.clicked
            assert not Worker.preparation_retryable(error.value,driver,False)
    asyncio.run(check())


def test_unknown_page_close_failure_preserves_receipt_and_stops_owned_pool(tmp_path):
    async def check():
        owner=worker(tmp_path);slot=worker(tmp_path/'slot');slot._pool_owner=owner
        control={'stop':asyncio.Event(),'readiness':{'ready':True}};owner._pool_control=control
        j=job();j['claim_protocol_version']=6;write_json(slot.journal,{'phase':'click_intent','job':j});before=slot.journal.read_bytes()
        async def failed_close():raise RuntimeError('Synthetic close failure')
        page=SimpleNamespace(close=failed_close,is_closed=lambda:False);slot._active_page=page
        try:
            value=await slot._quiesce_unknown_page(j,{'status':'uncertain'},page)
            assert value=={'status':'uncertain'} and slot.journal.read_bytes()==before and slot._active_page is page
            assert owner._browser_failed and control['stop'].is_set() and control['browser_failure']=='unknown_submission_page_close_failed'
        finally:await slot.http.aclose();await owner.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('close_confirmed',[True,False])
def test_post_permit_reference_guard_reports_safe_failure_only_after_close_proof(tmp_path,monkeypatch,close_confirmed):
    from test_worker_pipeline_v6 import install
    async def check():
        w,j,calls,pages,reports=install(tmp_path,monkeypatch);base=worker_module.NativeUI
        class Driver(base):
            async def submit(self,spec,files,before_click,**kwargs):
                await before_click()
                if not close_confirmed:
                    async def cannot_close():raise RuntimeError('Synthetic closure failure')
                    self.page.close=cannot_close
                error=ReferenceCountMismatch('No Generate action was sent.');error.native_activation_not_attempted=True
                raise error
        monkeypatch.setattr(worker_module,'NativeUI',Driver)
        try:
            assert await w.handle(j) is close_confirmed
            assert len(reports)==1 and sum(str(p).endswith('/activate') for p in calls)==1
            assert reports[0]['generation_not_activated'] is close_confirmed
            assert reports[0]['status']==('failed' if close_confirmed else 'uncertain')
            assert 'submission_window_closed' not in reports[0] and not reports[0].get('submit_id')
            if not close_confirmed:assert w._browser_failed
        finally:await w.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('finish',['terminal','detach'])
def test_owned_assets_removed_only_after_durable_finish(tmp_path,finish):
    async def check():
        w=worker(tmp_path);j=job();directory=tmp_path/'assets'/j['id'];directory.mkdir(parents=True)
        owned=directory/('asset_0_'+'a'*24+'.png');owned.write_bytes(b'private local copy')
        unknown=directory/'operator-note.txt';unknown.write_text('keep')
        other=tmp_path/'assets'/str(uuid.uuid4());other.mkdir();otherfile=other/owned.name;otherfile.write_bytes(b'keep')
        try:
            write_json(w.journal,{'phase':'click_intent','job':j});w._cleanup_job_assets(j['id']);assert owned.exists()
            if finish=='detach':w._archive_detached(j,{'status':'submitted','submit_id':str(uuid.uuid4())})
            else:w._mark_finished(j['id'])
            assert not owned.exists() and unknown.exists() and otherfile.exists()
            assert json.loads(w.journal.read_text())['phase']=='finished'
            if finish=='detach':assert (tmp_path/'history'/(j['id']+'.json')).exists()
        finally:await w.http.aclose()
    asyncio.run(check())


def test_asset_cleanup_cannot_escape_own_job_directory(tmp_path):
    async def check():
        w=worker(tmp_path/'slot');outside=tmp_path/'outside';outside.mkdir();sentinel=outside/('asset_0_'+'a'*24+'.png');sentinel.write_bytes(b'keep')
        try:
            write_json(w.journal,{'phase':'finished','job_id':'../../outside'});w._cleanup_job_assets('../../outside')
            assert sentinel.exists()
        finally:await w.http.aclose()
    asyncio.run(check())


def test_worker_pops_secrets_before_driver_start_and_browser_inheritance(tmp_path,monkeypatch):
    async def check():
        key=Fernet.generate_key().decode();monkeypatch.setenv('DREAMINA_PROFILE_KEY',key);monkeypatch.setenv('DREAMINA_EGRESS',direct_egress().to_worker_json())
        monkeypatch.setenv('DREAMINA_WORKER_KEY','private-bridge-key');cfg=worker_module.safe_config(worker_config(tmp_path,[]),'one');calls=[]
        w=Worker(cfg)
        async def launch(*_,**options):
            calls.append(options);return SimpleNamespace(pages=[SimpleNamespace(goto=noop)],close=noop)
        try:
            assert not any(k in os.environ for k in ('DREAMINA_PROFILE_KEY','DREAMINA_EGRESS','DREAMINA_WORKER_KEY'))
            assert w._profile_key==key
            await w.open(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch)))
            assert calls[0]['ignore_default_args']==['--disable-background-timer-throttling']
            assert not any(k in calls[0]['env'] for k in ('DREAMINA_PROFILE_KEY','DREAMINA_EGRESS','DREAMINA_WORKER_KEY'))
        finally:await w.close()
    asyncio.run(check())


def test_login_browser_environment_is_allowlisted_without_mutating_server(tmp_path,monkeypatch):
    from dreamina_api.account_login import AccountLoginManager,LoginSession
    from test_browser_launch import FakeStore
    async def check():
        secret_fields=('ADMIN_PASSWORD','PLATFORM_ENCRYPTION_KEY','CHROME_BRIDGE_KEY','DREAMINA_PROFILE_KEY','DREAMINA_EGRESS')
        for key in secret_fields:monkeypatch.setenv(key,'synthetic-server-secret')
        monkeypatch.setenv('LOCALAPPDATA','synthetic-system-path');monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH','synthetic-playwright-path')
        captured=[]
        class Captured(Exception):pass
        async def capture(*args):captured.append(args[2]);raise Captured()
        manager=AccountLoginManager(FakeStore(),None,None,root=tmp_path,context_factory=capture)
        session=LoginSession('fixture','one','owner',time.time(),time.time()+60)
        try:
            with pytest.raises(Captured):await manager._open(session)
            assert not any(key in captured[0]['env'] for key in secret_fields)
            assert captured[0]['env']['LOCALAPPDATA']=='synthetic-system-path'
            assert captured[0]['env']['PLAYWRIGHT_BROWSERS_PATH']=='synthetic-playwright-path'
            assert all(os.environ[key]=='synthetic-server-secret' for key in secret_fields)
        finally:await manager._close_context(session)
    asyncio.run(check())


def test_timer_throttling_override_absent_from_real_offline_chromium_command():
    async def check():
        async with async_playwright() as pw:
            if not __import__('pathlib').Path(pw.chromium.executable_path).exists():pytest.skip('Offline Chromium unavailable')
            browser=await pw.chromium.launch(headless=True,args=['--enable-automation'],ignore_default_args=list(IGNORED_DEFAULT_ARGS),env=browser_environment())
            try:
                context=await browser.new_context(offline=True);page=await context.new_page();await page.set_content('<p>offline</p>')
                session=await browser.new_browser_cdp_session();result=await session.send('Browser.getBrowserCommandLine')
                assert '--disable-background-timer-throttling' not in result['arguments']
                assert await page.locator('p').inner_text()=='offline'
            finally:await browser.close()
    asyncio.run(check())


@pytest.mark.parametrize('value',[None,1,'true',[],{}])
def test_startup_cache_cleanup_flag_must_be_boolean(tmp_path,value):
    path=worker_config(tmp_path,[]);data=json.loads(path.read_text());data['accounts'][0]['clean_cache_on_start']=value;path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='clean_cache_on_start'):worker_module.safe_config(path,'one')


@pytest.mark.parametrize('enabled',[False,True])
def test_startup_cache_cleanup_is_inside_profile_guard_before_browser(tmp_path,monkeypatch,enabled):
    import dreamina_api.maintenance as maintenance
    async def check():
        path=worker_config(tmp_path,[]);data=json.loads(path.read_text());data['accounts'][0]['clean_cache_on_start']=enabled;path.write_text(json.dumps(data));events=[]
        profile=worker_module.safe_config(path,'one')['profile_dir']
        def clean(directory,**options):
            assert directory==profile and options=={'root':profile,'owned_lock':True}
            assert (profile/'.dreamina-worker.lock').is_file();events.append('cache')
        class Playwright:
            async def __aenter__(self):events.append('browser');return object()
            async def __aexit__(self,*_):pass
        async def execute(self,*_):events.append('run')
        monkeypatch.setattr(maintenance,'cleanup_profile_cache',clean,raising=False)
        monkeypatch.setattr(worker_module,'async_playwright',Playwright);monkeypatch.setattr(Worker,'run',execute);monkeypatch.setattr(Worker,'heartbeat',noop)
        await worker_module.main_async(SimpleNamespace(config=path,account='one',command='run',import_local_session=False,max_jobs=0))
        assert events==(['cache','browser','run'] if enabled else ['browser','run'])
        assert not (profile/'.dreamina-worker.lock').exists()
    asyncio.run(check())


@pytest.mark.parametrize('failure',['classified','chromium_proxy','unrelated'])
def test_main_persists_only_instance_bound_proxy_failure_before_cleanup(tmp_path,monkeypatch,failure):
    async def check():
        path=worker_config(tmp_path,[]);data=json.loads(path.read_text());data['accounts'][0]['instance_id']='fixture-instance'
        path.write_text(json.dumps(data));state=worker_module.safe_config(path,'one')['state_dir'];closed=[]
        class Playwright:
            async def __aenter__(self):return object()
            async def __aexit__(self,*_):pass
        error=(BrowserProxyUnavailable('synthetic private proxy credential') if failure=='classified' else
               PlaywrightError('net::ERR_TUNNEL_CONNECTION_FAILED at https://private.fixture/?secret=value') if failure=='chromium_proxy' else RuntimeError('unrelated'))
        async def execute(self,*_):raise error
        async def close(self):
            closed.append(True)
            write_json(self.status_file,{'state':'stopped'})
            await self.http.aclose()
        monkeypatch.setattr(worker_module,'async_playwright',Playwright);monkeypatch.setattr(Worker,'run',execute);monkeypatch.setattr(Worker,'close',close)
        with pytest.raises(type(error)):await worker_module.main_async(SimpleNamespace(config=path,account='one',command='run',import_local_session=False,max_jobs=0))
        assert closed==[True]
        receipt=state/'proxy-failure.json'
        if failure=='unrelated':assert not receipt.exists();return
        raw=receipt.read_text();value=json.loads(raw)
        assert set(value)=={'account_id','pid','instance_id','error_code','observed_at'}
        assert value['account_id']=='one' and value['pid']==os.getpid() and value['instance_id']=='fixture-instance'
        assert value['error_code']=='proxy_error' and value['observed_at']>0
        assert 'secret' not in raw and 'private' not in raw
    asyncio.run(check())
