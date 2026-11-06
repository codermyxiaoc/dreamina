"""Account-scoped interactive login panes backed by private browser contexts.

Frames and human input are transported only inside the authenticated admin API.
No password/OTP, frame image, OAuth URL query, or Cookie is logged or returned.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass,field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import APIRouter,Depends,Request,Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from playwright.async_api import async_playwright

from .browser_worker import profile_lock
from .browser_launch import parse_browser_args,managed_browser_args,browser_environment,IGNORED_DEFAULT_ARGS,is_browser_proxy_error
from .egress import Egress,direct_egress,parse_proxy_url
from .process_identity import process_snapshot
from .db_async import db_call
from .browser_driver import PAGE_URL
from .client import API_ORIGINS
from .config import ROOT,Settings
from .platform_store import PlatformError

VIEWPORT={'width':1100,'height':760}
TERMINAL={'succeeded','failed','expired','cancelled'}
FIRST_PARTY={'capcut.com','capcutapi.us','capcutapi.com'}
AUTH_NAVIGATION={'dreamina.capcut.com','www.capcut.com','accounts.google.com','appleid.apple.com',
                 'account.apple.com','www.facebook.com','m.facebook.com','www.tiktok.com',
                 'accounts.youtube.com','consent.google.com','passport.capcut.com'}
KEYS={'Enter','Tab','Shift+Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown',
      'Home','End','PageUp','PageDown','Control+A','Meta+A'}


def party_domain(domain):
    host=domain.lstrip('.').lower()
    return any(host==d or host.endswith('.'+d) for d in FIRST_PARTY)


def identity_response(url,payload):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not party_domain(parsed.hostname or '') or not parsed.path.startswith('/passport/') or '/account/info' not in parsed.path:return None
    if not isinstance(payload,dict) or any(str(payload.get(k,'0')) not in ('0','None','') for k in ('ret','code','status_code')):return None
    data=payload.get('data')
    if not isinstance(data,dict):return None
    uid=data.get('user_id_str') or data.get('user_id') or data.get('uid')
    if not isinstance(uid,(str,int)) or isinstance(uid,bool) or not re.fullmatch(r'[1-9]\d{3,30}',str(uid)):return None
    return str(uid)


def credentials_from_cookies(cookies,api_origin):
    # These cookies have already been filtered by Chromium for the actual API URL.
    values={}
    for cookie in sorted(cookies,key=lambda c:len(c.get('path','/'))):
        if not party_domain(cookie.get('domain','')):continue
        name,value=cookie.get('name'),cookie.get('value')
        if isinstance(name,str) and isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_!#$%&\x27*+.^`|~-]+',name) and not any(x in value for x in '\r\n;'):
            values[name]=value
    if not values.get('sessionid'):raise PlatformError('Login has not produced a usable Dreamina session.',code='session_missing',status=409)
    return {'session_id':values['sessionid'],'cookie':'; '.join(k+'='+v for k,v in values.items()),
            'api_origin':api_origin,'workspace_id':'','ms_token':'','device_id':'','signing':'none'}


@dataclass
class LoginSession:
    id:str
    account_id:str
    owner:str
    created_at:float
    expires_at:float
    state:str='starting'
    message:str='正在准备独立登录环境…'
    error_code:str|None=None
    context:object=None
    pages:dict=field(default_factory=dict)
    active_page:str|None=None
    task:object=None
    guard:object=None
    lock:object=field(default_factory=asyncio.Lock)
    responses:set=field(default_factory=set)
    identity:str|None=None
    identity_session_digest:str|None=None
    api_origin:str|None=None
    profile_saved:bool=False
    credentials_synced:bool=False
    worker_state:str|None=None
    dialog:object=None
    dialog_info:dict|None=None
    cancel_requested:bool=False
    cleanup_blocked:bool=False
    browser_process:dict|None=None
    context_closed:bool=False
    close_attempted:bool=False
    close_task:object=None


class AccountLoginManager:
    def __init__(self,store,bridge,supervisor,*,config=None,root=None,context_factory=None,account_check=None,clock=time.time):
        self.store=store;self.bridge=bridge;self.supervisor=supervisor;self.config=config or {}
        self.root=Path(root or ROOT/'outputs'/'managed-browsers');self.root.mkdir(parents=True,exist_ok=True)
        self.browser_args=parse_browser_args(self.config.get('LOGIN_BROWSER_ARGS'))
        self.context_factory=context_factory;self.account_check=account_check;self.clock=clock
        self.sessions={};self.by_account={};self.lock=asyncio.Lock();self.playwright=None
        self.max_sessions=int(self.config.get('LOGIN_MAX_SESSIONS','2'));self.ttl=int(self.config.get('LOGIN_SESSION_TTL','900'))
        self.close_timeout=20
        if not 1<=self.max_sessions<=10 or not 60<=self.ttl<=3600:raise ValueError('Invalid interactive login resource limits.')

    async def startup(self):
        # Complete or roll back file replacement after an interrupted DB transaction.
        for account in (await db_call(self.store.list_accounts, _store=self.store)):
            path=self.root/'auth'/(account['id']+'.enc');backup=path.with_suffix('.rollback')
            if backup.exists():
                if account['status']=='login_pending':
                    if backup.stat().st_size:backup.replace(path)
                    else:path.unlink(missing_ok=True);backup.unlink(missing_ok=True)
                else:backup.unlink()
        (await db_call(self.store.recover_pending_logins, _store=self.store))

    def public(self,session):
        tabs=[]
        for id_,page in session.pages.items():
            try:
                if page.is_closed():continue
                parsed=urlsplit(page.url);origin=parsed.hostname or '正在加载'
                tabs.append({'id':id_,'title':origin,'origin':f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme in ('http','https') else '',
                             'active':id_==session.active_page})
            except Exception:pass
        return {'id':session.id,'account_id':session.account_id,'state':session.state,'created_at':session.created_at,
                'expires_at':session.expires_at,'viewport':dict(VIEWPORT),'tabs':tabs,'message':session.message,
                'error_code':session.error_code,'profile_saved':session.profile_saved,'credentials_synced':session.credentials_synced,
                'worker_state':session.worker_state,'dialog':session.dialog_info}

    def get(self,id_,owner):
        session=self.sessions.get(id_)
        if not session or session.owner!=owner:raise PlatformError('Login session not found.',code='not_found',status=404)
        return session

    def for_account(self,account_id,owner):
        id_=self.by_account.get(account_id)
        if not id_:return None
        return self.public(self.get(id_,owner))

    async def create(self,account_id,owner):
        async with self.lock:
            for id_,expired in list(self.sessions.items()):
                if expired.state in TERMINAL and not expired.cleanup_blocked and self.clock()-expired.created_at>3600:
                    self.sessions.pop(id_,None)
                    if self.by_account.get(expired.account_id)==id_:self.by_account.pop(expired.account_id,None)
            old=self.sessions.get(self.by_account.get(account_id))
            if old and old.cleanup_blocked:
                await self._close_context(old)
            if old and old.state in TERMINAL and old.task and not old.task.done():
                raise PlatformError('The previous login session is finishing cleanup; try again shortly.',code='login_busy',status=409)
            if old and old.state not in TERMINAL:
                if old.owner!=owner:raise PlatformError('Another administrator session is logging into this account.',code='login_busy',status=409)
                return self.public(old)
            if sum(s.cleanup_blocked or s.state not in TERMINAL or s.task and not s.task.done() for s in self.sessions.values())>=self.max_sessions:
                raise PlatformError('All login slots are occupied; finish or close an existing panel first.',code='login_capacity',status=429)
            unfinished=any(a.get('account_id')==account_id and
                (a.get('blocking_jobs',sum(a.get('jobs',{}).get(status,0) for status in ('queued','claimed','submitted','uncertain')))>0)
                for a in (await db_call(self.bridge.accounts, _store=self.store)))
            if unfinished:raise PlatformError('This account has unfinished browser tasks.',code='account_busy',status=409)
            (await db_call(self.store.begin_browser_login, account_id, _store=self.store))
            now=self.clock();session=LoginSession(secrets.token_urlsafe(24),account_id,owner,now,now+self.ttl)
            self.sessions[session.id]=session;self.by_account[account_id]=session.id
            session.task=asyncio.create_task(self._run(session))
            return self.public(session)

    async def _open(self,session):
        if self.supervisor:await self.supervisor.stop(session.account_id)
        profile=self.root/'profiles'/session.account_id
        session.guard=profile_lock(profile);await session.guard.__aenter__()
        settings=(await db_call(self.store.settings_for_account, session.account_id, _store=self.store))
        egress=getattr(settings,'egress',None)
        if egress is None:
            egress=direct_egress()
            if settings.proxy:
                spec=parse_proxy_url(settings.proxy);egress=Egress(None,spec.canonical_url,spec.browser_proxy(),'legacy')
        options={'headless':str(self.config.get('LOGIN_BROWSER_HEADLESS','true')).lower()=='true',
                 'viewport':dict(VIEWPORT),'locale':egress.locale,'accept_downloads':False,
                 'extra_http_headers':{'Accept-Language':'en-US,en;q=0.9'},
                 'ignore_default_args':list(IGNORED_DEFAULT_ARGS),'env':browser_environment(),
                 'args':managed_browser_args(self.browser_args,proxied=bool(egress.playwright_proxy))}
        if egress.timezone:options['timezone_id']=egress.timezone
        channel=self.config.get('LOGIN_BROWSER_CHANNEL','chrome' if os.name=='nt' else 'chromium')
        if channel=='chrome':options['channel']='chrome'
        elif channel!='chromium':raise ValueError('Unsupported login browser channel.')
        if self.config.get('LOGIN_BROWSER_EXECUTABLE'):options['executable_path']=self.config['LOGIN_BROWSER_EXECUTABLE']
        if egress.playwright_proxy:options['proxy']=dict(egress.playwright_proxy)
        started=time.time()
        if self.context_factory:session.context=await self.context_factory(session.account_id,profile,options)
        else:
            if not self.playwright:self.playwright=await async_playwright().start()
            session.context=await self.playwright.chromium.launch_persistent_context(str(profile),**options)
        session.browser_process=await asyncio.to_thread(self._owned_browser,profile,started)
        if callable(getattr(session.context,'on',None)):
            session.context.on('close',lambda *_:setattr(session,'context_closed',True))
        auth=self.root/'auth'/(session.account_id+'.enc')
        if auth.exists():
            state=json.loads(Fernet((await db_call(self.store.profile_cipher_key, session.account_id, _store=self.store))).decrypt(auth.read_bytes()))
            await session.context.add_cookies([c for c in state.get('cookies',[]) if party_domain(c.get('domain',''))])
        await session.context.route('**/*',lambda route:self._navigation_guard(session,route))
        session.context.on('page',lambda page:self._register_page(session,page))
        for page in session.context.pages:self._register_page(session,page)
        if not session.pages:self._register_page(session,await session.context.new_page())
        page=session.pages[session.active_page]
        await page.goto(PAGE_URL,wait_until='domcontentloaded',timeout=45000)
        session.state='awaiting_login';session.message='请直接在下面的网页登录；密码和验证码不会保存到管理日志。'
        try:
            buttons=page.get_by_role('button',name=re.compile(r'^(Sign in|Log in)$',re.I))
            await buttons.first.wait_for(state='visible',timeout=5000)
            if await buttons.count()==1:await buttons.click(timeout=5000)
        except Exception:pass

    async def _navigation_guard(self,session,route):
        request=route.request
        try:
            top_level=False
            if request.is_navigation_request():
                try:top_level=request.frame.parent_frame is None
                except Exception:top_level=True  # OAuth popup may not have a frame yet.
            if top_level:
                p=urlsplit(request.url)
                if p.scheme!='https' or p.username or p.password or not (p.hostname in AUTH_NAVIGATION or party_domain(p.hostname or '')):
                    session.message='登录页请求了未允许的站点，导航已停止。请使用网站支持的登录方式。'
                    await route.abort();return
        except Exception:
            await route.abort();return
        await route.fallback()

    def _register_page(self,session,page):
        if any(p is page for p in session.pages.values()):return
        id_=secrets.token_hex(8);session.pages[id_]=page;session.active_page=id_
        page.on('close',lambda:self._page_closed(session,id_))
        page.on('dialog',lambda dialog:self._dialog(session,dialog))
        def response(response):
            task=asyncio.create_task(self._response(session,response));session.responses.add(task);task.add_done_callback(session.responses.discard)
        page.on('response',response)

    def _page_closed(self,session,id_):
        if session.active_page==id_:session.active_page=next((k for k,p in reversed(list(session.pages.items())) if not p.is_closed()),None)

    def _dialog(self,session,dialog):
        session.dialog=dialog;session.dialog_info={'type':dialog.type,'message':dialog.message[:1000]}

    async def _response(self,session,response):
        if session.state not in ('starting','awaiting_login'):return
        try:
            parsed=urlsplit(response.url);origin=f'{parsed.scheme}://{parsed.netloc}'
            if origin in API_ORIGINS and parsed.path.startswith('/mweb/') and response.status==200:session.api_origin=origin
            if '/passport/' in parsed.path and '/account/info' in parsed.path and response.status==200:
                identity=identity_response(response.url,await response.json())
                headers=await response.request.all_headers()
                cookie_values={k.strip():v for item in headers.get('cookie','').split(';') for k,sep,v in [item.strip().partition('=')] if sep}
                request_session=cookie_values.get('sessionid')
                if identity and request_session and session.state in ('starting','awaiting_login'):
                    session.identity=identity
                    session.identity_session_digest=hashlib.sha256(request_session.encode()).hexdigest()
        except Exception:pass

    async def _persist_login(self,session):
        session.state='verifying';session.message='正在验证账号身份、保存登录状态并同步查询凭据…'
        account=(await db_call(self.store.get_account, session.account_id, _store=self.store))
        origin=session.api_origin or ('https://dreamina-api.us.capcut.com' if account['region']=='US' else 'https://mweb-api-sg.capcut.com')
        cookies=await session.context.cookies([origin+'/mweb/v1/get_history_by_ids'])
        credentials=credentials_from_cookies(cookies,origin)
        if session.identity_session_digest!=hashlib.sha256(credentials['session_id'].encode()).hexdigest():
            raise PlatformError('登录身份与当前会话不一致，请重新打开登录面板核对账号。',code='identity_session_changed',status=409)
        old=(await db_call(self.store.settings_for_account, session.account_id, _store=self.store))
        settings=Settings(region=account['region'],proxy=old.proxy,account_id=session.account_id,egress=getattr(old,'egress',None),**credentials)
        checked=await self.account_check(session.account_id,settings) if self.account_check else {'status':'ready','balance':None}
        if checked.get('status')!='ready' and not checked.get('authenticated'):raise PlatformError('The new login could not be verified.',code='login_verification_failed',status=409)
        snapshot={'cookies':[c for c in await session.context.cookies() if party_domain(c.get('domain',''))]}
        raw=json.dumps(snapshot,ensure_ascii=False,separators=(',',':')).encode()
        if len(raw)>2_000_000:raise PlatformError('Browser authentication state exceeds the allowed size.',code='state_too_large',status=413)
        # Verify identity transactionally BEFORE replacing a previously valid auth snapshot.
        path=self.root/'auth'/(session.account_id+'.enc');path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp');temporary.write_bytes(Fernet((await db_call(self.store.profile_cipher_key, session.account_id, _store=self.store))).encrypt(raw))
        backup=path.with_suffix('.rollback')
        backup.write_bytes(path.read_bytes() if path.exists() else b'')
        for private in (temporary,backup):
            try:private.chmod(0o600)
            except OSError:pass
        cancelled=False
        try:
            commit=asyncio.create_task(db_call(self.store.complete_browser_login, session.account_id,credentials=credentials,identity=session.identity,
                        browser_profile=session.account_id,balance=checked.get('balance'),persist_state=lambda:temporary.replace(path), _store=self.store))
            while not commit.done():
                try:await asyncio.shield(commit)
                except asyncio.CancelledError:cancelled=True
            commit.result()
        except Exception:
            if backup.stat().st_size:backup.replace(path)
            else:path.unlink(missing_ok=True);backup.unlink(missing_ok=True)
            raise
        else:backup.unlink(missing_ok=True)
        finally:temporary.unlink(missing_ok=True)
        try:path.chmod(0o600)
        except OSError:pass
        session.credentials_synced=True;session.profile_saved=True;session.identity=None
        session.message='登录成功，浏览器状态已保存，查询凭据已自动加密同步。'
        if cancelled:raise asyncio.CancelledError()

    async def _finish(self,session):
        # Finish any human input already in flight before freezing credentials.
        async with session.lock:
            await self._persist_login(session)
        await self._close_context(session)
        if self.supervisor and str(self.config.get('LOGIN_AUTO_START_WORKER','true')).lower()=='true':
            try:
                worker=await self.supervisor.start(session.account_id);session.worker_state=worker['state']
                if worker['state']=='saved_capacity_wait':session.message+=' 当前执行节点已满，可稍后在账号列表启动。'
            except Exception:
                session.worker_state='start_failed';session.message+=' 执行节点尚未启动，可在账号列表重试启动。'
        session.state='succeeded'

    async def _run(self,session):
        try:
            await self._open(session)
            while self.clock()<session.expires_at:
                if session.cancel_requested:return
                if session.identity:
                    await self._finish(session);return
                await asyncio.sleep(1)
            session.state='expired';session.message='登录会话已超时，请重新打开登录面板。'
        except asyncio.CancelledError:
            if session.profile_saved:
                session.state='succeeded';session.message='登录状态及凭据已保存，可在账号列表检查或启动执行节点。'
            elif session.state not in TERMINAL:session.state='cancelled';session.message='登录面板已关闭，账号尚未完成登录。'
            raise
        except Exception as error:
            if is_browser_proxy_error(error):error=PlatformError('登录浏览器无法连接所配置的代理，请检查代理后重试。',code='proxy_error',status=503)
            session.state='failed';session.error_code=error.code if isinstance(error,PlatformError) else 'login_browser_unavailable'
            session.message=str(error) if isinstance(error,PlatformError) else '无法完成登录环境准备或状态保存，请检查浏览器运行环境后重试。'
        finally:
            try:
                if session.state!='succeeded':(await db_call(self.store.abort_browser_login, session.account_id, _store=self.store))
            finally:await self._close_context(session)

    async def _close_context(self,session):
        async with session.lock:
            await self._close_locked(session)

    async def _close_locked(self,session):
        tasks=list(session.responses)
        for task in tasks:task.cancel()
        if tasks:
            done,pending=await asyncio.wait(tasks,timeout=5)
            for task in done:
                if not task.cancelled():task.exception()
            for pending_task in pending:
                pending_task.cancel()
                pending_task.add_done_callback(lambda task:None if task.cancelled() else task.exception())
        if session.context:
            closed=session.context_closed
            if not closed and not session.close_attempted:
                # Playwright marks a context "closing" before waiting for the
                # close acknowledgement. A second close() may return early;
                # never interpret that shortcut as proof the browser exited.
                session.close_attempted=True;session.cleanup_blocked=True
                session.close_task=asyncio.create_task(session.context.close())
                done,_=await asyncio.wait({session.close_task},timeout=self.close_timeout)
                if done:
                    try:session.close_task.result();closed=True
                    except (Exception,asyncio.CancelledError):closed=False
                else:
                    session.close_task.cancel()
                    session.close_task.add_done_callback(lambda task:None if task.cancelled() else task.exception())
            elif not closed and session.close_task and session.close_task.done() and not session.close_task.cancelled():
                try:session.close_task.result();closed=True
                except Exception:closed=False
            if not closed:closed=await asyncio.to_thread(self._terminate_owned_browser,session.browser_process)
            if not closed:
                session.cleanup_blocked=True;session.error_code='login_cleanup_pending'
                session.message='登录浏览器未确认关闭，资料目录仍受保护；请检查该账号浏览器进程后再重新登录。'
                raise PlatformError(session.message,code='login_cleanup_pending',status=409)
            session.context=None;session.cleanup_blocked=False
        if session.guard:
            try:await session.guard.__aexit__(None,None,None)
            except Exception:pass
            session.guard=None

    @staticmethod
    def _owned_browser(profile,started):
        """Capture only a newly launched Linux Chromium child for this profile."""
        if not sys.platform.startswith('linux'):return None
        candidates=[];wanted='--user-data-dir='+str(Path(profile).resolve())
        try:processes=list(Path('/proc').iterdir())
        except OSError:return None
        for path in processes:
            if not path.name.isdigit():continue
            pid=int(path.name);snapshot=process_snapshot(pid) or {};argv=snapshot.get('argv') or []
            if not argv or Path(argv[0]).name not in ('chrome','chromium','headless_shell','chrome-headless-shell') or wanted not in argv:continue
            if any(a.startswith('--type=') for a in argv) or not snapshot.get('identity') or snapshot.get('created_at',0)<started-1:continue
            parent=pid;seen=set()
            try:
                while parent not in (0,1,os.getpid()) and parent not in seen:
                    seen.add(parent);parent=int((Path('/proc')/str(parent)/'stat').read_text().rsplit(')',1)[1].split()[1])
            except (OSError,ValueError,IndexError):continue
            if parent==os.getpid():candidates.append({'pid':pid,'identity':snapshot['identity'],'profile':str(Path(profile).resolve())})
        return candidates[0] if len(candidates)==1 else None

    @staticmethod
    def _terminate_owned_browser(record):
        """PID-fd prevents PID reuse; never kill by a stale numeric PID alone."""
        if not isinstance(record,dict) or not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):return False
        pid=record.get('pid');identity=record.get('identity');profile=record.get('profile')
        if type(pid) is not int or pid<=0 or not isinstance(identity,str) or not isinstance(profile,str):return False
        fd=None
        try:
            before=process_snapshot(pid)
            if before is None:return not Path('/proc',str(pid)).exists()
            if before.get('identity')!=identity or '--user-data-dir='+profile not in before.get('argv',[]):return False
            fd=os.pidfd_open(pid)
            after=process_snapshot(pid)
            if not after or after.get('identity')!=identity:return False
            signal.pidfd_send_signal(fd,signal.SIGTERM)
            import select
            poller=select.poll();poller.register(fd,select.POLLIN)
            if poller.poll(2000):return True
            signal.pidfd_send_signal(fd,signal.SIGKILL)
            return bool(poller.poll(2000))
        except ProcessLookupError:return True
        except (OSError,ValueError,TypeError):return False
        finally:
            if fd is not None:os.close(fd)

    def page(self,session):
        if session.state not in ('starting','awaiting_login'):raise PlatformError('Login input is no longer active.',code='login_not_active',status=409)
        page=session.pages.get(session.active_page)
        if not page or page.is_closed():raise PlatformError('Login page is still loading.',code='page_loading',status=409)
        return page

    async def frame(self,id_,owner):
        session=self.get(id_,owner)
        async with session.lock:
            page=self.page(session)
            return await page.screenshot(type='jpeg',quality=75,timeout=10000)

    async def input(self,id_,owner,data):
        session=self.get(id_,owner)
        if not isinstance(data,dict) or len(json.dumps(data))>20000:raise PlatformError('Invalid login input.')
        async with session.lock:
            page=self.page(session);action=data.get('action')
            if action=='click':
                x,y=data.get('x'),data.get('y');button=data.get('button','left')
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (x,y)) or not 0<=x<VIEWPORT['width'] or not 0<=y<VIEWPORT['height'] or button not in ('left','right','middle'):raise PlatformError('Invalid pointer coordinates.')
                await page.mouse.click(x,y,button=button)
            elif action=='scroll':
                dx,dy=data.get('delta_x',0),data.get('delta_y',0)
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or abs(v)>2000 for v in (dx,dy)):raise PlatformError('Invalid scroll amount.')
                await page.mouse.wheel(dx,dy)
            elif action=='drag':
                coordinates=[data.get(k) for k in ('from_x','from_y','to_x','to_y')]
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in coordinates):raise PlatformError('Invalid drag coordinates.')
                x,y,tx,ty=coordinates
                if not all((0<=x<VIEWPORT['width'],0<=tx<VIEWPORT['width'],0<=y<VIEWPORT['height'],0<=ty<VIEWPORT['height'])):raise PlatformError('Drag is outside the login view.')
                await page.mouse.move(x,y);await page.mouse.down()
                try:await page.mouse.move(tx,ty,steps=20)
                finally:await page.mouse.up()
            elif action=='text':
                value=data.get('text')
                if not isinstance(value,str) or not 1<=len(value)<=4096:raise PlatformError('Invalid text input.')
                await page.keyboard.insert_text(value)
            elif action=='key':
                if data.get('key') not in KEYS:raise PlatformError('This key is not available in the login panel.')
                await page.keyboard.press(data['key'])
            elif action=='tab':
                if data.get('tab_id') not in session.pages or session.pages[data['tab_id']].is_closed():raise PlatformError('Login tab unavailable.')
                session.active_page=data['tab_id']
            elif action=='reload':await page.reload(wait_until='domcontentloaded',timeout=30000)
            elif action=='back':await page.go_back(wait_until='domcontentloaded',timeout=30000)
            elif action=='dialog':
                if session.dialog is None or type(data.get('accept')) is not bool:raise PlatformError('No interactive dialog is pending.')
                if data['accept']:await session.dialog.accept()
                else:await session.dialog.dismiss()
                session.dialog=None;session.dialog_info=None
            else:raise PlatformError('Unsupported login action.')
        return {'ok':True}

    async def cancel(self,id_,owner):
        session=self.get(id_,owner)
        if session.state in TERMINAL:
            if session.cleanup_blocked:await self._close_context(session)
            return self.public(session)
        session.cancel_requested=True;session.task.cancel()
        await asyncio.gather(session.task,return_exceptions=True)
        # A task cancelled before its first instruction never runs its finally block.
        if session.state not in TERMINAL:
            session.state='cancelled';session.message='登录面板已关闭，账号尚未完成登录。'
        try:
            if session.state!='succeeded':(await db_call(self.store.abort_browser_login, session.account_id, _store=self.store))
        finally:await self._close_context(session)
        return self.public(session)

    async def shutdown(self):
        for session in list(self.sessions.values()):
            if session.task and not session.task.done():session.task.cancel()
        await asyncio.gather(*(s.task for s in self.sessions.values() if s.task),return_exceptions=True)
        blocked=False;failed=False
        for session in self.sessions.values():
            if session.state not in TERMINAL:session.state='cancelled'
            try:
                if session.state!='succeeded':(await db_call(self.store.abort_browser_login, session.account_id, _store=self.store))
            except Exception:failed=True
            try:await self._close_context(session)
            except PlatformError as error:
                if error.code!='login_cleanup_pending':raise
                blocked=True
        if blocked:
            # One unconfirmed orphan must not prevent closing other owned
            # sessions, nor release the blocked profile's exclusivity guard.
            raise PlatformError('Some login browsers have not confirmed shutdown; their profiles remain protected.',code='login_cleanup_pending',status=409)
        if self.playwright:await self.playwright.stop();self.playwright=None
        if failed:raise PlatformError('Login browsers closed but account state needs inspection.',code='login_cleanup_state_failed',status=409)


def create_account_login_router(manager,admin_dependency):
    class LoginRoute(APIRoute):
        def get_route_handler(self):
            original=super().get_route_handler()
            async def handle(request):
                try:response=await original(request)
                except PlatformError as error:response=JSONResponse({'error':{'code':error.code,'message':str(error)}},status_code=error.status)
                response.headers['Cache-Control']='no-store, max-age=0'
                response.headers['X-Content-Type-Options']='nosniff'
                return response
            return handle
    router=APIRouter(prefix='/api/admin',tags=['Account login'],route_class=LoginRoute)
    def owner(principal):return hashlib.sha256(principal['csrf_token'].encode()).hexdigest()
    async def execute(action):
        try:return await action
        except PlatformError as error:return JSONResponse({'error':{'code':error.code,'message':str(error)}},status_code=error.status)
        except Exception:return JSONResponse({'error':{'code':'login_action_failed','message':'登录页面操作暂不可用，请检查会话状态。'}},status_code=409)

    @router.post('/accounts/{account_id}/login-sessions')
    async def start(account_id:str,principal=Depends(admin_dependency)):
        return await execute(manager.create(account_id,owner(principal)))

    @router.get('/accounts/{account_id}/login-sessions')
    async def current(account_id:str,principal=Depends(admin_dependency)):
        try:return {'session':manager.for_account(account_id,owner(principal))}
        except PlatformError as error:return JSONResponse({'error':{'code':error.code,'message':str(error)}},status_code=error.status)

    @router.get('/login-sessions/{session_id}')
    async def status(session_id:str,principal=Depends(admin_dependency)):
        try:return manager.public(manager.get(session_id,owner(principal)))
        except PlatformError as error:return JSONResponse({'error':{'code':error.code,'message':str(error)}},status_code=error.status)

    @router.get('/login-sessions/{session_id}/frame')
    async def frame(session_id:str,principal=Depends(admin_dependency)):
        try:
            data=await manager.frame(session_id,owner(principal))
            return Response(data,media_type='image/jpeg',headers={'Cache-Control':'no-store, max-age=0','Pragma':'no-cache','X-Content-Type-Options':'nosniff'})
        except PlatformError as error:return JSONResponse({'error':{'code':error.code,'message':str(error)}},status_code=error.status)
        except Exception:return Response(status_code=409)

    @router.post('/login-sessions/{session_id}/input')
    async def input_(session_id:str,request:Request,principal=Depends(admin_dependency)):
        parts=[];size=0
        async for part in request.stream():
            size+=len(part)
            if size>20000:return Response(status_code=413)
            parts.append(part)
        raw=b''.join(parts)
        try:data=json.loads(raw)
        except ValueError:return Response(status_code=400)
        return await execute(manager.input(session_id,owner(principal),data))

    @router.delete('/login-sessions/{session_id}')
    async def cancel(session_id:str,principal=Depends(admin_dependency)):
        return await execute(manager.cancel(session_id,owner(principal)))

    @router.post('/accounts/{account_id}/worker/start')
    async def start_worker(account_id:str,principal=Depends(admin_dependency)):
        return await execute(manager.supervisor.start(account_id))

    @router.post('/accounts/{account_id}/worker/stop')
    async def stop_worker(account_id:str,principal=Depends(admin_dependency)):
        return await execute(manager.supervisor.stop(account_id))

    return router
