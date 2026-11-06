import asyncio
import hashlib
import json
from pathlib import Path
import time

from cryptography.fernet import Fernet
import pytest
from playwright.async_api import async_playwright
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dreamina_api.account_login import (AccountLoginManager,LoginSession,create_account_login_router,
                                       identity_response,credentials_from_cookies)
from dreamina_api.admin_api import create_admin_router
from dreamina_api.platform_store import PlatformStore,PlatformError
from dreamina_api.chrome_bridge import BridgeStore


def make_store(tmp_path):
    store=PlatformStore('sqlite:///'+str(tmp_path/'platform.db'),encryption_key=Fernet.generate_key())
    store.create_account({'id':'one','name':'Fixture one','capabilities':['seedance-2.0-mini']})
    store.create_account({'id':'two','name':'Fixture two','capabilities':['seedance-2.0-mini']})
    return store,BridgeStore(tmp_path/'bridge')


def test_identity_only_from_known_first_party_account_response():
    body={'data':{'user_id_str':'123456789'}}
    assert identity_response('https://dreamina.capcut.com/passport/web/account/info/',body)=='123456789'
    for url in ['https://attacker.example/passport/web/account/info/','https://capcut.com.attacker.test/passport/web/account/info/','http://dreamina.capcut.com/passport/web/account/info/','https://dreamina.capcut.com/mweb/other']:
        assert identity_response(url,body) is None
    assert identity_response('https://dreamina.capcut.com/passport/web/account/info/',{'ret':'-1',**body}) is None


def test_only_first_party_credentials_are_synced():
    cookies=[{'domain':'.capcut.com','name':'sessionid','value':'synthetic','path':'/'},
             {'domain':'.google.com','name':'google-secret','value':'never-copy','path':'/'},
             {'domain':'.capcut.com','name':'bad','value':'line\nbreak','path':'/'}]
    result=credentials_from_cookies(cookies,'https://dreamina-api.us.capcut.com')
    assert result['session_id']=='synthetic'
    assert result['cookie']=='sessionid=synthetic'
    assert 'google' not in json.dumps(result)
    assert result['workspace_id']==''


def test_session_owner_and_capacity_are_isolated(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path)
        manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed',config={'LOGIN_MAX_SESSIONS':'1'})
        blocker=asyncio.Event()
        async def hold(session):await blocker.wait()
        manager._run=hold
        a=await manager.create('one','admin-a')
        with pytest.raises(PlatformError) as error:manager.get(a['id'],'admin-b')
        assert error.value.status==404
        with pytest.raises(PlatformError) as error:await manager.create('two','admin-a')
        assert error.value.code=='login_capacity'
        with pytest.raises(PlatformError) as error:await manager.create('one','admin-b')
        assert error.value.code=='login_busy'
        await manager.shutdown()
    asyncio.run(run())


def test_cleanup_must_finish_before_reopen(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path);manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
        pending=asyncio.create_task(asyncio.sleep(30))
        s=LoginSession('s','one','owner',time.time(),time.time()+900,state='succeeded',task=pending)
        manager.sessions['s']=s;manager.by_account['one']='s'
        with pytest.raises(PlatformError) as error:await manager.create('one','owner')
        assert error.value.code=='login_busy'
        pending.cancel();await asyncio.gather(pending,return_exceptions=True)
    asyncio.run(run())


class FakeLoginContext:
    def __init__(self,session_value='synthetic-session'):
        self.cookie_reads=[]
        self.closed=False
        self.session_value=session_value

    async def cookies(self,urls=None):
        self.cookie_reads.append(urls)
        return [{'domain':'.capcut.com','name':'sessionid','value':self.session_value,'path':'/','secure':True}]

    async def close(self):
        self.closed=True


class BlockingSupervisor:
    def __init__(self):
        self.started=asyncio.Event()
        self.release=asyncio.Event()

    async def start(self,account_id):
        assert account_id=='one'
        self.started.set()
        await self.release.wait()
        return {'state':'running'}


def fake_login_session(store,manager):
    store.begin_browser_login('one')
    session=LoginSession('synthetic-login','one','owner',time.time(),time.time()+900,
                         state='awaiting_login',context=FakeLoginContext(),identity='123456789',
                         identity_session_digest=hashlib.sha256(b'synthetic-session').hexdigest())
    manager.sessions[session.id]=session
    manager.by_account['one']=session.id
    return session


def test_login_persistence_waits_for_inflight_human_input(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path)
        manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
        session=fake_login_session(store,manager)
        context=session.context
        input_started=asyncio.Event()
        input_release=asyncio.Event()

        class InputPage:
            def __init__(self):self.keyboard=self
            def is_closed(self):return context.closed
            async def insert_text(self,text):
                assert text=='synthetic-final-input'
                input_started.set()
                await input_release.wait()

        session.pages['page']=InputPage();session.active_page='page'
        typing=asyncio.create_task(manager.input(session.id,'owner',{'action':'text','text':'synthetic-final-input'}))
        finish=None
        try:
            await asyncio.wait_for(input_started.wait(),1)
            assert session.lock.locked()
            finish=asyncio.create_task(manager._finish(session))
            await asyncio.sleep(0)
            assert not finish.done()
            assert context.cookie_reads==[]
            assert not session.profile_saved and not session.credentials_synced
            assert store.get_account('one')['status']=='login_pending'
            assert not (tmp_path/'managed'/'auth'/'one.enc').exists()
            input_release.set()
            await asyncio.wait_for(asyncio.gather(typing,finish),2)
            assert session.state=='succeeded' and context.closed
            assert store.settings_for_account('one').session_id=='synthetic-session'
        finally:
            input_release.set()
            for task in (typing,finish):
                if task and not task.done():task.cancel()
            await asyncio.gather(*(task for task in (typing,finish) if task),return_exceptions=True)
            store.engine.dispose()
    asyncio.run(run())


def test_login_remains_verifying_until_worker_start_result_is_visible(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path);supervisor=BlockingSupervisor()
        manager=AccountLoginManager(store,bridge,supervisor,root=tmp_path/'managed')
        session=fake_login_session(store,manager)
        context=session.context
        session.task=asyncio.create_task(manager._finish(session))
        try:
            await asyncio.wait_for(supervisor.started.wait(),2)
            before=manager.public(session)
            assert before['state']=='verifying'
            assert before['profile_saved'] and before['credentials_synced']
            assert before['worker_state'] is None and context.closed
            assert not session.task.done()
            supervisor.release.set()
            await asyncio.wait_for(session.task,2)
            after=manager.public(session)
            assert after['state']=='succeeded' and after['worker_state']=='running'
        finally:
            supervisor.release.set()
            await manager.shutdown()
            store.engine.dispose()
    asyncio.run(run())


def test_cancel_after_persistence_preserves_success_and_saved_credentials(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path);supervisor=BlockingSupervisor()
        manager=AccountLoginManager(store,bridge,supervisor,root=tmp_path/'managed')
        session=fake_login_session(store,manager)
        context=session.context

        async def already_open(current):
            assert current is session
        manager._open=already_open
        session.task=asyncio.create_task(manager._run(session))
        try:
            await asyncio.wait_for(supervisor.started.wait(),2)
            assert session.state=='verifying' and session.profile_saved
            auth=tmp_path/'managed'/'auth'/'one.enc'
            before=auth.read_bytes()
            result=await asyncio.wait_for(manager.cancel(session.id,'owner'),2)
            assert result['state']=='succeeded'
            assert result['credentials_synced'] and result['profile_saved']
            assert store.get_account('one')['status']=='ready'
            assert store.settings_for_account('one').session_id=='synthetic-session'
            assert auth.read_bytes()==before
            assert context.closed and session.context is None
            assert session.task.done()
        finally:
            supervisor.release.set()
            await manager.shutdown()
            store.engine.dispose()
    asyncio.run(run())


def test_identity_cookie_mismatch_rejects_relogin_and_preserves_saved_credentials(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path)
        manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
        previous=fake_login_session(store,manager)
        await manager._finish(previous)
        auth=tmp_path/'managed'/'auth'/'one.enc'
        snapshot_before=auth.read_bytes()
        account_before=store.get_account('one')
        credentials_before=store.settings_for_account('one')
        session=fake_login_session(store,manager)
        session.context.session_value='synthetic-replacement-session'
        context=session.context

        async def already_open(current):
            assert current is session
        manager._open=already_open
        session.task=asyncio.create_task(manager._run(session))
        try:
            await asyncio.wait_for(session.task,2)
            assert session.state=='failed' and session.error_code=='identity_session_changed'
            assert not session.credentials_synced and not session.profile_saved
            after=store.get_account('one')
            assert after['status']=='login_required'
            for field in ('identity_hint','browser_profile_saved','last_login_at'):
                assert after[field]==account_before[field]
            saved=store.settings_for_account('one')
            assert saved.session_id==credentials_before.session_id
            assert saved.cookie==credentials_before.cookie
            assert auth.read_bytes()==snapshot_before
            assert context.closed and session.context is None
        finally:
            await manager.shutdown()
            store.engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('has_previous_login',[False,True])
def test_database_failure_after_snapshot_replace_restores_previous_file(tmp_path,monkeypatch,has_previous_login):
    async def run():
        store,bridge=make_store(tmp_path)
        manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
        auth=tmp_path/'managed'/'auth'/'one.enc'
        if has_previous_login:
            await manager._finish(fake_login_session(store,manager))
        previous_bytes=auth.read_bytes() if auth.exists() else None
        previous_credentials=store.settings_for_account('one')
        session=fake_login_session(store,manager)
        session.context.session_value='synthetic-replacement-session'
        session.identity_session_digest=hashlib.sha256(b'synthetic-replacement-session').hexdigest()
        persisted=[]

        def fail_after_file_replacement(account_id,**kwargs):
            assert account_id=='one'
            kwargs['persist_state']()
            replaced=auth.read_bytes()
            assert replaced!=previous_bytes
            contents=json.loads(Fernet(store.profile_cipher_key('one')).decrypt(replaced))
            assert contents['cookies'][0]['value']=='synthetic-replacement-session'
            persisted.append(True)
            raise RuntimeError('Synthetic database commit failure')

        async def already_open(current):
            assert current is session
        monkeypatch.setattr(store,'complete_browser_login',fail_after_file_replacement)
        manager._open=already_open
        session.task=asyncio.create_task(manager._run(session))
        try:
            await asyncio.wait_for(session.task,2)
            assert persisted==[True] and session.state=='failed'
            assert not session.profile_saved and not session.credentials_synced
            assert store.get_account('one')['status']=='login_required'
            saved=store.settings_for_account('one')
            assert saved.session_id==previous_credentials.session_id
            assert saved.cookie==previous_credentials.cookie
            if has_previous_login:assert auth.read_bytes()==previous_bytes
            else:assert not auth.exists()
            assert not auth.with_suffix('.rollback').exists()
            assert not auth.with_suffix('.tmp').exists()
        finally:
            await manager.shutdown()
            store.engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('status,has_previous_snapshot',[('login_pending',True),('login_pending',False),('ready',True)])
def test_startup_recovers_snapshot_consistently_with_database_state(tmp_path,status,has_previous_snapshot):
    async def run():
        store,bridge=make_store(tmp_path)
        manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
        if status=='login_pending':store.begin_browser_login('one')
        else:store.record_account_check('one',status='ready')
        auth=tmp_path/'managed'/'auth'/'one.enc'
        auth.parent.mkdir(parents=True)
        cipher=Fernet(store.profile_cipher_key('one'))
        previous=cipher.encrypt(b'{"cookies":[],"fixture":"previous"}')
        replacement=cipher.encrypt(b'{"cookies":[],"fixture":"replacement"}')
        auth.write_bytes(replacement)
        backup=auth.with_suffix('.rollback')
        backup.write_bytes(previous if has_previous_snapshot else b'')
        try:
            await manager.startup()
            assert not backup.exists()
            if status=='ready':
                assert auth.read_bytes()==replacement
                assert store.get_account('one')['status']=='ready'
            else:
                assert store.get_account('one')['status']=='login_required'
                if has_previous_snapshot:assert auth.read_bytes()==previous
                else:assert not auth.exists()
            expected=auth.read_bytes() if auth.exists() else None
            await manager.startup()
            assert (auth.read_bytes() if auth.exists() else None)==expected
        finally:
            await manager.shutdown()
            store.engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('url,allowed',[
    ('https://accounts.google.com/o/oauth2/v2/auth?state=synthetic',True),
    ('https://appleid.apple.com/auth/authorize',True),
    ('https://dreamina.capcut.com/passport/web/login/',True),
    ('https://accounts.google.com.attacker.test/oauth',False),
    ('http://accounts.google.com/oauth',False),
    ('https://synthetic:password@accounts.google.com/oauth',False),
    ('file:///synthetic.html',False),
])
def test_oauth_navigation_without_frame_still_checks_allowed_origin(tmp_path,url,allowed):
    async def run():
        manager=AccountLoginManager(None,None,None,root=tmp_path/'managed')
        session=LoginSession('navigation','one','owner',time.time(),time.time()+900)

        class PopupRequest:
            def __init__(self):self.url=url
            def is_navigation_request(self):return True
            @property
            def frame(self):raise RuntimeError('Popup navigation has no frame yet')

        class Route:
            def __init__(self):self.request=PopupRequest();self.actions=[]
            async def abort(self):self.actions.append('abort')
            async def fallback(self):self.actions.append('fallback')

        route=Route()
        await manager._navigation_guard(session,route)
        assert route.actions==(['fallback'] if allowed else ['abort'])
    asyncio.run(run())


@pytest.mark.parametrize('action',['cancel','shutdown'])
def test_login_cancelled_before_task_starts_releases_account_ownership(tmp_path,action):
    async def run():
        store,bridge=make_store(tmp_path)
        manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
        entered=[]

        async def must_not_start(session):
            entered.append(session.id)
            raise AssertionError('The scheduled login task must be cancelled before its first instruction')
        manager._run=must_not_start
        try:
            created=await manager.create('one','owner')
            session=manager.get(created['id'],'owner')
            assert not entered and not session.task.done()
            assert store.get_account('one')['status']=='login_pending'
            # Do not wrap this call in wait_for: scheduling an extra task would allow _run to start first.
            if action=='cancel':
                result=await manager.cancel(session.id,'owner')
                assert result['state']=='cancelled'
            else:
                await manager.shutdown()
            assert not entered
            assert session.task.done() and session.task.cancelled()
            assert session.state=='cancelled' and session.context is None
            assert store.get_account('one')['status']=='login_required'
            assert not list((tmp_path/'managed').rglob('*.enc'))
        finally:
            await manager.shutdown()
            store.engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('unfinished_status',['queued','submitted','uncertain'])
def test_old_unfinished_bridge_job_blocks_login_beyond_history_page(tmp_path,unfinished_status):
    async def run():
        store,bridge=make_store(tmp_path)
        manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
        oldest=bridge.enqueue({'model':'seedance-2.0-mini','prompt':'Synthetic scene'},account_id='one')
        with bridge._connection() as db:
            db.execute('UPDATE jobs SET status=? WHERE id=?',(unfinished_status,oldest['id']))
            db.executemany('INSERT INTO jobs(id,account_id,request_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                           [(f'00000000-0000-4000-8000-{index:012d}','one',json.dumps(oldest['request']),
                             'completed',time.time(),time.time()) for index in range(101)])
        recent=bridge.list_jobs(account_id='one',limit=100)
        assert len(recent)==100 and all(job['status']=='completed' for job in recent)
        assert oldest['id'] not in {job['id'] for job in recent}
        try:
            with pytest.raises(PlatformError) as error:
                await manager.create('one','owner')
            assert error.value.code=='account_busy' and error.value.status==409
            assert store.get_account('one')['status']=='unverified'
            assert not manager.sessions
        finally:
            await manager.shutdown()
            store.engine.dispose()
    asyncio.run(run())


def test_login_routes_require_admin_owner_and_csrf(tmp_path):
    store,bridge=make_store(tmp_path);store.bootstrap_admin('admin','synthetic-password-long')
    manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed')
    admin=create_admin_router(store,bridge);app=FastAPI();app.include_router(admin);app.include_router(create_account_login_router(manager,admin.admin_dependency))
    with TestClient(app) as c:
        assert c.get('/api/admin/login-sessions/unknown').status_code==401
        login=c.post('/api/admin/login',json={'username':'admin','password':'synthetic-password-long'});csrf=login.json()['csrf_token']
        assert c.post('/api/admin/accounts/one/login-sessions').status_code==403
        owner=hashlib.sha256(csrf.encode()).hexdigest()
        manager.sessions['owned']=LoginSession('owned','one','different-owner',time.time(),time.time()+900)
        assert c.get('/api/admin/login-sessions/owned').status_code==404
        assert c.get('/api/admin/login-sessions/owned/frame').status_code==404
        assert c.post('/api/admin/login-sessions/owned/input',headers={'X-CSRF-Token':csrf},json={'action':'text','text':'not-forwarded'}).status_code==404


def test_offline_remote_login_panel_saves_state_and_credentials_without_copying_cookie(tmp_path):
    async def run():
        store,bridge=make_store(tmp_path)
        checked=[]
        async def check(account,settings):
            checked.append(account)
            assert settings.session_id=='synthetic-session'
            return {'status':'ready','balance':42}
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Chromium fixture runtime unavailable')
            browser=await pw.chromium.launch(headless=True)
            async def factory(account,profile,options):
                context=await browser.new_context(offline=True,viewport={'width':1100,'height':760})
                html='''<!doctype html><body>
                <button id="signin" style="position:absolute;left:20px;top:20px" onclick="document.querySelector('#form').hidden=false">Sign in</button>
                <div id="form" hidden><input id="secret" type="password" style="position:absolute;left:20px;top:90px;width:300px;height:30px">
                <button id="finish" style="position:absolute;left:20px;top:150px;width:300px;height:40px" onclick="document.cookie='sessionid=synthetic-session; Domain=.capcut.com; Path=/; Secure';fetch('/passport/web/account/info/').then(()=>document.querySelector('#signin').remove())">Finish synthetic login</button></div></body>'''
                async def route(route):
                    if '/passport/web/account/info/' in route.request.url:await route.fulfill(status=200,content_type='application/json',body=json.dumps({'data':{'user_id_str':'123456789'}}))
                    elif route.request.resource_type=='document':await route.fulfill(status=200,content_type='text/html',body=html)
                    else:await route.abort()
                await context.route('**/*',route)
                return context
            manager=AccountLoginManager(store,bridge,None,root=tmp_path/'managed',context_factory=factory,account_check=check)
            try:
                created=await manager.create('one','owner');session=manager.get(created['id'],'owner')
                for _ in range(100):
                    if session.state=='awaiting_login':break
                    await asyncio.sleep(.05)
                assert session.state=='awaiting_login'
                image=await manager.frame(session.id,'owner');assert image[:2]==b'\xff\xd8'
                # The state is published before the optional Sign in click finishes.
                # Wait for the fixture form instead of racing the next pointer event.
                await session.pages[session.active_page].locator('#secret').wait_for(state='visible',timeout=5000)
                await manager.input(session.id,'owner',{'action':'click','x':90,'y':105})
                await manager.input(session.id,'owner',{'action':'text','text':'synthetic-password-not-recorded'})
                await manager.input(session.id,'owner',{'action':'click','x':90,'y':170})
                await asyncio.wait_for(session.task,10)
                public=manager.public(session)
                assert public['state']=='succeeded' and public['credentials_synced'] and public['profile_saved']
                assert 'synthetic-session' not in json.dumps(public) and 'password' not in json.dumps(public).lower()
                assert store.get_account('one')['status']=='ready' and store.get_account('one')['balance']==42
                assert store.settings_for_account('one').session_id=='synthetic-session'
                auth=tmp_path/'managed'/'auth'/'one.enc';assert b'synthetic-session' not in auth.read_bytes()
                restored=json.loads(Fernet(store.profile_cipher_key('one')).decrypt(auth.read_bytes()))
                assert restored['cookies'][0]['value']=='synthetic-session'
                assert not list((tmp_path/'managed').rglob('*.png')) and not list((tmp_path/'managed').rglob('*.jpeg'))
                assert checked==['one']
            finally:await manager.shutdown();await browser.close()
    asyncio.run(run())
