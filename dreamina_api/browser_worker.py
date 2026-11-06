"""Persistent, extension-free Chrome worker for the existing queue protocol."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from urllib.parse import urlsplit
import uuid

import httpx
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright, TimeoutError as BrowserTimeoutError

from .browser_driver import NativeUI, UIUnavailable, PAGE_URL, BrowserProxyUnavailable,ReferenceCountMismatch,classify_browser_proxy_errors
from .browser_launch import parse_browser_args,managed_browser_args,browser_environment,IGNORED_DEFAULT_ARGS,is_browser_proxy_error
from .egress import Egress,direct_egress,parse_proxy_url
from .config import ROOT
from .audit import sanitize
from .process_identity import process_snapshot


class WorkerAPIError(RuntimeError):
    def __init__(self,status,message='Worker service request failed.',code='worker_http_error'):
        self.status=status;self.safe_message=message;self.error_code=code
        super().__init__(f'Worker API HTTP {status}; {message}')


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
    temp.replace(path)
    try:path.chmod(0o600)
    except OSError:pass


def process_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    if os.name=='nt':
        # Windows os.kill(pid, 0) is NOT a POSIX liveness probe: do not call it.
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()==5
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except PermissionError:return True
    return True


def safe_config(path,account_id):
    path=Path(path).resolve();data=json.loads(path.read_text(encoding='utf-8-sig'))
    base=data.get('server_url','').rstrip('/');parsed=urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('server_url must be an origin without credentials, path, query or fragment.')
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('Remote worker requires HTTPS; HTTP is limited to loopback.')
    key=data.get('worker_key') or os.environ.get('DREAMINA_WORKER_KEY','')
    if not isinstance(key,str) or len(key)<24 or any(c.isspace() for c in key):raise ValueError('A worker key of at least 24 non-whitespace characters is required.')
    accounts=data.get('accounts',[])
    account=next((a for a in accounts if a.get('id')==account_id),None)
    if not account or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',account_id):raise ValueError('Account is not present in the worker configuration.')
    raw=Path(account.get('profile_dir',f'profiles/{account_id}'))
    profile=(path.parent/raw).resolve() if not raw.is_absolute() else raw.resolve()
    # Never point automation at the user's everyday browser profile.
    if profile.name.lower() in ('default','profile 1','profile 2','profile 3') and 'google/chrome/user data' in profile.as_posix().lower():raise ValueError('Use a dedicated profile directory, not the normal Chrome profile.')
    if profile.name.lower()=='user data':raise ValueError('Use a separate automation profile directory.')
    state=path.parent/'worker-state'/account_id
    channel=account.get('channel','chrome')
    if channel not in ('chrome','chromium'):raise ValueError('channel must be chrome or chromium.')
    if not isinstance(account.get('headless',False),bool):raise ValueError('headless must be a JSON boolean.')
    retries=account.get('prepare_attempts',2)
    if type(retries) is not int or not 1<=retries<=3:raise ValueError('prepare_attempts must be between 1 and 3.')
    concurrency=account.get('concurrency',1)
    if type(concurrency) is not int or not 1<=concurrency<=8:raise ValueError('concurrency must be between 1 and 8.')
    deadlines={}
    for name,default,maximum in (('preparation_timeout_seconds',180,600),('submission_timeout_seconds',60,120),('generation_timeout_seconds',600,3600)):
        value=account.get(name,default)
        if type(value) is not int or not 1<=value<=maximum:raise ValueError(name+' is outside its supported range.')
        deadlines[name]=value
    refresh=account.get('entry_refresh_seconds',300)
    if type(refresh) is not int or not 30<=refresh<=3600:raise ValueError('entry_refresh_seconds must be between 30 and 3600.')
    reload_jobs=account.get('page_reload_jobs',20)
    if type(reload_jobs) is not int or not 1<=reload_jobs<=100:raise ValueError('page_reload_jobs must be between 1 and 100.')
    clean_cache=account.get('clean_cache_on_start',False)
    if type(clean_cache) is not bool:raise ValueError('clean_cache_on_start must be a JSON boolean.')
    return {'server_url':base,'worker_key':key,'account':account_id,'profile_dir':profile,'state_dir':state,
            'channel':channel,'headless':bool(account.get('headless',False)),
            'args':parse_browser_args(account.get('args')),
            'prepare_attempts':retries,
            'concurrency':concurrency,'entry_refresh_seconds':refresh,'page_reload_jobs':reload_jobs,'clean_cache_on_start':clean_cache,**deadlines,
            'proxy':account.get('proxy'),'executable_path':account.get('executable_path'),
            'proxy_id':account.get('proxy_id'),'egress_managed':account.get('egress_managed',False),
            'instance_id':account.get('instance_id'),
            'encrypted_state_file':Path(account['encrypted_state_file']).resolve() if account.get('encrypted_state_file') else None}


@asynccontextmanager
async def profile_lock(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True);lock=directory/'.dreamina-worker.lock'
    if lock.exists():
        with lock.open('rb') as source:original=source.read(4097)
        stamp=lock.stat()
        try:
            if len(original)>4096:raise ValueError()
            value=json.loads(original.decode('utf-8'))
            record={'pid':value} if type(value) is int else value
            if not isinstance(record,dict) or type(record.get('pid')) is not int or record['pid']<=0:raise ValueError()
            old_identity=record.get('process_identity')
            if old_identity is not None and (not isinstance(old_identity,str) or not old_identity):raise ValueError()
            pid=record['pid']
        except (ValueError,UnicodeError):raise RuntimeError('Profile lock metadata is invalid; review it before starting another worker.') from None
        if process_alive(pid):
            snapshot=process_snapshot(pid) or {}
            current=snapshot.get('identity');created=snapshot.get('created_at')
            changed=bool(old_identity and isinstance(current,str) and current!=old_identity)
            newer=not old_identity and isinstance(created,(int,float)) and not isinstance(created,bool) and math.isfinite(created) and created>stamp.st_mtime+1
            if not changed and not newer:
                raise RuntimeError('The profile is already owned by another worker process.')
        # Never remove a lock replaced while its owner was being inspected.
        current_stamp=lock.stat()
        if lock.read_bytes()!=original or (current_stamp.st_ino,current_stamp.st_mtime_ns)!=(stamp.st_ino,stamp.st_mtime_ns):
            raise RuntimeError('Profile lock changed during inspection; retry without touching its owner.')
        lock.unlink()
    birth=(process_snapshot(os.getpid()) or {}).get('identity')
    marker=json.dumps({'pid':os.getpid(),'process_identity':birth,'token':uuid.uuid4().hex},sort_keys=True).encode()
    fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(marker);f.flush();os.fsync(f.fileno())
    try:yield json.loads(marker)
    finally:
        try:
            if lock.read_bytes()==marker:lock.unlink()
        except FileNotFoundError:pass


class Worker:
    def __init__(self,config):
        # Read once before Playwright's driver/Chromium children are spawned.
        # Slot objects share the owner's context and do not need these secrets.
        self._profile_key=os.environ.pop('DREAMINA_PROFILE_KEY',None)
        self._egress_environment=os.environ.pop('DREAMINA_EGRESS',None)
        os.environ.pop('DREAMINA_WORKER_KEY',None)
        self.config=config;self.state_dir=config['state_dir'];self.state_dir.mkdir(parents=True,exist_ok=True)
        self.journal=self.state_dir/'current.json';self.status_file=self.state_dir/'status.json'
        identity=self.state_dir/'identity.json'
        if not identity.exists():write_json(identity,{'worker_id':'native-'+uuid.uuid4().hex})
        self.worker_id=json.loads(identity.read_text())['worker_id']
        self.http=httpx.AsyncClient(base_url=config['server_url'],timeout=30,follow_redirects=False,trust_env=False)
        self.context=None;self.entry=None;self.active=None;self._last_status=None
        self._snapshot_write_allowed=False
        self.resume_job=None
        self.recovered_claim=None;self._claim_reserved=False
        self._owns_context=True;self._slots=[];self._active_page=None;self._last_status_data={}
        self._preparation_lock=asyncio.Lock()
        self._retained_page=None;self._retained_bootstrap=set();self._retained_kind=None
        self._entry_checked_at=time.monotonic()
        self._closing_context=False;self._browser_failed=False;self._browser_failure_reason=None
        self._recovery_window_jobs=set();self._retained_uses=0
        self._entry_readiness={'ready':False,'checked_at':0.0};self._pool_control=None
        self._entry_parked=False;self._entry_waking=False;self._entry_wake_requested=False
        self._retained_parked=False;self._last_work_at=time.monotonic();self._park_failures=0
        self._park_retry_at=0.0
        self._idle_park_seconds=60;self._park_probe_seconds=45

    def _browser_problem(self,reason):
        owner=getattr(self,'_pool_owner',self)
        if owner._closing_context:return
        owner._browser_failed=True;owner._browser_failure_reason=reason
        owner._entry_readiness={'ready':False,'checked_at':time.time()}
        if owner._pool_control is not None:
            owner._pool_control['readiness']={'ready':False,'reason':'Browser context is unavailable.'}
            owner._pool_control['browser_failure']=reason;owner._pool_control['stop'].set()

    def _watch_page(self,page):
        if callable(getattr(page,'on',None)):
            page.on('crash',lambda *_:self._browser_problem('browser_page_crashed'))

    def _startup_journal_ids(self):
        ids=set()
        for path in [self.journal,*self.state_dir.glob('slots/[1-8]/current.json')]:
            if not path.exists():continue
            try:
                state=json.loads(path.read_text());job=state.get('job') or {}
                if state.get('phase') not in ('finished','idle','migrated') and isinstance(job.get('id'),str):ids.add(job['id'])
            except (OSError,ValueError,AttributeError):pass
        return ids

    def _closed_recovery_receipt(self,job,value):
        value=dict(value)
        if ((job.get('claim_protocol_version') or 0)>=6 and job['id'] in self._recovery_window_jobs
                and self._active_page is None and value.get('status')=='uncertain'):
            value['submission_window_closed']=True
        return value

    def status(self,state,**detail):
        data={'state':state,'account_id':self.config['account'],'worker_id':self.worker_id,'pid':os.getpid(),
              'instance_id':self.config.get('instance_id'),'updated_at':time.time(),**detail}
        write_json(self.status_file,data)
        self._last_status_data=data
        marker=json.dumps(data,sort_keys=True,default=str)
        if state!=self._last_status:print(json.dumps(data,ensure_ascii=False),flush=True);self._last_status=state

    async def api(self,path,body=None,*,method='POST',headers=None):
        # Claim is never retried: a lost response may already own a job.
        repeatable=method=='GET' or path in ('/heartbeat','/slots/recover') or path.endswith(('/status','/telemetry','/report','/preflight','/activate','/prepare','/submission-turn'))
        attempts=3 if repeatable else 1
        for attempt in range(attempts):
            try:
                response=await self.http.request(method,'/v1/chrome'+path,json=body,
                    headers={'X-Chrome-Bridge-Key':self.config['worker_key'],**(headers or {})})
                if response.is_redirect:raise RuntimeError('Worker API redirects are not followed.')
                if response.status_code in (408,429,500,502,503,504) and attempt+1<attempts:
                    await asyncio.sleep(min(2**attempt,4));continue
                if not response.is_success:
                    message='Worker service request failed; request values omitted.';code='worker_http_error'
                    if path.endswith(('/preflight','/activate')):
                        try:detail=response.json().get('detail')
                        except (ValueError,AttributeError):detail=None
                        if isinstance(detail,str):message=str(sanitize(detail))[:300]
                        elif isinstance(detail,dict) and detail.get('code')=='proxy_error':message='The configured proxy is unavailable; no generation was submitted.';code='proxy_error'
                        if path.endswith('/preflight') and code!='proxy_error':code='credit_check_unavailable' if response.status_code>=500 else 'credit_preflight_rejected'
                        elif path.endswith('/activate'):code='activation_denied' if response.status_code<500 else 'activation_check_unavailable'
                    if response.status_code==401:code='worker_auth_failed';message='Worker authentication failed; restore this node connection.'
                    raise WorkerAPIError(response.status_code,message,code)
                return response if method=='GET' else response.json()
            except httpx.TransportError:
                if attempt+1>=attempts:raise
                await asyncio.sleep(min(2**attempt,4))

    async def heartbeat(self,ready,reason=''):
        owner=getattr(self,'_pool_owner',self);entry=owner._entry_readiness
        checked=entry.get('checked_at',0.0);verified=entry.get('ready') is True and -5<=time.time()-checked<=120 and not owner._browser_failed
        parked=owner._entry_parked;entry_ready=verified and not parked
        wakeable=bool(ready and parked and verified and not self.active and not self._claim_reserved and not getattr(self,'_retired',False))
        response=await self.api('/heartbeat',{'account_id':self.config['account'],'worker_id':self.worker_id,
            'page_url':PAGE_URL,'ready':bool(ready and entry_ready and not getattr(self,'_retired',False)),'protocol_version':8,'driver':'native','reason':reason[:300],
            'entry_ready':entry_ready,'entry_checked_at':checked,
            'entry_parked':parked,'wakeable':wakeable,
            'owner_id':self.config.get('owner_id',self.worker_id),'slot_index':self.config.get('slot_index',1),
            'slot_count':max(self.config.get('concurrency',1),self.config.get('slot_index',1))})
        if isinstance(response,dict):
            owner._apply_slot_limit(response.get('concurrency_limit'))
            if response.get('retired') is True:self._retired=True
            if response.get('wake_required') is True:owner._entry_wake_requested=True
        return response

    def _idle_for_parking(self):
        if self._browser_failed or time.monotonic()<self._park_retry_at or time.monotonic()-self._last_work_at<self._idle_park_seconds:return False
        try:
            return all(not slot.active and not slot._claim_reserved and slot.resume_job is None and slot.recovered_claim is None
                       and self._pending_journal(slot.journal) is None for slot in self._slots)
        except (OSError,ValueError):return False

    def _can_wake(self):
        return (self._entry_parked and not self._browser_failed and self._entry_readiness.get('ready') is True
                and -5<=time.time()-self._entry_readiness.get('checked_at',0)<=120)

    async def _announce_entry_check(self):
        # Persist a real nonparked check before declaring capacity wakeable.
        # Failed internal telemetry never invents or extends a server lease.
        try:
            await asyncio.wait_for(asyncio.gather(*(slot.heartbeat(not slot.active and not slot._retired,
                'Entry readiness was checked before idle parking.') for slot in self._slots),return_exceptions=True),5)
        except Exception:self._park_failures+=1

    async def _park_idle_pages_locked(self,control):
        """Caller owns both the claim lock and preparation lock."""
        if self._entry_parked or not self._idle_for_parking() or self._entry_readiness.get('ready') is not True:return False
        await self._announce_entry_check()
        targets=[(None,self.entry)]
        targets.extend((slot,slot._retained_page) for slot in self._slots if slot._retained_page is not None and slot._retained_page is not self.entry and slot._active_page is None)
        async def park(slot,page):
            if page is None:return False
            try:
                if page.is_closed():return False
                await asyncio.wait_for(page.goto('about:blank',wait_until='domcontentloaded',timeout=10000),12)
                if page.url!='about:blank':return False
                if slot is not None:
                    slot._retained_parked=True;slot._retained_bootstrap=set()
                return True
            except Exception:
                # A navigation may have committed before its acknowledgement.
                if getattr(page,'url',None)=='about:blank':
                    if slot is not None:slot._retained_parked=True;slot._retained_bootstrap=set()
                    return True
                return False
        results=await asyncio.gather(*(park(slot,page) for slot,page in targets))
        self._park_failures+=sum(not value for value in results)
        if not all(results):self._park_retry_at=time.monotonic()+min(120,5*2**min(self._park_failures,5))
        if results[0]:
            self._entry_parked=True;self._entry_waking=False
            control['readiness']={'ready':False,'reason':'Entry is parked; a fresh verified profile can wake before preparation.'}
            return True
        return False

    async def _wake_entry_locked(self,control=None):
        """Wake under the existing preparation budget, before any UI action."""
        if not self._entry_parked:return self._entry_readiness
        self._entry_waking=True;self._entry_wake_requested=False
        self.status('browser_waking',reason='Restoring the idle entry page and checking its actual login controls.')
        try:
            # Periodic wake must not hold preparation behind a full navigation
            # timeout plus a second full readiness timeout.
            async with asyncio.timeout(45):
                await self.entry.goto(PAGE_URL+'?type=video',wait_until='domcontentloaded',timeout=30000)
                state=await asyncio.wait_for(NativeUI(self.entry,self.noop_telemetry).wait_ready(timeout=25),30)
        except asyncio.CancelledError:
            self._entry_waking=False;self._entry_wake_requested=True
            raise
        except Exception as error:
            self._park_failures+=1
            state={'ready':False,'reason':'Account entry could not wake before preparation.'}
            if is_browser_proxy_error(error):state['reason']='The configured browser proxy could not connect.';state['error_code']='proxy_error'
        self._entry_parked=False;self._entry_waking=False
        self._entry_checked_at=time.monotonic()
        self._entry_readiness={'ready':state.get('ready') is True,'checked_at':time.time()}
        if control is not None:control['readiness']=state
        return state

    def _apply_slot_limit(self,value):
        """Admission only: assigned work keeps its original recovery journal."""
        current=self.config.get('concurrency',1)
        if type(value) is not int or not 1<=value<current:return
        self.config['concurrency']=value
        for slot in self._slots:
            slot.config['concurrency']=value
            if slot.config['slot_index']>value:slot._retired=True

    def _read_slot_retirement(self):
        path=self.state_dir/'slot-retirement.json'
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(self.state_dir.resolve()) or path.stat().st_size>4096:return
            value=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value,dict) or not self.config.get('instance_id') or value.get('instance_id')!=self.config['instance_id']:return
            self._apply_slot_limit(value.get('concurrency'))
        except (OSError,ValueError):return

    async def _finish_retired_slot(self,slot):
        if slot.active or self._pending_journal(slot.journal):return False
        for page in (slot._active_page,slot._retained_page):
            if page is None:continue
            if page is slot.entry:return False
            try:
                await asyncio.wait_for(page.close(),5)
                if not callable(getattr(page,'is_closed',None)) or page.is_closed() is not True:return False
            except Exception:return False
        slot._active_page=None;slot._retained_page=None;slot._retained_bootstrap=set();slot._retained_kind=None;slot._retained_uses=0
        write_json(slot.state_dir/'retired.json',{'worker_id':slot.worker_id,'slot_index':slot.config['slot_index'],'retired_at':time.time()})
        slot.status('retired',reason='Slot reduced; task pages closed and original receipts retained.')
        try:await slot.api('/workers/retire',{'account_id':slot.config['account'],'worker_id':slot.worker_id})
        except Exception:pass  # Offline registration expires; never discard paid-task receipts.
        if slot in self._slots:self._slots.remove(slot)
        await slot.http.aclose()
        return True

    async def telemetry(self,job,values):
        body={k:v for k,v in values.items() if v is not None}
        await self.api(f"/jobs/{job['id']}/telemetry",{'worker_id':self.worker_id,'claim_token':job['claim_token'],**body})

    @staticmethod
    def normalized_outcome(result):
        result=dict(result)
        if result.get('status')=='failed' and result.get('submit_id'):
            ret=result.get('ret',result.get('upstream_ret'))
            if isinstance(ret,(str,int)) and not isinstance(ret,bool) and str(ret)!='0' and re.fullmatch(r'[A-Za-z0-9_-]{1,40}',str(ret)):
                result.update(upstream_ret=str(ret),error_code='upstream_rejected')
            else:
                result.update(status='uncertain',error_code='submission_unconfirmed',generation_not_activated=False,
                              error='A submitted request has no confirmed rejection; query the original task instead of regenerating.')
        elif result.get('status')=='failed' and result.get('generation_not_activated') is False:
            result.update(status='uncertain',error_code='submission_unconfirmed',
                          error='The browser was activated but no authoritative rejection was observed; reconcile the original task without resubmitting.')
        return result

    async def report(self,job,result):
        result=self.normalized_outcome(result)
        body={'worker_id':self.worker_id,'claim_token':job['claim_token'],'status':result['status']}
        for key in ('submit_id','api_origin','error','error_code','generation_not_activated','upstream_ret','upstream_error','submission_window_closed'):
            if result.get(key) is not None:body[key]=result[key]
        if result['status'] in ('submitted','uncertain') and result.get('submit_id') and result.get('reference_bindings') is not None:
            body['reference_bindings']=result['reference_bindings']
        async def publish():
            try:return await self.api(f"/jobs/{job['id']}/report",body)
            except RuntimeError as error:
                if 'HTTP 409' not in str(error):raise
                remote=await self.api(f"/jobs/{job['id']}/status",{'worker_id':self.worker_id,'claim_token':job['claim_token']})
                same_bindings='reference_bindings' not in body or remote.get('reference_bindings')==body['reference_bindings']
                acknowledged=remote.get('status') in ('completed','failed')
                # A request-observed journal may recover after the prior report
                # committed centrally but its local acknowledgement was lost.
                if body['status']=='uncertain' and body.get('submit_id') and remote.get('status')=='submitted':
                    acknowledged=remote.get('reference_bindings')==body.get('reference_bindings')
                if body.get('error_code')=='generation_timeout':
                    acknowledged=acknowledged and remote.get('error_code')=='generation_timeout'
                if acknowledged and (not body.get('submit_id') or remote.get('submit_id')==body['submit_id']) and same_bindings:return remote
                raise
        try:write_json(self.journal,{'phase':'reporting','job':job,'result':body,'confirmation':result})
        except OSError:
            await publish()
            raise RuntimeError('Local receipt storage failed; the central receipt was saved. Worker will stop without claiming another task.') from None
        acknowledged=await publish()
        write_json(self.journal,{'phase':'reported','job':job,'result':body,'confirmation':result})
        return acknowledged

    async def seed_local_session(self):
        """Explicit one-time migration of the already user-supplied local Cookie."""
        marker=self.config['profile_dir']/'.session-imported'
        if marker.exists():return
        from .config import Settings
        settings=Settings.load();values={}
        if settings.session_id:values.update({k:settings.session_id for k in ('sessionid','sessionid_ss','sid_tt')})
        for field in settings.cookie.split(';'):
            key,sep,value=field.strip().partition('=')
            if sep and re.fullmatch(r'[A-Za-z0-9_!#$%&\x27*+.^`|~-]+',key):values[key]=value
        if not values:raise ValueError('No user-provided local session is configured for import.')
        cookies=[]
        for name,value in values.items():
            cookie={'name':name,'value':value,'secure':True,'sameSite':'None','httpOnly':name.startswith(('sessionid','sid_','uid_'))}
            if name.startswith('__Host-'):cookie['url']='https://dreamina.capcut.com/'
            else:cookie.update(domain='.capcut.com',path='/')
            cookies.append(cookie)
        await self.context.add_cookies(cookies);marker.write_text('Imported once; subsequent session state is browser-owned.',encoding='utf-8')

    @classify_browser_proxy_errors
    async def open(self,pw,seed=False):
        self._snapshot_write_allowed=False
        startup_jobs=self._startup_journal_ids();owned_guard=False
        try:
            guard=self.config.get('_profile_guard');current=json.loads((self.config['profile_dir']/'.dreamina-worker.lock').read_text())
            owned_guard=bool(guard and current==guard and current.get('pid')==os.getpid())
        except (OSError,ValueError):pass
        env=self._egress_environment
        if env:
            egress=Egress.from_worker_json(env)
            if self.config.get('egress_managed') and egress.proxy_id!=self.config.get('proxy_id'):raise ValueError('Worker proxy identity differs from its account configuration.')
        elif self.config.get('egress_managed') or self.config.get('proxy_id'):
            raise ValueError('Managed worker egress environment is missing; direct fallback is disabled.')
        else:
            egress=direct_egress()
            if self.config.get('proxy'):
                spec=parse_proxy_url(self.config['proxy'])
                egress=Egress(None,spec.canonical_url,spec.browser_proxy(),'legacy')
        self._egress_environment=None
        options={'headless':self.config['headless'],'locale':egress.locale,'viewport':{'width':1280,'height':900},'accept_downloads':True,'service_workers':'block',
                 'args':managed_browser_args(self.config.get('args'),proxied=bool(egress.playwright_proxy)),
                 'ignore_default_args':list(IGNORED_DEFAULT_ARGS),'env':browser_environment(),
                 'extra_http_headers':{'Accept-Language':'en-US,en;q=0.9'}}
        if egress.timezone:options['timezone_id']=egress.timezone
        if self.config['channel']=='chrome':options['channel']='chrome'
        if self.config['executable_path']:options['executable_path']=self.config['executable_path']
        if egress.playwright_proxy:options['proxy']=dict(egress.playwright_proxy)
        self.context=await pw.chromium.launch_persistent_context(str(self.config['profile_dir']),**options)
        if callable(getattr(self.context,'on',None)):
            self.context.on('close',lambda *_:self._browser_problem('browser_context_closed'))
            self.context.on('page',self._watch_page)
        for page in self.context.pages:self._watch_page(page)
        state_file=self.config.get('encrypted_state_file')
        if state_file and state_file.exists():
            from .account_login import party_domain
            key=self._profile_key
            if not key:raise ValueError('A per-account profile key is required for encrypted browser state.')
            state=json.loads(Fernet(key.encode()).decrypt(state_file.read_bytes()))
            await self.context.add_cookies([c for c in state.get('cookies',[]) if party_domain(c.get('domain',''))])
        if seed:await self.seed_local_session()
        self.entry=self.context.pages[0] if self.context.pages else await self.context.new_page()
        for restored in list(self.context.pages):
            if restored is not self.entry:
                await asyncio.wait_for(restored.close(),5)
                if callable(getattr(restored,'is_closed',None)) and restored.is_closed() is not True:
                    raise RuntimeError('A restored browser page did not confirm closure; preserve recovery evidence.')
        await self.entry.goto(PAGE_URL+'?type=video',wait_until='domcontentloaded',timeout=60000)
        # Only journals predating this fresh launch under its exact exclusive
        # profile guard can receive the old-page closure evidence.
        if owned_guard:self._recovery_window_jobs=startup_jobs
        # A failed restore must never replace the snapshot that caused it to fail.
        self._snapshot_write_allowed=True

    async def noop_telemetry(self,*_):pass

    async def probe(self,*,advertise=True):
        driver=NativeUI(self.entry,self.noop_telemetry,diagnostics_dir=self.state_dir/'diagnostics')
        state=await driver.wait_ready()
        self._entry_readiness={'ready':state.get('ready') is True,'checked_at':time.time()}
        if advertise:await self.heartbeat(state['ready'],state['reason'])
        if state['ready']:await driver.diagnostic('readiness')
        self.status('ready' if state['ready'] else 'login_required' if 'Login required' in state['reason'] else 'verification_required' if 'verification' in state['reason'] else 'page_unavailable',reason=state['reason'])
        return state

    async def files(self,job):
        out=[]
        for item in job['request'].get('references',[]):
            response=await self.api(f"/jobs/{job['id']}/assets/{item['asset_id']}",method='GET',headers={
                'X-Chrome-Claim-Token':job['claim_token'],'X-Chrome-Worker-Id':self.worker_id})
            raw=response.content
            if len(raw)!=item['size_bytes'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Reference integrity mismatch.')
            ext={'image/png':'.png','image/jpeg':'.jpeg','image/webp':'.webp','video/mp4':'.mp4'}.get(item['mime_type'])
            if not ext or not re.fullmatch(r'asset_\d{1,2}_[a-f0-9]{24}',item['asset_id']):raise ValueError('Invalid reference metadata.')
            path=self.state_dir/'assets'/job['id']/(item['asset_id']+ext);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);out.append(path)
        return out

    def _cleanup_job_assets(self,job_id):
        """Remove only this slot's known flat upload copies after durable finish."""
        try:
            if str(uuid.UUID(job_id))!=job_id:return
            current=json.loads(self.journal.read_text(encoding='utf-8'))
            if current.get('phase')!='finished' or current.get('job_id')!=job_id:return
            root=self.state_dir.absolute();directory=root/'assets'/job_id
            if not directory.resolve().is_relative_to(root.resolve()):return
            for path in (root,root/'assets',directory):
                if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):return
            if not directory.is_dir():return
            for index,path in enumerate(directory.iterdir()):
                if index>=100:break
                if not re.fullmatch(r'asset_\d{1,2}_[a-f0-9]{24}\.(?:png|jpeg|webp|mp4)',path.name):continue
                info=path.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and not path.is_symlink():path.unlink()
            try:directory.rmdir()
            except OSError:pass  # Unknown files are never a reason for recursive deletion.
        except (OSError,ValueError,TypeError,AttributeError):pass

    def _mark_finished(self,job_id,**fields):
        write_json(self.journal,{'phase':'finished','job_id':job_id,**fields})
        self._cleanup_job_assets(job_id)

    async def recover(self):
        if not self.journal.exists():return True
        state=json.loads(self.journal.read_text(encoding='utf-8'))
        if state.get('phase') in ('idle','finished'):
            if state.get('phase')=='finished':self._cleanup_job_assets(state.get('job_id'))
            return True
        job=state.get('job')
        if not job:
            if state.get('phase')=='claiming':
                nonce=state.get('claim_request_id')
                try:uuid.UUID(nonce)
                except (ValueError,TypeError,AttributeError):
                    self.status('attention_required',reason='Legacy lost claim has no fenced request ID; manual reconciliation is required.');return False
                response=await self.api('/slots/recover',{'account_id':self.config['account'],'worker_id':self.worker_id,'claim_request_id':nonce})
                recovered=response.get('job')
                if recovered is not None:
                    if not isinstance(recovered,dict) or recovered.get('account_id')!=self.config['account'] or recovered.get('worker_id')!=self.worker_id or recovered.get('claim_request_id')!=nonce or recovered.get('status')!='claimed' or not isinstance(recovered.get('claim_token'),str) or not recovered['claim_token']:
                        raise RuntimeError('Recovered claim ownership is invalid; no generation will be activated.')
                    try:uuid.UUID(recovered['id'])
                    except (ValueError,KeyError,TypeError,AttributeError):raise RuntimeError('Recovered claim identity is invalid.') from None
                    write_json(self.journal,{'phase':'preparing','job':recovered,'recovered_claim':True})
                    self.recovered_claim=recovered;return True
                if response.get('slot_released') is True:
                    write_json(self.journal,{'phase':'idle','recovered_empty_claim':True});return True
            self.status('attention_required',reason='A prior claim outcome is unknown. Inspect this worker on the server before resuming.');return False
        remote=await self.api(f"/jobs/{job['id']}/status",{'worker_id':self.worker_id,'claim_token':job['claim_token']})
        if remote['status'] in ('completed','failed'):
            self._mark_finished(job['id']);return True
        if ((job.get('claim_protocol_version') or 0)>=6 and remote.get('slot_released') is True
                and remote.get('submission_window_closed_at') is not None and remote.get('status')=='uncertain'):
            self._archive_detached(job,dict(state.get('confirmation') or {},status='uncertain',submit_id=remote.get('submit_id')))
            return True
        if state.get('phase')=='request_observed' and state.get('confirmation',{}).get('submit_id'):
            result=self._closed_recovery_receipt(job,state['confirmation']|{'status':'uncertain','error':'Worker restarted after observing the request; querying the original submit ID.'})
            await self.report(job,result);self.resume_job=(job,result);return True
        if state.get('phase') in ('reporting','reported') and state.get('result'):
            confirmation=self._closed_recovery_receipt(job,self.normalized_outcome(state.get('confirmation') or state['result']))
            acknowledged=await self.report(job,confirmation)
            if confirmation.get('submission_window_closed') is True and acknowledged.get('slot_released') is True:
                self._archive_detached(job,confirmation);return True
            if confirmation['status']=='failed':self._mark_finished(job['id']);return True
            if confirmation['status'] in ('submitted','uncertain') and confirmation.get('submit_id'):
                self.resume_job=(job,confirmation);return True
        elif state.get('phase')=='preparing':
            await self.report(job,{'status':'failed','error':'Worker restarted before the durable click intent; no generation was submitted.','generation_not_activated':True})
            self._mark_finished(job['id']);return True
        elif state.get('phase')=='submission_ambiguous':
            confirmation=dict(state.get('confirmation') or {});confirmation.pop('submit_id',None);confirmation.pop('reference_bindings',None)
            receipt=self._closed_recovery_receipt(job,confirmation|{'status':'uncertain','error':'Multiple submission IDs were observed; operator reconciliation is required. No automatic resubmission.'})
            acknowledged=await self.report(job,receipt)
            if receipt.get('submission_window_closed') is True and acknowledged.get('slot_released') is True:self._archive_detached(job,receipt);return True
        else:
            receipt=self._closed_recovery_receipt(job,{'status':'uncertain','error':'Worker stopped after click intent. Reconcile the original task; it will not be resubmitted.'})
            acknowledged=await self.report(job,receipt)
            if receipt.get('submission_window_closed') is True and acknowledged.get('slot_released') is True:self._archive_detached(job,receipt);return True
        self.status('attention_required',job_id=job['id'],reason='Original task requires reconciliation; no automatic second click.');return False

    @staticmethod
    def preparation_retryable(error,driver,intent_written):
        if intent_written or getattr(driver,'clicked',False) or getattr(driver,'request_observed',None):return False
        if is_browser_proxy_error(error) or getattr(error,'error_code',None) in ('proxy_error','reference_count_mismatch'):return False
        if isinstance(error,UIUnavailable):
            return not re.search(r'login|human|verification|captcha|upgrade|subscription|unsupported|rejected|integrity|original page language|claim is not active|credit|quote|balance',str(error),re.I)
        return isinstance(error,(BrowserTimeoutError,httpx.TransportError)) or isinstance(error,RuntimeError) and bool(re.search(r'Worker API HTTP (408|429|500|502|503|504);',str(error)))

    async def _close_task_page(self,page):
        if page:
            try:await asyncio.wait_for(page.close(),5)
            except Exception:pass
        if page is self._active_page:self._active_page=None
        if page is self._retained_page:
            self._retained_page=None;self._retained_bootstrap=set();self._retained_kind=None;self._retained_uses=0;self._retained_parked=False

    async def _release_task_page(self,job,page,driver=None):
        """Keep only a centrally resolved page, owned exclusively by this slot."""
        reusable=(job.get('claim_protocol_version') or 0)>=6 and page is not None
        if reusable:
            self._retained_page=page;self._active_page=None
            self._retained_uses+=1
            self._retained_bootstrap=set(getattr(driver,'bootstrap',set()))
            self._retained_kind=job['request'].get('kind','video')
            self._retained_parked=False
        else:await self._close_task_page(page)

    async def _quiesce_unknown_page(self,job,result,page,driver=None):
        """Close this task's UI before releasing only its pacing lease.

        This proves no further actions can leave this page. It does not prove
        the upstream task failed, permit resubmission, or release credit/slots.
        """
        if (job.get('claim_protocol_version') or 0)<6 or result.get('status')!='uncertain':return result
        if page is None or page is self.entry:
            self._browser_problem('unknown_submission_page_not_isolated');return result
        if driver:
            try:await asyncio.wait_for(driver.diagnostic('uncertain'),1)
            except Exception:pass
        try:
            await asyncio.wait_for(page.close(),5)
            if page.is_closed() is not True:
                self._browser_problem('unknown_submission_page_close_failed');return result
        except Exception:
            self._browser_problem('unknown_submission_page_close_failed');return result
        if page is self._active_page:self._active_page=None
        if page is self._retained_page:
            self._retained_page=None;self._retained_bootstrap=set();self._retained_kind=None;self._retained_uses=0
        return dict(result,submission_window_closed=True)

    @staticmethod
    async def _finish_shielded(task):
        # A caller may request shutdown repeatedly. The task itself has bounded
        # preparation/submission/HTTP waits and keeps recording the same intent.
        while not task.done():
            try:await asyncio.shield(task)
            except asyncio.CancelledError:continue
        return task.result()

    def _archive_detached(self,job,result):
        # The original paid task remains centrally queryable. This file is not a
        # new dispatch candidate, and never loses its ownership/receipt evidence.
        write_json(self.state_dir/'history'/(job['id']+'.json'),
                   {'phase':'detached','job':job,'confirmation':result,'detached_at':time.time()})
        self._mark_finished(job['id'],submit_id=result.get('submit_id'),detached=True)

    async def wait_original(self,job,result,driver=None,page=None,timeout=None):
        """Only read the original task; a timeout never releases it for resubmission."""
        timeout=self.config.get('generation_timeout_seconds',600) if timeout is None else timeout
        last=None;failures=0;deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if getattr(self,'_pool_owner',self)._browser_failed:
                self.status('attention_required',job_id=job['id'],submit_id=result.get('submit_id'),
                            reason='Owned browser is unavailable; the original central task and journal remain intact for restart.')
                return False
            try:
                remote=await asyncio.wait_for(self.api(f"/jobs/{job['id']}/status",{'worker_id':self.worker_id,'claim_token':job['claim_token']}),max(.001,deadline-time.monotonic()))
            except TimeoutError:break
            except (httpx.TransportError,RuntimeError) as error:
                message=str(error)
                if isinstance(error,RuntimeError) and not re.search(r'HTTP (408|429|500|502|503|504)',message):
                    self.status('attention_required',job_id=job['id'],reason='Original task polling needs worker connectivity or authorization. No new submission.');return False
                failures+=1;self.status('reconnecting',job_id=job['id'],reason='Query connection interrupted; preserving the original task.')
                await asyncio.sleep(min(2**min(failures,5),30,max(0,deadline-time.monotonic())));continue
            failures=0
            if remote['status'] in ('completed','failed'):
                if (job.get('claim_protocol_version') or 0)>=6 and remote.get('submit_id')!=result.get('submit_id'):
                    self.status('attention_required',job_id=job['id'],reason='Terminal receipt identity did not match this slot; preserving the original task.')
                    return False
                self.status(remote['status'],job_id=job['id'],submit_id=result.get('submit_id'))
                self._mark_finished(job['id'],submit_id=result.get('submit_id'))
                if driver:
                    try:await driver.diagnostic('final')
                    except Exception:pass
                await self._release_task_page(job,page,driver)
                return True
            if remote.get('slot_released') is True and result.get('submit_id') and remote.get('submit_id')==result['submit_id']:
                self._archive_detached(job,result);await self._release_task_page(job,page,driver)
                self.status('detached',job_id=job['id'],submit_id=result['submit_id'],reason='Central service continues the original task; browser slot released.')
                return True
            if driver:
                try:
                    progress=await driver.progress(result['submit_id'])
                    if progress is not None and progress!=last:
                        await driver.stage('generating',progress=progress,progress_source='page');last=progress
                except Exception:pass  # Losing the page must not lose the upstream task.
            await asyncio.sleep(min(3,max(0,deadline-time.monotonic())))
        timeout_result={**result,'status':'uncertain','error_code':'generation_timeout',
                        'error':'Generation did not finish within the browser wait deadline. Query the original task; do not regenerate.'}
        await self.report(job,timeout_result)
        remote=await self.api(f"/jobs/{job['id']}/status",{'worker_id':self.worker_id,'claim_token':job['claim_token']})
        terminal=remote.get('status') in ('completed','failed') and ((job.get('claim_protocol_version') or 0)<6 or remote.get('submit_id')==result.get('submit_id'))
        if terminal or (remote.get('slot_released') is True and remote.get('submit_id')==result.get('submit_id') and result.get('submit_id')):
            self._archive_detached(job,timeout_result);await self._release_task_page(job,page,driver)
            self.status('detached',job_id=job['id'],submit_id=result.get('submit_id'),reason='Wait deadline returned to caller; original task remains tracked centrally.')
            return True
        self.status('attention_required',job_id=job['id'],reason='Original generation remains pending. Resume its submit ID, never recreate it.')
        return False

    async def handle(self,job):
        self.active=job;attempts=self.config.get('prepare_attempts',2)
        owner=getattr(self,'_pool_owner',self);owner._last_work_at=time.monotonic()
        # Lock queueing is bounded by the central queue/total limits. The UI
        # preparation budget starts only after this account's focus lock is held.
        deadline=None;result=None;driver=None;page=None;cancel_requested=False;native_activation_excluded=False
        for attempt in range(1,attempts+1):
            write_json(self.journal,{'phase':'preparing','job':job,'prepare_attempt':attempt})
            page=None;driver=None;preparation_timer=None
            intent_written=False;activation_requested=False
            async def before_click():
                nonlocal intent_written,activation_requested
                # The separate bounded response window must not be cancelled by
                # the preparation timer after the central activation request.
                if preparation_timer is not None:preparation_timer.reschedule(None)
                write_json(self.journal,{'phase':'activation_requested','job':job,'prepare_attempt':attempt})
                activation_requested=True
                remote=await self.api(f"/jobs/{job['id']}/activate",{'worker_id':self.worker_id,'claim_token':job['claim_token']})
                if remote.get('can_activate') is not True:
                    if remote.get('generation_not_activated') is True and remote.get('job_id')==job['id']:
                        activation_requested=False
                        write_json(self.journal,{'phase':'preparing','job':job,'prepare_attempt':attempt})
                        error=UIUnavailable('Generation activation rejected: '+str(sanitize(remote.get('message','No generation submitted.')))[:300])
                        code=remote.get('error_code')
                        error.error_code=code if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',code) else 'activation_denied'
                        raise error
                    raise UIUnavailable('Activation response is unconfirmed; reconcile the original task before another attempt.')
                write_json(self.journal,{'phase':'click_intent','job':job,'prepare_attempt':attempt})
                intent_written=True
            async def wait_submission_turn():
                if (job.get('claim_protocol_version') or 0)<6:return
                while True:
                    turn=await self.api(f"/jobs/{job['id']}/submission-turn",{'worker_id':self.worker_id,'claim_token':job['claim_token']})
                    if turn.get('ready') is True:return
                    delay=turn.get('wait_seconds',1)
                    if type(delay) not in (int,float) or not math.isfinite(delay) or delay<0:raise UIUnavailable('Submission pacing response is invalid; no generation submitted.')
                    await asyncio.sleep(max(.1,min(delay,5)))
            async def credit_preflight(quote):
                result=await self.api(f"/jobs/{job['id']}/preflight",{'worker_id':self.worker_id,'claim_token':job['claim_token'],
                    'generation_not_activated':True,**quote})
                if result.get('allowed') is not True:
                    reason=result.get('reason','insufficient_credits')
                    messages={'below_credit_threshold':'Account credit balance is below its scheduling pause threshold; no generation submitted.',
                              'credits_reserved':'Other in-flight tasks already reserve these credits; no generation submitted.'}
                    error=UIUnavailable(messages.get(reason,'Insufficient credits confirmed before generation. The server will select another eligible account.'))
                    error.error_code=reason if reason in ('below_credit_threshold','credits_reserved') else 'insufficient_credits'
                    raise error
            def observed(receipt):
                phase='submission_ambiguous' if len(receipt.get('observed_submit_ids',[]))>1 else 'request_observed'
                write_json(self.journal,{'phase':phase,'job':job,'confirmation':receipt,'prepare_attempt':attempt})
            async def prepare_and_submit():
                nonlocal page,driver,deadline,preparation_timer
                # Asset HTTP reads do not steal browser focus and may overlap
                # another slot's UI preparation, but still have a finite budget.
                files=await asyncio.wait_for(self.files(job),self.config.get('preparation_timeout_seconds',180))
                async with self._preparation_lock:
                    if deadline is None:
                        if (job.get('claim_protocol_version') or 0)>=6:
                            ready=await self.api(f"/jobs/{job['id']}/prepare",{'worker_id':self.worker_id,'claim_token':job['claim_token']})
                            if ready.get('can_prepare') is not True:raise UIUnavailable('Claim is not active for preparation; no generation submitted.')
                        deadline=time.monotonic()+self.config.get('preparation_timeout_seconds',180)
                    async with asyncio.timeout(max(.001,deadline-time.monotonic())) as preparation_timer:
                        if owner._entry_parked:
                            waking=await owner._wake_entry_locked(owner._pool_control)
                            if not waking.get('ready'):
                                if waking.get('error_code')=='proxy_error':raise BrowserProxyUnavailable(waking['reason'])
                                raise UIUnavailable(waking.get('reason','The account entry could not be verified after waking.'))
                            await owner._announce_entry_check()
                        page=self._retained_page
                        kind=job['request'].get('kind','video')
                        if page is not None and (page.is_closed() or kind!=self._retained_kind or self._retained_uses>=self.config.get('page_reload_jobs',20)):
                            await self._close_task_page(page);page=None
                        reused=page is not None
                        if not reused:page=await self.context.new_page()
                        self._active_page=page;await page.bring_to_front()
                        driver=NativeUI(page,lambda values:self.telemetry(job,values),diagnostics_dir=self.state_dir/'diagnostics'/job['id']/f'attempt-{attempt}',require_bootstrap=True,
                                        submission_timeout_ms=self.config.get('submission_timeout_seconds',60)*1000)
                        parked_page=reused and self._retained_parked
                        if reused and not parked_page:driver.bootstrap.update(self._retained_bootstrap)
                        driver.before_activation_turn=wait_submission_turn
                        if not reused or parked_page:
                            await page.goto(PAGE_URL+'?type='+('image' if kind=='image' else 'video'),wait_until='domcontentloaded',timeout=60000)
                            self._retained_parked=False
                        ready=await driver.wait_ready()
                        if not ready['ready']:raise UIUnavailable(ready['reason'])
                        return await driver.submit(job['request'],files,before_click,on_request_observed=observed,on_preflight=credit_preflight)
            operation=asyncio.create_task(prepare_and_submit())
            try:
                try:result=await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancel_requested=True
                    if not (activation_requested or intent_written or getattr(driver,'clicked',False) or getattr(driver,'request_observed',None)):
                        operation.cancel();await asyncio.gather(operation,return_exceptions=True)
                        raise
                    result=await self._finish_shielded(operation)
                break
            except asyncio.CancelledError:
                if activation_requested or intent_written or getattr(driver,'clicked',False) or getattr(driver,'request_observed',None):
                    # The shielded coroutine itself may be cancelled by runtime
                    # shutdown. That cannot turn an activated task into a safe
                    # pre-submit failure, even when its request ID is missing.
                    cancel_requested=True
                    result=getattr(driver,'submission_issue',None) or getattr(driver,'response_observed',None)
                    result=dict(result) if result else {**(getattr(driver,'request_observed',None) or {}),
                        'status':'uncertain','error_code':'submission_unconfirmed',
                        'error':'Browser response capture was interrupted after activation; reconcile the original task.'}
                    break
                # No activation happened, so cancellation may safely stop the UI.
                await self._close_task_page(page)
                reporting=asyncio.create_task(self.report(job,{'status':'failed','generation_not_activated':True,
                    'error_code':'preparation_cancelled','error':'Worker stopped before generation activation; no generation submitted.'}))
                await self._finish_shielded(reporting)
                self._mark_finished(job['id'])
                self.active=None
                raise
            except Exception as error:
                if is_browser_proxy_error(error):error=BrowserProxyUnavailable('The configured browser proxy could not connect; no direct fallback was used.')
                native_activation_excluded=(isinstance(error,ReferenceCountMismatch) and getattr(error,'native_activation_not_attempted',False) is True
                    and not getattr(driver,'clicked',False) and not getattr(driver,'request_observed',None))
                if driver:
                    try:await asyncio.wait_for(driver.diagnostic('failure'),5)
                    except Exception:pass
                if not cancel_requested and attempt<attempts and deadline is not None and time.monotonic()<deadline and self.preparation_retryable(error,driver,intent_written or activation_requested):
                    try:await self.telemetry(job,{'stage':'preparation_retry','observed_at':time.time(),'detail':{'attempt':attempt,'next_attempt':attempt+1,'reason':type(error).__name__,'generation_not_activated':True}})
                    except Exception:pass
                    await self._close_task_page(page)
                    await asyncio.sleep(min(attempt*2,4));continue
                message=error.safe_message if isinstance(error,WorkerAPIError) else str(error)[:500] if isinstance(error,(UIUnavailable,ValueError)) else type(error).__name__+' during native UI execution.'
                issue=getattr(driver,'submission_issue',None)
                result=dict(issue) if issue else {**(getattr(driver,'request_observed',None) or {}),'status':'uncertain' if getattr(driver,'clicked',False) or intent_written or activation_requested else 'failed','error':message}
                if isinstance(error,TimeoutError):
                    result['error_code']='submission_unconfirmed' if getattr(driver,'clicked',False) or intent_written or activation_requested else 'preparation_timeout'
                    result['error']='Preparation deadline exceeded before generation.' if result['status']=='failed' else 'Browser execution timed out after activation; reconcile the original submission.'
                elif getattr(error,'error_code',None):result['error_code']=error.error_code
                elif isinstance(error,httpx.TransportError):result['error_code']='worker_connection_lost'
                break
            finally:
                if driver and hasattr(driver,'dispose'):
                    try:await driver.dispose()
                    except Exception:pass
        result['generation_not_activated']=not (activation_requested or intent_written or getattr(driver,'clicked',False) or result.get('submit_id'))
        result=self.normalized_outcome(result)
        closing=asyncio.create_task(self._quiesce_unknown_page(job,result,page,driver))
        try:result=await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancel_requested=True;result=await self._finish_shielded(closing)
        if native_activation_excluded and result.get('submission_window_closed') is True and not result.get('submit_id'):
            # A central activation permit is not a physical Generate action.
            # Only this specific pre-keypress guard plus confirmed page close
            # proves the native action never happened. Unknown actions stay held.
            result=dict(result,status='failed',generation_not_activated=True)
            result.pop('submission_window_closed',None)
        reporting=asyncio.create_task(self.report(job,result))
        try:acknowledged=await asyncio.shield(reporting)
        except asyncio.CancelledError:
            cancel_requested=True;acknowledged=await self._finish_shielded(reporting)
        if cancel_requested:
            self.active=None
            raise asyncio.CancelledError()
        if result['status']=='uncertain' and driver and not result.get('submission_window_closed'):
            try:await asyncio.wait_for(driver.diagnostic('uncertain'),1)
            except Exception:pass
        self.status(result['status'],job_id=job['id'],submit_id=result.get('submit_id'),error=result.get('error'))
        resolved=True
        if result.get('submit_id') and result['status'] in ('submitted','uncertain'):
            if isinstance(acknowledged,dict) and acknowledged.get('slot_released') is True and acknowledged.get('submit_id')==result['submit_id']:
                self._archive_detached(job,result);await self._release_task_page(job,page,driver)
            else:resolved=await self.wait_original(job,result,driver,page)
        elif result['status']=='failed':
            self._mark_finished(job['id'])
            await self._close_task_page(page)
        else:resolved=False
        self.active=None
        owner._last_work_at=time.monotonic()
        return resolved

    async def heartbeat_loop(self):
        while True:
            try:
                if self.active:await self.heartbeat(False,'Native worker is processing one assigned task.')
            except Exception:pass
            await asyncio.sleep(15)

    @staticmethod
    def _pending_journal(path):
        if not path.exists():return None
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError('Worker journal is not an object.')
        return value if value.get('phase') not in ('idle','finished','migrated') else None

    def prepare_slots(self):
        """Migrate one legacy journal once, retaining the original claim owner."""
        count=self.config.get('concurrency',1)
        if type(count) is not int or not 1<=count<=8:raise ValueError('Invalid configured browser slot count.')
        first=self.state_dir/'slots'/'1';first.mkdir(parents=True,exist_ok=True)
        identity=first/'identity.json'
        if identity.exists():
            if json.loads(identity.read_text()).get('worker_id')!=self.worker_id:
                raise RuntimeError('Legacy and first-slot worker identities differ; review the saved journals.')
        else:write_json(identity,{'worker_id':self.worker_id})
        legacy=self._pending_journal(self.journal)
        if legacy:
            destination=first/'current.json'
            if destination.exists():
                prior=json.loads(destination.read_text())
                prior_id=(prior.get('job') or {}).get('id') or prior.get('job_id')
                old_id=(legacy.get('job') or {}).get('id') or legacy.get('job_id')
                if not old_id or prior_id!=old_id:
                    raise RuntimeError('Conflicting legacy and slot journals require review; no task was reclaimed.')
            else:write_json(destination,legacy)
            write_json(self.journal,{'phase':'migrated','slot_index':1,'job_id':(legacy.get('job') or {}).get('id'),'migrated_at':time.time()})
        indices=set(range(1,count+1))
        directory=self.state_dir/'slots'
        for child in directory.iterdir():
            if child.is_dir() and re.fullmatch(r'[1-8]',child.name) and self._pending_journal(child/'current.json'):
                indices.add(int(child.name))  # Retired slots recover old work, but cannot claim new jobs.
        slots=[]
        for index in sorted(indices):
            slot_directory=directory/str(index);retired=slot_directory/'retired.json'
            if index>1 and index<=count and retired.exists() and not self._pending_journal(slot_directory/'current.json'):
                value=json.loads(retired.read_text(encoding='utf-8'))
                prior=json.loads((slot_directory/'identity.json').read_text(encoding='utf-8'))
                if value.get('worker_id')==prior.get('worker_id'):
                    write_json(slot_directory/'identity.json',{'worker_id':'native-'+uuid.uuid4().hex})
                    retired.unlink()
            config=dict(self.config,state_dir=directory/str(index),slot_index=index,owner_id=self.worker_id,
                        owner_state_dir=self.state_dir,concurrency=count)
            slot=Worker(config);slot.context=self.context;slot.entry=self.entry;slot._owns_context=False
            slot._preparation_lock=self._preparation_lock
            slot._pool_owner=self;slot._retired=index>count;slots.append(slot)
            slot._recovery_window_jobs=set(self._recovery_window_jobs)
        self._slots=slots
        return slots

    def _pool_summary(self,control):
        slots=[]
        for slot in self._slots:
            raw=slot._last_status_data
            entry={'slot_index':slot.config['slot_index'],'worker_state':raw.get('state','starting')}
            for field in ('job_id','submit_id'):
                if isinstance(raw.get(field),str):entry[field]=raw[field]
            slots.append(entry)
        active=sum(slot.active is not None for slot in self._slots)
        attention=sum(slot._last_status_data.get('state') in ('attention_required','worker_error') for slot in self._slots)
        ready=sum(slot._last_status_data.get('state')=='ready' and not slot._retired for slot in self._slots)
        state='stopping' if control['stop'].is_set() else 'browser_waking' if self._entry_waking else 'browser_parked' if self._entry_parked else ('login_required' if 'Login required' in control['readiness'].get('reason','') else
              'verification_required' if 'verification' in control['readiness'].get('reason','').lower() else
              'degraded' if attention else 'ready' if ready else 'processing' if active else 'page_unavailable')
        self.status(state,effective_concurrency=len(self._slots),target_concurrency=self.config.get('concurrency',1),
                    retiring_slots=sum(slot._retired for slot in self._slots),active_slots=active,
                    attention_slots=attention,ready_slots=ready,slots=slots,
                    entry_parked=self._entry_parked,entry_checked_at=self._entry_readiness.get('checked_at'),parking_failures=self._park_failures,
                    reason='Independent browser task slots; one failure does not stop the other slots.')

    async def _pool_monitor(self,control):
        unavailable=0
        while True:
            if (self.state_dir/'stop.request').exists():control['stop'].set()
            self._read_slot_retirement()
            if self._browser_failed and not control['stop'].is_set():
                control['browser_failure']=self._browser_failure_reason or 'browser_unavailable';control['stop'].set()
            if not control['stop'].is_set() and not self._preparation_lock.locked():
                checked=False
                try:
                    async with self._preparation_lock:
                        if self._entry_parked:
                            if self._entry_wake_requested or time.time()-self._entry_readiness.get('checked_at',0)>=self._park_probe_seconds:
                                await self._wake_entry_locked(control);checked=True
                        else:
                            interval=self.config.get('entry_refresh_seconds',300)
                            idle=not any(slot.active or getattr(slot,'_claim_reserved',False) for slot in self._slots)
                            if idle and time.monotonic()-self._entry_checked_at>=interval:
                                self._entry_checked_at=time.monotonic()
                                await asyncio.wait_for(self.entry.reload(wait_until='domcontentloaded',timeout=20000),25)
                            control['readiness']=await asyncio.wait_for(NativeUI(self.entry,self.noop_telemetry).check(),5)
                            self._entry_readiness={'ready':control['readiness'].get('ready') is True,'checked_at':time.time()};checked=True
                except Exception:
                    control['readiness']={'ready':False,'reason':'Account entry page is unavailable.'}
                    self._entry_readiness={'ready':False,'checked_at':time.time()};checked=True
                if checked:
                    reason=control['readiness'].get('reason','').lower()
                    if self._entry_readiness['ready']:unavailable=0
                    elif not any(word in reason for word in ('login required','verification','captcha')):
                        unavailable+=1
                        if unavailable>=3:self._browser_problem('browser_readiness_failed')
                    else:unavailable=0
            # Lock order is claim -> preparation. Claimed work becomes active
            # before releasing claim_lock, so a navigation cannot race a claim.
            if not control['stop'].is_set() and not self._entry_parked and control.get('claim_lock') is not None and not control['claim_lock'].locked() and not self._preparation_lock.locked() and self._idle_for_parking():
                async with control['claim_lock']:
                    async with self._preparation_lock:await self._park_idle_pages_locked(control)
            self._pool_summary(control)
            await asyncio.sleep(2)

    @staticmethod
    def recovery_delay(attempt):
        return min(60,2**min(max(attempt,1),6)+secrets.randbelow(1001)/1000)

    async def _recovery_sleep(self,control,attempt):
        # Sleep in short pieces so an operator's normal stop is not held behind
        # a 60-second retry delay. No new claim is made by this recovery loop.
        deadline=time.monotonic()+self.recovery_delay(attempt)
        while not control['stop'].is_set() and time.monotonic()<deadline:
            await asyncio.sleep(min(1,max(0,deadline-time.monotonic())))

    async def _await_slot_recovery(self,slot,control):
        """Keep the original receipt isolated; never turn a lost claim into retry."""
        attempt=0;previous=None
        while not control['stop'].is_set():
            try:
                await slot.heartbeat(False,'This slot is reconciling its original task.')
                slot.resume_job=None
                recovered=await slot.recover()
                if recovered:
                    resume=slot.recovered_claim or (slot.resume_job[0] if slot.resume_job else None)
                    if slot._claim_reserved or resume is not None:
                        async with control['claim_lock']:
                            if slot._claim_reserved:
                                control['pending_claims']-=1;slot._claim_reserved=False
                            if resume is not None and resume['id'] not in control['counted_job_ids']:
                                control['counted_job_ids'].add(resume['id']);control['claimed']+=1
                    if slot.recovered_claim is not None:
                        job=slot.recovered_claim;slot.recovered_claim=None;slot.active=job
                        if not await slot.handle(job):
                            slot.active=None;attempt+=1;await self._recovery_sleep(control,attempt);continue
                    if slot.resume_job:
                        job,result=slot.resume_job;slot.active=job
                        if not await slot.wait_original(job,result,page=slot._active_page):
                            slot.active=None;attempt+=1;await self._recovery_sleep(control,attempt);continue
                    slot.active=None;slot.resume_job=None
                    await slot._close_task_page(slot._active_page)
                    return True
            except Exception:
                slot.status('attention_required',reason='Original slot state could not be reconciled. Other slots remain independent.')
            marker=(slot._last_status_data.get('state'),slot._last_status_data.get('submit_id'))
            attempt=attempt+1 if marker==previous else 1;previous=marker
            await self._recovery_sleep(control,attempt)
        return False

    async def _run_slot(self,slot,control,max_jobs=0):
        heartbeat=asyncio.create_task(slot.heartbeat_loop())
        try:
            if not await self._await_slot_recovery(slot,control):return
            while not control['stop'].is_set() and not slot._retired:
                readiness=control['readiness']
                try:
                    advertised=bool(readiness.get('ready') or self._can_wake())
                    lease=await slot.heartbeat(advertised,readiness.get('reason',''))
                    if slot._retired:break
                    can_claim=lease.get('ready') if isinstance(lease,dict) and 'ready' in lease else bool(readiness.get('ready')) and not self._entry_parked
                    if not can_claim:
                        slot.status('page_unavailable',reason=readiness.get('reason',''));await asyncio.sleep(2);continue
                    async with control['claim_lock']:
                        if control['stop'].is_set() or slot._retired or max_jobs and control['claimed']+control['pending_claims']>=max_jobs:return
                        nonce=str(uuid.uuid4())
                        write_json(slot.journal,{'phase':'claiming','started_at':time.time(),'claim_request_id':nonce})
                        control['pending_claims']+=1;slot._claim_reserved=True
                        try:response=await slot.api('/claim',{'account_id':slot.config['account'],'worker_id':slot.worker_id,'claim_request_id':nonce})
                        except Exception:
                            slot.status('attention_required',reason='Claim response was lost; this slot will not issue another claim.')
                            response=None
                        if response is not None:
                            control['pending_claims']-=1;slot._claim_reserved=False
                            job=response.get('job')
                            if job and job['id'] not in control['counted_job_ids']:
                                control['counted_job_ids'].add(job['id']);control['claimed']+=1
                            if job:
                                slot.active=job;self._last_work_at=time.monotonic()
                    if response is None:
                        if not await self._await_slot_recovery(slot,control):return
                        continue
                    if not job:
                        write_json(slot.journal,{'phase':'idle'});slot.status('ready');await asyncio.sleep(2);continue
                    if job.get('account_id')!=slot.config['account']:
                        slot.status('attention_required',reason='Claim account mismatch; no generation will be activated.')
                        if not await self._await_slot_recovery(slot,control):return
                        continue
                    slot.active=job;await slot.heartbeat(False,'This browser slot is processing its assigned task.')
                    if not await slot.handle(job):
                        slot.status('attention_required',job_id=job['id'],reason='This slot retains its original uncertain task.')
                        if not await self._await_slot_recovery(slot,control):return
                except asyncio.CancelledError:raise
                except Exception:
                    slot.status('attention_required',reason='This slot failed; its journal is preserved for recovery.')
                    if not await self._await_slot_recovery(slot,control):return
        finally:
            heartbeat.cancel();await asyncio.gather(heartbeat,return_exceptions=True)
            try:await asyncio.wait_for(slot.heartbeat(False,'Browser slot stopped.'),3)
            except Exception:pass
            if slot._retired:
                attempt=0
                while True:
                    try:
                        if await self._finish_retired_slot(slot):break
                        slot.status('attention_required',reason='Retired slot retains unresolved work or an unclosed page; original receipts preserved.')
                    except Exception:slot.status('attention_required',reason='Retired slot cleanup is pending; original receipts preserved.')
                    if control['stop'].is_set():break
                    attempt+=1;await self._recovery_sleep(control,attempt)

    async def run_pool(self,*,max_jobs=0,readiness=None):
        self.prepare_slots()
        counted=set();pending=0
        for slot in self._slots:
            state=self._pending_journal(slot.journal)
            if state and isinstance(state.get('job'),dict) and state['job'].get('id'):counted.add(state['job']['id'])
            elif state and state.get('phase')=='claiming':slot._claim_reserved=True;pending+=1
        control={'stop':asyncio.Event(),'claim_lock':asyncio.Lock(),'claimed':len(counted),'counted_job_ids':counted,'pending_claims':pending,
                 'readiness':readiness or {'ready':True,'reason':'Ready'}}
        self._pool_control=control
        if readiness:self._entry_readiness={'ready':readiness.get('ready') is True,'checked_at':time.time()}
        monitor=asyncio.create_task(self._pool_monitor(control))
        tasks=[asyncio.create_task(self._run_slot(slot,control,max_jobs)) for slot in self._slots]
        try:await asyncio.gather(*tasks)
        finally:
            control['stop'].set();monitor.cancel()
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(monitor,*tasks,return_exceptions=True)
            await asyncio.gather(*(slot.close() for slot in self._slots),return_exceptions=True)
            self._pool_summary(control)
            self._pool_control=None
        if control.get('browser_failure'):raise RuntimeError('Owned browser became unavailable; preserve journals and restart this worker.')

    async def run(self,pw,mode='run',seed=False,max_jobs=0):
        await self.open(pw,seed)
        if mode=='login':
            self.status('login_window_open',reason='Complete normal login in this dedicated Chrome window. No credentials are entered by the worker.')
            while not (await self.probe(advertise=False))['ready']:await asyncio.sleep(5)
            self.status('login_saved',profile=str(self.config['profile_dir']));return
        ready=await self.probe(advertise=False)
        if self._browser_failed:raise RuntimeError('Owned browser closed during readiness checks; preserve recovery journals.')
        if mode=='run' and not ready['ready'] and not re.search(r'login required|verification|captcha',ready.get('reason',''),re.I):
            raise RuntimeError('Owned browser did not become ready; restart this worker with its original journals.')
        if not ready['ready'] or mode=='probe':return
        await self.run_pool(max_jobs=max_jobs,readiness=ready)

    async def close(self):
        self._closing_context=True
        errors=[]
        try:
            try:await asyncio.wait_for(self.heartbeat(False,'Worker stopped.'),3)
            except Exception:pass
            if self.context and self._owns_context:
                state_file=self.config.get('encrypted_state_file');key=self._profile_key
                if state_file and key and self._snapshot_write_allowed:
                    try:
                        from .account_login import party_domain
                        cookies=await asyncio.wait_for(self.context.cookies(),5)
                        state={'cookies':[c for c in cookies if party_domain(c.get('domain',''))]}
                        encoded=Fernet(key.encode()).encrypt(json.dumps(state,separators=(',',':')).encode())
                        temp=state_file.with_suffix('.tmp');temp.write_bytes(encoded);temp.replace(state_file)
                    except Exception:pass
                self._snapshot_write_allowed=False
                try:await asyncio.wait_for(self.context.close(),10)
                except TimeoutError:errors.append('browser_close_timeout')
                except Exception:errors.append('browser_close_failed')
        finally:
            # A dead browser must not prevent closing this slot's HTTP client.
            # Playwright's owner context manager still tears down its driver;
            # no external or unidentified process is terminated here.
            try:await asyncio.wait_for(self.http.aclose(),5)
            except TimeoutError:errors.append('http_close_timeout')
            except Exception:errors.append('http_close_failed')
            if errors:
                try:
                    write_json(self.state_dir/'shutdown.json',{'state':'shutdown_incomplete','errors':errors,'observed_at':time.time()})
                    self.status('worker_error',reason='Owned worker cleanup did not finish within its deadline; journals were retained.')
                except OSError:pass


async def main_async(args):
    config=safe_config(args.config,args.account)
    async with profile_lock(config['profile_dir']) as guard:
        config['_profile_guard']=guard
        if config.get('clean_cache_on_start'):
            from .maintenance import cleanup_profile_cache
            await asyncio.to_thread(cleanup_profile_cache,config['profile_dir'],root=config['profile_dir'],owned_lock=True)
        worker=Worker(config)
        async with async_playwright() as pw:
            try:await worker.run(pw,args.command,args.import_local_session,args.max_jobs)
            except Exception as error:
                if isinstance(error,BrowserProxyUnavailable) or is_browser_proxy_error(error):
                    # A separate, instance-bound signal survives close/status
                    # updates without persisting URLs or proxy credentials.
                    try:write_json(worker.state_dir/'proxy-failure.json',{
                        'account_id':config['account'],'pid':os.getpid(),'instance_id':config.get('instance_id'),
                        'error_code':'proxy_error','observed_at':time.time()})
                    except OSError:pass
                raise
            finally:await worker.close()


def main():
    parser=argparse.ArgumentParser(description='Extension-free persistent Chrome worker')
    parser.add_argument('command',choices=['login','probe','run'])
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--account',default='default')
    parser.add_argument('--max-jobs',type=int,default=0)
    parser.add_argument('--import-local-session',action='store_true',help='One-time import of the user-provided local .env Cookie; never reads Chrome databases.')
    args=parser.parse_args()
    if args.max_jobs<0:parser.error('--max-jobs must be nonnegative')
    if hasattr(__import__('sys').stdout,'reconfigure'):__import__('sys').stdout.reconfigure(encoding='utf-8')
    try:asyncio.run(main_async(args))
    except KeyboardInterrupt:pass
    except Exception as error:
        print(json.dumps({'state':'worker_error','error_type':type(error).__name__,
                          'message':str(error) if isinstance(error,(ValueError,RuntimeError)) and not isinstance(error,httpx.HTTPError) else 'Worker failed; inspect the private local diagnostics.'}),flush=True)
        raise SystemExit(1)


if __name__=='__main__':main()
