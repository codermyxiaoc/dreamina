"""Bounded local supervision of account-specific native browser workers.

Only this package's worker module is launched. Stop uses a private request file;
neither an unrecognized PID nor a browser process is ever force-terminated.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Callable
import uuid

from .browser_worker import process_alive, safe_config
from .browser_launch import parse_browser_args
from .config import ROOT
from .platform_store import PlatformError
from .process_identity import process_snapshot
from .egress import Egress,direct_egress,parse_proxy_url
from .db_async import db_call

ACCOUNT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
ACTIVE_BRIDGE_STATES = ('queued', 'claimed', 'submitted', 'uncertain')
SYSTEM_ENV = {
    'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
    # Playwright resolves system-wide Windows Chrome installations through these.
    'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
    'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOME', 'USER', 'USERNAME',
    'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'DISPLAY', 'XAUTHORITY',
    'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
    'DBUS_SESSION_BUS_ADDRESS', 'PLAYWRIGHT_BROWSERS_PATH',
    'PLAYWRIGHT_NODEJS_PATH', 'LD_LIBRARY_PATH', 'VIRTUAL_ENV',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
}


def _positive_int(value, name, maximum):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a positive integer.')
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name} must be a positive integer.') from error
    if str(value).strip() != str(result) or not 1 <= result <= maximum:
        raise ValueError(f'{name} must be between 1 and {maximum}.')
    return result


def _boolean(value, name):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', '1', 'yes', 'false', '0', 'no'):
        return value.strip().lower() in ('true', '1', 'yes')
    raise ValueError(f'{name} must be a boolean.')


def _write_private(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, maximum=32_768):
    try:
        if path.stat().st_size > maximum:
            return None
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class WorkerPaths:
    profile: Path
    auth: Path
    directory: Path
    config: Path
    state: Path
    registry: Path
    status: Path
    stop: Path


class ManagedWorkers:
    def __init__(self, store, bridge, config: dict | None = None, root: Path | None = None,
                 *, process_factory: Callable | None = None, alive: Callable | None = None,
                 clock: Callable = time.time, sleep: Callable = asyncio.sleep,process_identity: Callable | None = None,
                 jitter: Callable | None = None):
        self.store, self.bridge = store, bridge
        self.config = dict(config or {})
        self.browser_args = parse_browser_args(self.config.get('LOGIN_BROWSER_ARGS'))
        self.root = Path(root or ROOT / 'outputs' / 'managed-browsers').resolve()
        self.max_workers = _positive_int(self.config.get('MANAGED_MAX_WORKERS', 4), 'MANAGED_MAX_WORKERS', 100)
        self.max_account_slots = _positive_int(self.config.get('NATIVE_MAX_ACCOUNT_SLOTS',4),'NATIVE_MAX_ACCOUNT_SLOTS',8)
        self.stop_timeout = _positive_int(self.config.get('MANAGED_STOP_TIMEOUT', 20), 'MANAGED_STOP_TIMEOUT', 60)
        self.prepare_attempts = _positive_int(self.config.get('NATIVE_PREPARE_ATTEMPTS', 2), 'NATIVE_PREPARE_ATTEMPTS', 3)
        self.preparation_timeout = _positive_int(self.config.get('NATIVE_PREPARATION_TIMEOUT_SECONDS',180),'NATIVE_PREPARATION_TIMEOUT_SECONDS',600)
        self.submission_timeout = _positive_int(self.config.get('NATIVE_SUBMISSION_TIMEOUT_SECONDS',60),'NATIVE_SUBMISSION_TIMEOUT_SECONDS',120)
        self.generation_timeout = _positive_int(self.config.get('NATIVE_GENERATION_TIMEOUT_SECONDS',600),'NATIVE_GENERATION_TIMEOUT_SECONDS',3600)
        self.entry_refresh_seconds=_positive_int(self.config.get('NATIVE_ENTRY_REFRESH_SECONDS',300),'NATIVE_ENTRY_REFRESH_SECONDS',3600)
        if self.entry_refresh_seconds<30:raise ValueError('NATIVE_ENTRY_REFRESH_SECONDS must be at least 30.')
        self.page_reload_jobs=_positive_int(self.config.get('NATIVE_PAGE_RELOAD_JOBS',20),'NATIVE_PAGE_RELOAD_JOBS',100)
        self.restart_limit=_positive_int(self.config.get('MANAGED_RESTART_LIMIT',5),'MANAGED_RESTART_LIMIT',20)
        self.restart_stable_seconds=_positive_int(self.config.get('MANAGED_RESTART_STABLE_SECONDS',120),'MANAGED_RESTART_STABLE_SECONDS',3600)
        self.jitter=jitter or (lambda:secrets.randbelow(1001)/1000)
        self.channel = self.config.get('LOGIN_BROWSER_CHANNEL', 'chrome' if os.name == 'nt' else 'chromium')
        if self.channel not in ('chrome', 'chromium'):
            raise ValueError('LOGIN_BROWSER_CHANNEL must be chrome or chromium.')
        self.headless = _boolean(self.config.get('LOGIN_BROWSER_HEADLESS', True), 'LOGIN_BROWSER_HEADLESS')
        self.process_factory = process_factory or subprocess.Popen
        self.alive = alive or process_alive
        self.process_identity=process_identity or process_snapshot
        self.clock, self.sleep = clock, sleep
        self._processes = {}
        self._lock = asyncio.Lock()
        self._scan_seen = {}

    def _supervision(self,account_id,record=None,*,persist=False):
        path=self.paths(account_id).directory/'supervision.json'
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise PlatformError('Worker supervision path needs inspection.',code='worker_supervision_invalid',status=409)
        if path.exists():
            value=_read_json(path,8192)
            if not isinstance(value,dict) or value.get('account_id')!=account_id or type(value.get('desired_running')) is not bool:
                raise PlatformError('Worker supervision state needs inspection.',code='worker_supervision_invalid',status=409)
            if type(value.get('failures')) is not int or not 0<=value['failures']<=100 or type(value.get('circuit_open')) is not bool:
                raise PlatformError('Worker supervision state needs inspection.',code='worker_supervision_invalid',status=409)
            if 'proxy_failures' in value and (type(value['proxy_failures']) is not int or not 0<=value['proxy_failures']<=100):
                raise PlatformError('Worker proxy retry state needs inspection.',code='worker_supervision_invalid',status=409)
            for field in ('next_restart_at','stable_since','updated_at'):
                number=value.get(field)
                if number is not None and (type(number) not in (int,float) or not math.isfinite(number) or number<0):
                    raise PlatformError('Worker supervision time is invalid.',code='worker_supervision_invalid',status=409)
            return value
        record=record if record is not None else self._record(account_id)
        # A saved account without a launched worker is not an instruction to
        # start it. A graceful legacy stop remains a stop after an upgrade.
        desired=bool(record and record.get('stopped_at') is None and not self.paths(account_id).stop.exists())
        value={'account_id':account_id,'desired_running':desired,'failures':0,'circuit_open':False,
               'next_restart_at':None,'stable_since':None,'last_failed_instance':None,'updated_at':self.clock()}
        if persist:_write_private(path,value)
        return value

    def _save_supervision(self,account_id,value):
        value=dict(value,account_id=account_id,updated_at=self.clock())
        _write_private(self.paths(account_id).directory/'supervision.json',value)
        return value

    @staticmethod
    def _supervision_fields(value):
        return {'desired_running':value['desired_running'],'restart_failures':value['failures'],
                'restart_circuit_open':value['circuit_open'],'next_restart_at':value.get('next_restart_at'),
                'supervision_reason':value.get('reason')}

    def _restart_failure(self,account_id,state,instance):
        if state.get('last_failed_instance')==instance:return state
        failures=state['failures']+1
        delay=min(120,5*2**min(failures-1,5)+max(0,min(1,float(self.jitter())))*3)
        return self._save_supervision(account_id,dict(state,failures=failures,
            circuit_open=failures>=self.restart_limit,next_restart_at=None if failures>=self.restart_limit else self.clock()+delay,
            stable_since=None,last_failed_instance=instance,
            reason='restart_circuit_open' if failures>=self.restart_limit else 'worker_crash_backoff'))

    def _proxy_wait(self,account_id,state,reason):
        """Proxy outages are not browser crashes and never open its circuit."""
        attempts=min(100,max(0,int(state.get('proxy_failures',0)))+1)
        delay=min(300,15*2**min(attempts-1,5))
        return self._save_supervision(account_id,dict(state,circuit_open=False,proxy_failures=attempts,
            next_restart_at=self.clock()+delay,stable_since=None,reason=reason))

    def _proxy_failure_receipt(self,account_id,record):
        if not record:return False
        path=self.paths(account_id).state/'proxy-failure.json'
        if path.is_symlink():return False
        receipt=_read_json(path,4096)
        return bool(receipt and receipt.get('account_id')==account_id and receipt.get('instance_id')==record.get('instance_id')
            and receipt.get('pid')==record['pid'] and receipt.get('error_code')=='proxy_error'
            and type(receipt.get('observed_at')) in (int,float) and receipt['observed_at']>=record['started_at']-1)

    def paths(self, account_id: str) -> WorkerPaths:
        if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
            raise PlatformError('Invalid account ID.', code='invalid_account', status=400)
        directory = self.root / 'workers' / account_id
        state = directory / 'worker-state' / account_id
        values = WorkerPaths(profile=self.root / 'profiles' / account_id,
                             auth=self.root / 'auth' / f'{account_id}.enc',
                             directory=directory, config=directory / 'browser-worker.json',
                             state=state, registry=directory / 'process.json',
                             status=state / 'status.json', stop=state / 'stop.request')
        for path in values.__dict__.values():
            if not path.resolve().is_relative_to(self.root):
                raise PlatformError('Managed worker path escapes its configured root.', code='invalid_worker_path', status=409)
        return values

    def _record(self, account_id):
        paths = self.paths(account_id)
        if not paths.registry.exists():
            return None
        value = _read_json(paths.registry, 8192)
        if (value is None or value.get('account_id') != account_id or
                type(value.get('pid')) is not int or value['pid'] <= 0 or
                not isinstance(value.get('started_at'), (int, float)) or
                not math.isfinite(value['started_at'])):
            raise PlatformError('Managed worker registry needs inspection; no process will be started or killed.',
                                code='worker_registry_invalid', status=409)
        stopped=value.get('stopped_at');birth=value.get('process_birth_id')
        if (stopped is not None and (isinstance(stopped,bool) or not isinstance(stopped,(int,float)) or not math.isfinite(stopped) or stopped<value['started_at'])) or (birth is not None and (not isinstance(birth,str) or not 1<=len(birth)<=512)):
            raise PlatformError('Managed worker identity metadata needs inspection.',code='worker_registry_invalid',status=409)
        return value

    def _running_evidence(self, account_id, record):
        if not record:
            return False,False
        process = self._processes.get(account_id)
        if process is not None and process.pid == record['pid']:
            return process.poll() is None,True
        if record.get('stopped_at') is not None:return False,True
        if not self.alive(record['pid']):return False,True
        try:snapshot=self.process_identity(record['pid'])
        except Exception:snapshot=None
        if not isinstance(snapshot,dict):return True,False
        if snapshot.get('missing'):return bool(self.alive(record['pid'])),False
        recorded=record.get('process_birth_id');current=snapshot.get('identity')
        if isinstance(recorded,str) and isinstance(current,str) and recorded!=current:return False,True
        argv=snapshot.get('argv')
        if isinstance(argv,list):
            expected=['-m','dreamina_api.browser_worker','run','--config',str(self.paths(account_id).config),'--account',account_id]
            if len(argv)!=8 or argv[1:]!=expected:return False,True
            if recorded and not current:return True,False
            return True,True
        # Windows can prove a new registry's birth time without inspecting its
        # command line. A legacy PID with no such evidence remains unverified.
        return True,bool(recorded and current and recorded==current)

    def _is_running(self, account_id, record):
        return self._running_evidence(account_id,record)[0]

    def _status(self, account_id, account=None):
        account = account or self.store.get_account(account_id)
        record = self._record(account_id)
        configured=_positive_int(account.get('max_concurrent',1),'max_concurrent',100)
        desired=min(configured,self.max_account_slots)
        base = {'account_id': account_id, 'profile_saved': bool(account.get('browser_profile_saved')),
                'configured_concurrency':configured,'server_slot_limit':self.max_account_slots}
        base.update(self._supervision_fields(self._supervision(account_id,record)))
        if not record:
            return base | {'state': 'not_managed', 'reason': 'No managed worker has been started.'}
        running,process_verified = self._running_evidence(account_id, record)
        raw = _read_json(self.paths(account_id).status) or {}
        verified = (process_verified and raw.get('account_id') == account_id and raw.get('pid') == record['pid'] and
                    bool(record.get('instance_id')) and raw.get('instance_id') == record['instance_id'] and
                    isinstance(raw.get('updated_at'), (int, float)) and math.isfinite(raw['updated_at']) and
                    raw['updated_at'] >= record['started_at'] - 1)
        worker_state = raw.get('state') if verified else None
        if not isinstance(worker_state, str) or not re.fullmatch(r'[a-z_]{1,40}', worker_state):
            worker_state = None
        own = self._processes.get(account_id)
        if running:
            state = 'running' if verified else 'starting' if own is not None and own.pid == record['pid'] else 'running_unverified'
            reason = 'Managed browser worker is running.' if verified else 'Waiting for this worker instance to report its status.'
        else:
            state, reason = 'stopped', 'Managed worker is stopped.'
        if verified:
            if worker_state == 'login_required':
                reason = 'Website login is required.'
            elif worker_state == 'verification_required':
                reason = 'Complete website verification in the login panel.'
            elif worker_state == 'attention_required':
                reason = 'The original task needs reconciliation; no automatic retry.'
            elif worker_state in ('worker_error', 'page_unavailable'):
                reason = 'Worker reported an execution problem; inspect the private server diagnostics.'
            elif worker_state=='browser_parked':reason='Idle browser is parked; it wakes and verifies the website before preparing a task.'
            elif worker_state=='browser_waking':reason='Browser is waking and verifying its account entry page.'
        effective=record.get('concurrency',1)
        if type(effective) is not int or not 1<=effective<=8:effective=1
        if verified and type(raw.get('effective_concurrency')) is int and 0<=raw['effective_concurrency']<=8:
            effective=raw['effective_concurrency']
        slot_fields={'effective_concurrency':effective,'target_concurrency':desired,'configuration_pending':bool(running and effective!=desired)}
        if verified:
            for name in ('active_slots','attention_slots','ready_slots','retiring_slots'):
                value=raw.get(name)
                if type(value) is int and 0<=value<=8:slot_fields[name]=value
            slots=raw.get('slots')
            if isinstance(slots,list):
                slot_fields['slots']=[{k:v for k,v in item.items() if k in ('slot_index','worker_state','job_id','submit_id')}
                                      for item in slots[:8] if isinstance(item,dict) and type(item.get('slot_index')) is int and 1<=item['slot_index']<=8]
        return base | slot_fields | {'state': state, 'pid': record['pid'], 'worker_state': worker_state,
                       'reason': reason, 'updated_at': raw['updated_at'] if verified else record['started_at']}

    def status(self, account_id):
        return self._status(account_id)

    def browser_memory_snapshot(self, *, _proc_root=None):
        """Linux RSS for proven worker-owned Chromium trees, never cmdlines.

        RSS includes shared pages once per process, so this is an operational
        upper estimate, not a container's unique physical-memory accounting.
        Unverifiable/orphan processes are excluded rather than attributed.
        """
        result={'available':False,'rss_bytes':None,'browser_processes':0,'accounts':[],'observed_at':self.clock()}
        if not sys.platform.startswith('linux'):return result
        root=Path(_proc_root or '/proc');processes={}
        try:
            for directory in root.iterdir():
                if not directory.name.isdigit():continue
                if len(processes)>=20000:return result
                try:
                    fields=(directory/'stat').read_text().rsplit(')',1)[1].split()
                    raw=(directory/'cmdline').read_bytes()
                    if len(raw)>65536:continue
                    argv=[part.decode('utf-8','strict') for part in raw.split(b'\0') if part]
                    processes[int(directory.name)]={'parent':int(fields[1]),'birth':fields[19],
                        'rss':max(0,int(fields[21]))*os.sysconf('SC_PAGE_SIZE'),'argv':argv}
                except (OSError,UnicodeError,ValueError,IndexError):continue
        except OSError:return result
        result.update(available=True,rss_bytes=0)
        def descends(pid,ancestor):
            seen=set()
            while pid in processes and pid not in seen:
                if pid==ancestor:return True
                seen.add(pid);pid=processes[pid]['parent']
            return False
        for account in self.store.list_accounts():
            aid=account['id']
            try:
                record=self._record(aid);running,verified=self._running_evidence(aid,record)
                if not running or not verified or record['pid'] not in processes:continue
                wanted='--user-data-dir='+str(self.paths(aid).profile.resolve())
                roots=[pid for pid,item in processes.items() if item['argv'] and
                    Path(item['argv'][0]).name in ('chrome','chromium','headless_shell','chrome-headless-shell') and
                    wanted in item['argv'] and not any(arg.startswith('--type=') for arg in item['argv']) and
                    descends(pid,record['pid'])]
                count=total=0
                for pid,item in processes.items():
                    if not any(descends(pid,browser) for browser in roots):continue
                    # A PID reused after collection must not inflate another
                    # account's metric or disclose an unrelated process.
                    try:
                        after=(root/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
                        if after[19]!=item['birth']:continue
                    except (OSError,IndexError):continue
                    count+=1;total+=item['rss']
                result['accounts'].append({'account_id':aid,'rss_bytes':total,'browser_processes':count})
                result['rss_bytes']+=total;result['browser_processes']+=count
            except (PlatformError,OSError,ValueError):continue
        return result

    def _active_process_count(self):
        count = 0
        directory = self.root / 'workers'
        if not directory.exists():
            return 0
        for path in directory.glob('*/process.json'):
            if not ACCOUNT_ID.fullmatch(path.parent.name):
                continue
            # Corrupt registries conservatively prevent new capacity claims.
            try:
                record = self._record(path.parent.name)
                count += int(self._is_running(path.parent.name, record))
            except (PlatformError,OSError,ValueError):
                # Reserve this possibly occupied capacity, without poisoning
                # every other account's startup or its crash circuit.
                count += 1
        return count

    def _account_egress(self,account_id):
        if hasattr(self.store,'resolve_account_egress'):return self.store.resolve_account_egress(account_id)
        settings=self.store.settings_for_account(account_id)
        value=getattr(settings,'egress',None)
        if value is not None:return value
        proxy=getattr(settings,'proxy',None)
        if proxy:
            spec=parse_proxy_url(proxy)
            # Legacy standalone fixtures/settings retain an explicit proxy;
            # managed production storage migrates these before this path.
            return Egress('proxy_'+spec.fingerprint[:32],spec.canonical_url,spec.browser_proxy(),'proxy-proxy_'+spec.fingerprint[:32])
        return direct_egress()

    def _child_environment(self, account_id,egress=None):
        environment = {key: value for key, value in os.environ.items() if key.upper() in SYSTEM_ENV}
        key = self.store.profile_cipher_key(account_id)
        if isinstance(key, bytes):
            key = key.decode('ascii')
        environment.update(PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1', DREAMINA_PROFILE_KEY=key)
        environment['DREAMINA_EGRESS']=(egress or self._account_egress(account_id)).to_worker_json()
        return environment

    async def start(self, account_id, *, _automatic=False):
        async with self._lock:
            account = (await db_call(self.store.get_account, account_id, _store=self.store))
            paths = self.paths(account_id)
            record = (await db_call(self._record, account_id, _store=self.store))
            state=(await db_call(self._supervision, account_id,record,persist=True, _store=self.store))
            if _automatic:
                if not state['desired_running'] or state['circuit_open'] or (state.get('next_restart_at') or 0)>self.clock():
                    return (await db_call(self._status, account_id,account, _store=self.store))
                if account.get('status') in ('login_required','verification_required','expired','error','login_pending','deleting','deleted'):
                    (await db_call(self._save_supervision, account_id,dict(state,reason='account_attention_required'), _store=self.store))
                    return (await db_call(self._status, account_id,account, _store=self.store))
            if (await db_call(self._is_running, account_id, record, _store=self.store)):
                if not _automatic:(await db_call(self._save_supervision, account_id,dict(state,desired_running=True,failures=0,circuit_open=False,next_restart_at=None,reason=None), _store=self.store))
                return (await db_call(self._status, account_id, account, _store=self.store))
            if account.get('status') == 'login_pending':
                raise PlatformError('Finish the login session before starting its worker.', code='login_busy', status=409)
            if not account.get('browser_profile_saved'):
                raise PlatformError('Complete website login and save this account profile first.', code='profile_missing', status=409)
            if not account.get('enabled', True):
                return {'account_id': account_id, 'state': 'disabled', 'profile_saved': True,
                        'reason': 'Account is disabled; its saved profile was retained.'}
            if not _automatic:
                state=(await db_call(self._save_supervision, account_id,dict(state,desired_running=True,failures=0,circuit_open=False,
                    next_restart_at=None,stable_since=None,last_failed_instance=None,reason=None), _store=self.store))
            if (await db_call(self._active_process_count, _store=self.store)) >= self.max_workers:
                (await db_call(self._save_supervision, account_id,dict(state,next_restart_at=self.clock()+15,reason='worker_capacity_wait'), _store=self.store))
                return {'account_id': account_id, 'state': 'saved_capacity_wait', 'profile_saved': True,
                        'reason': 'Login is saved; the managed worker capacity limit has been reached.'}
            worker_key = self.config.get('CHROME_BRIDGE_KEY', '')
            if not isinstance(worker_key, str) or len(worker_key) < 24 or any(x.isspace() for x in worker_key):
                raise PlatformError('Configure the internal worker key before starting managed workers.', code='worker_key_missing', status=503)
            if not paths.auth.is_file():
                raise PlatformError('The encrypted browser login snapshot is missing.', code='profile_state_missing', status=409)
            instance_id = uuid.uuid4().hex
            item = {'id': account_id, 'profile_dir': str(paths.profile), 'channel': self.channel,
                    'headless': self.headless, 'encrypted_state_file': str(paths.auth), 'instance_id': instance_id,
                    'args': list(self.browser_args),'prepare_attempts':self.prepare_attempts,
                    'concurrency':min(_positive_int(account.get('max_concurrent',1),'max_concurrent',100),self.max_account_slots),
                    'preparation_timeout_seconds':self.preparation_timeout,'submission_timeout_seconds':self.submission_timeout,
                    'generation_timeout_seconds':self.generation_timeout,'entry_refresh_seconds':self.entry_refresh_seconds,'page_reload_jobs':self.page_reload_jobs}
            try:
                await db_call(self._assert_idle,account_id,account,_store=self.store)
                item['clean_cache_on_start']=True
            except PlatformError:item['clean_cache_on_start']=False
            egress=await db_call(self._account_egress,account_id,_store=self.store)
            item.update(proxy_id=egress.proxy_id,egress_group=egress.group,egress_managed=True)
            executable = self.config.get('LOGIN_BROWSER_EXECUTABLE')
            if executable:
                item['executable_path'] = executable
            config = {'server_url': self.config.get('INTERNAL_API_URL', 'http://127.0.0.1:8787'),
                      'worker_key': worker_key, 'accounts': [item]}
            _write_private(paths.config, config)
            try:
                safe_config(paths.config, account_id)
            except ValueError as error:
                raise PlatformError('Managed worker configuration is invalid.', code='worker_config_invalid', status=503) from error
            paths.state.mkdir(parents=True, exist_ok=True)
            if paths.stop.exists():
                paths.stop.unlink()  # Exact account-scoped file; never recursive.
            command = [sys.executable, '-m', 'dreamina_api.browser_worker', 'run',
                       '--config', str(paths.config), '--account', account_id]
            options = {'cwd': str(ROOT), 'stdin': subprocess.DEVNULL, 'env': await db_call(self._child_environment,account_id,egress,_store=self.store),
                       'close_fds': True}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW
            else:
                options['start_new_session'] = True
            started_at = self.clock()
            try:
                stdout_path, stderr_path = paths.directory / 'worker.stdout.log', paths.directory / 'worker.stderr.log'
                for log_path in (stdout_path, stderr_path):
                    if not log_path.resolve().is_relative_to(self.root):
                        raise ValueError('Managed worker log path escapes its configured root.')
                    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    os.close(descriptor)
                    try:
                        log_path.chmod(0o600)
                    except OSError:
                        pass
                with stdout_path.open('ab') as stdout, stderr_path.open('ab') as stderr:
                    process = self.process_factory(command, stdout=stdout, stderr=stderr, **options)
            except Exception as error:
                if _automatic:(await db_call(self._restart_failure, account_id,state,'launch-'+uuid.uuid4().hex, _store=self.store))
                raise PlatformError('The managed worker could not start. Inspect private server diagnostics.',
                                    code='worker_start_failed', status=503) from error
            self._processes[account_id] = process
            try:birth=(self.process_identity(process.pid) or {}).get('identity')
            except Exception:birth=None
            _write_private(paths.registry, {'account_id': account_id, 'pid': process.pid,
                'instance_id': instance_id, 'started_at': started_at,'concurrency':item['concurrency'],
                **({'process_birth_id':birth} if isinstance(birth,str) else {})})
            (await db_call(self._save_supervision, account_id,dict(state,desired_running=True,next_restart_at=None,stable_since=None,reason=None), _store=self.store))
            return {'account_id': account_id, 'state': 'starting', 'pid': process.pid, 'profile_saved': True,
                    'reason': 'Worker started; waiting for a confirmed ready heartbeat.'}

    def _assert_idle(self, account_id, account):
        blocking=account.get('blocking_tasks')
        if type(blocking) is not int or blocking<0:blocking=account.get('active_tasks',0)
        if blocking:
            raise PlatformError('This account has unfinished tasks; the worker will not be stopped.', code='account_busy', status=409)
        if self.bridge is not None:
            records = self.bridge.accounts()
            for entry in records:
                if entry.get('account_id') != account_id:continue
                blocked=entry.get('blocking_jobs')
                if type(blocked) is not int or blocked<0:blocked=sum(entry.get('jobs',{}).get(state,0) for state in ACTIVE_BRIDGE_STATES)
                if blocked:
                    raise PlatformError('This account has unfinished browser work; reconcile it before stopping.', code='account_busy', status=409)

    async def stop(self, account_id, *, _preserve_desired=False):
        async with self._lock:
            account = (await db_call(self.store.get_account, account_id, _store=self.store))
            (await db_call(self._assert_idle, account_id, account, _store=self.store))
            record = (await db_call(self._record, account_id, _store=self.store))
            state=(await db_call(self._supervision, account_id,record,persist=True, _store=self.store))
            if not _preserve_desired:
                (await db_call(self._save_supervision, account_id,dict(state,desired_running=False,next_restart_at=None,stable_since=None,reason='operator_stopped'), _store=self.store))
            if not (await db_call(self._is_running, account_id, record, _store=self.store)):
                if record and self.bridge is not None and hasattr(self.bridge,'prune_workers'):
                    await db_call(self.bridge.prune_workers,account_id,keep_slots=0,process_stopped=True,_store=self.store)
                return {'account_id': account_id, 'state': 'stopped', 'reason': 'No running managed worker.'}
            paths = self.paths(account_id)
            _write_private(paths.stop, {'requested_at': self.clock(), 'reason': 'operator_login_or_stop'})
            deadline = self.clock() + self.stop_timeout
            while self.clock() < deadline:
                if not (await db_call(self._is_running, account_id, record, _store=self.store)):
                    _write_private(paths.registry, dict(record, stopped_at=self.clock()))
                    self._processes.pop(account_id, None)
                    if self.bridge is not None and hasattr(self.bridge,'prune_workers'):
                        await db_call(self.bridge.prune_workers,account_id,keep_slots=0,process_stopped=True,_store=self.store)
                    return {'account_id': account_id, 'state': 'stopped', 'reason': 'Worker exited normally after a stop request.'}
                await self.sleep(0.2)
            raise PlatformError('The worker has not confirmed a normal stop. No process was force-terminated; retry after it exits.',
                                code='worker_stop_pending', status=409)

    def _request_retirement(self,account_id,account,record,status):
        """Shrink admission without cancelling an already assigned operation."""
        desired=min(_positive_int(account.get('max_concurrent',1),'max_concurrent',100),self.max_account_slots)
        effective=status.get('effective_concurrency',1)
        running,verified=self._running_evidence(account_id,record)
        if not running or not verified or desired>=effective:return False
        _write_private(self.paths(account_id).state/'slot-retirement.json',{
            'instance_id':record.get('instance_id'),'concurrency':desired,'requested_at':self.clock()})
        return True

    async def reconfigure(self,account_id):
        """Apply a busy shrink immediately; expansion uses the idle restart."""
        async with self._lock:
            account=await db_call(self.store.get_account,account_id,_store=self.store)
            record=await db_call(self._record,account_id,_store=self.store)
            status=await db_call(self._status,account_id,account,_store=self.store)
            await db_call(self._request_retirement,account_id,account,record,status,_store=self.store)
            return status

    async def remove_node(self,account_id):
        """Stop one managed node normally and retire its local registration.

        Profile, encrypted login and task journals are retained. Unknown live
        profile owners block removal; no browser/process is force-terminated.
        """
        await self.stop(account_id)
        async with self._lock:
            account=await db_call(self.store.get_account,account_id,_store=self.store)
            await db_call(self._assert_idle,account_id,account,_store=self.store)
            paths=self.paths(account_id);record=await db_call(self._record,account_id,_store=self.store)
            if await db_call(self._is_running,account_id,record,_store=self.store):
                raise PlatformError('Worker still owns this node.',code='worker_stop_pending',status=409)
            shutdown=_read_json(paths.state/'shutdown.json')
            if record and isinstance(shutdown,dict) and shutdown.get('state')=='shutdown_incomplete':
                observed=shutdown.get('observed_at');errors=shutdown.get('errors')
                if type(observed) in (int,float) and observed>=record['started_at']-1 and isinstance(errors,list) and any(
                        value in ('browser_close_timeout','browser_close_failed') for value in errors):
                    raise PlatformError('The latest browser shutdown was not confirmed; node registration was retained.',code='worker_profile_busy',status=409)
            for name in ('.dreamina-worker.lock','SingletonLock','SingletonSocket','SingletonCookie'):
                candidate=paths.profile/name
                if candidate.exists() or candidate.is_symlink():
                    raise PlatformError('Browser profile closure is unconfirmed; node registration was retained.',code='worker_profile_busy',status=409)
            if record:_write_private(paths.directory/'retired-node.json',dict(record,removed_at=self.clock()))
            state=await db_call(self._supervision,account_id,record,persist=True,_store=self.store)
            await db_call(self._save_supervision,account_id,dict(state,desired_running=False,next_restart_at=None,reason='account_node_removed'),_store=self.store)
            for candidate in (paths.registry,paths.status):
                if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root):
                    raise PlatformError('Node registration path needs inspection.',code='invalid_worker_path',status=409)
                candidate.unlink(missing_ok=True)
            self._processes.pop(account_id,None);self._scan_seen.pop(account_id,None)
            return {'account_id':account_id,'state':'removed','reason':'Managed node exited; recovery journals and saved profile retained.'}

    async def scan(self):
        """Restart only desired, proven-dead workers; preserve their journals.

        Running-but-unverified processes are never killed or duplicated. Every
        launch reuses recovery state. Manual stops and login/verification blocks
        survive service restarts; repeated crashes open an operator-visible circuit.
        """
        result = [];resize=[];restart=[]
        async with self._lock:
            for account in await asyncio.to_thread(self.store.list_accounts):
                aid=account['id'];paths=self.paths(aid)
                if not paths.registry.exists() and not (paths.directory/'supervision.json').exists():continue
                try:
                    record=(await db_call(self._record, aid, _store=self.store));state=(await db_call(self._supervision, aid,record,persist=True, _store=self.store))
                    status=(await db_call(self._status, aid,account, _store=self.store))
                except PlatformError as error:
                    result.append({'account_id':aid,'state':'supervision_attention','reason':error.code,'restart_circuit_open':True})
                    continue
                result.append(status)
                if account.get('status') == 'login_pending':
                    continue
                if state['desired_running'] and str(state.get('reason','')).startswith('proxy_'):
                    if not state['circuit_open'] and (state.get('next_restart_at') or 0)>self.clock():continue
                    try:await db_call(self._account_egress,aid,_store=self.store)
                    except PlatformError as error:
                        if str(error.code).startswith('proxy_'):
                            if state['circuit_open'] or (state.get('next_restart_at') or 0)<=self.clock():
                                await db_call(self._proxy_wait,aid,state,error.code,_store=self.store)
                            continue
                        raise
                    else:
                        state=await db_call(self._save_supervision,aid,dict(state,circuit_open=False,failures=0,
                            next_restart_at=self.clock(),last_failed_instance=None,reason=None),_store=self.store)
                blocking=account.get('blocking_tasks')
                if type(blocking) is not int or blocking<0:blocking=account.get('active_tasks',0)
                if status.get('configuration_pending'):
                    await db_call(self._request_retirement,aid,account,record,status,_store=self.store)
                if status.get('configuration_pending') and account.get('enabled') and not blocking and not account.get('blocked_by_uncertain'):
                    resize.append(aid)
                if status.get('worker_state') in ('login_required','verification_required'):
                    observed=status.get('updated_at',0)
                    if observed >= (account.get('last_login_at') or 0):
                        signature=(status.get('pid'),observed,status['worker_state'])
                        if self._scan_seen.get(aid)!=signature:
                            await asyncio.to_thread(self.store.record_account_check,aid,status='login_required',balance=account.get('balance'))
                            self._scan_seen[aid]=signature
                        (await db_call(self._save_supervision, aid,dict(state,reason='account_attention_required',stable_since=None), _store=self.store))
                        continue
                if not state['desired_running'] or not account.get('enabled') or account.get('status') in ('login_required','verification_required','expired','error','deleting','deleted'):
                    continue
                running,verified=self._running_evidence(aid,record)
                if running:
                    fresh=status.get('updated_at',0)>=self.clock()-30
                    if verified and fresh and status.get('worker_state') in ('ready','processing','browser_parked'):
                        if state.get('stable_since') is None:
                            (await db_call(self._save_supervision, aid,dict(state,stable_since=self.clock()), _store=self.store))
                        elif self.clock()-state['stable_since']>=self.restart_stable_seconds and (state['failures'] or state['circuit_open'] or state.get('proxy_failures')):
                            (await db_call(self._save_supervision, aid,dict(state,failures=0,proxy_failures=0,circuit_open=False,next_restart_at=None,last_failed_instance=None,reason=None), _store=self.store))
                    elif state.get('stable_since') is not None:
                        (await db_call(self._save_supervision, aid,dict(state,stable_since=None), _store=self.store))
                    continue
                if state['circuit_open']:continue
                if await db_call(self._proxy_failure_receipt,aid,record,_store=self.store):
                    status['worker_error_code']='proxy_error'
                    if state.get('last_proxy_instance')!=record.get('instance_id'):
                        state=await db_call(self._proxy_wait,aid,dict(state,last_proxy_instance=record.get('instance_id')),'proxy_error',_store=self.store)
                        continue
                if state.get('next_restart_at') is None:
                    state=(await db_call(self._restart_failure, aid,state,(record or {}).get('instance_id') or 'missing-process-record', _store=self.store))
                if not state['circuit_open'] and (state.get('next_restart_at') or 0)<=self.clock():restart.append(aid)
        # stop/start own the same lock and recheck all task state. Never restart
        # under the scan lock, and never force-terminate a busy or unknown PID.
        for account_id in resize:
            try:
                await self.stop(account_id,_preserve_desired=True)
                await self.start(account_id,_automatic=True)
            except PlatformError:continue
        for account_id in restart:
            try:await self.start(account_id,_automatic=True)
            except PlatformError as error:
                async with self._lock:
                    state=(await db_call(self._supervision, account_id,persist=True, _store=self.store))
                    if str(error.code).startswith('proxy_'):
                        await db_call(self._proxy_wait,account_id,state,error.code,_store=self.store)
                    elif error.code=='worker_status_unavailable':
                        await db_call(self._save_supervision,account_id,dict(state,next_restart_at=self.clock()+5,reason=error.code),_store=self.store)
                    elif error.code not in ('worker_start_failed',):
                        (await db_call(self._save_supervision, account_id,dict(state,circuit_open=True,next_restart_at=None,reason=error.code), _store=self.store))
        return result
