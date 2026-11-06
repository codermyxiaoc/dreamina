"""Persistent, at-most-once handoff to an explicitly paired Chrome extension.

This module never controls Chrome, reads browser cookies, or submits an upstream
request. ``submitted`` means the paired worker observed a new page card; the
existing HTTP task query must confirm its actual upstream state.

First-version capability is intentionally narrow: Mini, 4s, 720p, 16:9,
text-only Omni mode. A lost claim becomes uncertain and is never requeued.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Annotated, Callable, Literal
import uuid
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .config import ROOT, load_env
from .models import model_config, validate_request
from .image_models import IMAGE_MODEL, IMAGE_MODEL_NAME, validate_image_spec
from .client import API_ORIGINS
from .audit import sanitize
from .upstream_errors import sanitize_upstream_error


PREFIX = '/v1/chrome'
ACCOUNT_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
WORKER_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
EXTENSION_PATTERN = re.compile(r'^[a-p]{32}$')
ASSET_PATTERN = r'^asset_\d{1,2}_[0-9a-f]{24}$'
RESOURCE_ID_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9/_.-]{0,1023}$'
CAPABILITIES = {'model': 'seedance-2.0-mini', 'duration': 4, 'resolution': '720p',
                'ratio': '16:9', 'mode': 'omni', 'references': True, 'seed': False,
                'image': {'model': 'seedream-5.0-lite', 'size':'2048x2048', 'n':1},
                'models':['seedance-2.5','seedance-2.0-mini','seedance-2.0-fast','seedance-2.0'],
                'new_session':True,'reference_upload':True}
STATES = {'queued', 'claimed', 'submitted', 'completed', 'failed', 'uncertain'}


class BridgeError(Exception):
    def __init__(self, message: str, *, status: int = 409):
        super().__init__(message)
        self.status = status


def _identifier(value: str, *, account: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(ACCOUNT_PATTERN if account else WORKER_PATTERN, value):
        raise ValueError('Invalid account_id.' if account else 'Invalid worker_id.')
    return value


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('A UUID is required.') from error


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _reference_bindings(value, request):
    """Validate worker-observed resource IDs against this job's private assets."""
    if not isinstance(value,dict) or not 1<=len(value)<=50:
        raise ValueError('reference_bindings must contain 1–50 asset bindings.')
    refs=request.get('references') or []
    expected={r['asset_id']:r['kind'] for r in refs}
    if len(expected)!=len(refs) or set(value)!=set(expected):
        raise ValueError('reference_bindings must match all assets of this job exactly.')
    clean={}
    for asset_id,binding in value.items():
        if not isinstance(asset_id,str) or not re.fullmatch(ASSET_PATTERN,asset_id):
            raise ValueError('Invalid binding asset identity.')
        if not isinstance(binding,dict) or set(binding)!={'kind','resource_id'} or binding['kind']!=expected[asset_id]:
            raise ValueError('Reference binding kind does not match this job.')
        resource=binding['resource_id']
        if not isinstance(resource,str) or not re.fullmatch(RESOURCE_ID_PATTERN,resource):
            raise ValueError('Reference resource_id must be a bounded resource identifier, not a URL or credential header.')
        clean[asset_id]={'kind':binding['kind'],'resource_id':resource}
    return clean


def _bridge_request(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError('request must be a generation object.')
    if value.get('kind') == 'image' or value.get('model') == IMAGE_MODEL:
        clean={k:v for k,v in value.items() if k!='new_session'}
        return dict(validate_image_spec(clean),new_session=True)
    allowed = {'prompt', 'model', 'duration', 'ratio', 'resolution', 'mode','references','asset_owner_task_id','new_session'}
    if set(value) - allowed:
        raise ValueError('Chrome bridge v1 supports text only; allowed fields: ' + ', '.join(sorted(allowed)))
    result = validate_request({k:v for k,v in value.items() if k in ('prompt','model','duration','ratio','resolution','mode')})
    if result['mode']!='omni' or result['resolution'].lower() not in ('480p','720p','1080p'):
        raise ValueError('Bridge supports native-resolution omni generation.')
    refs=value.get('references',[])
    if not isinstance(refs,list) or len(refs)>50:raise ValueError('Invalid reference list.')
    owner=value.get('asset_owner_task_id')
    if refs and (not isinstance(owner,str) or not re.fullmatch(r'video_[0-9a-f]{32}',owner)):raise ValueError('References require their owning platform task.')
    for item in refs:
        if not isinstance(item,dict) or not re.fullmatch(r'asset_\d{1,2}_[0-9a-f]{24}',item.get('asset_id','')) or item.get('kind') not in ('image','video') or not isinstance(item.get('size_bytes'),int) or not re.fullmatch(r'[0-9a-f]{64}',item.get('sha256','')):
            raise ValueError('Invalid private reference metadata.')
    if refs:
        from .video_inputs import validate_references
        validate_references(result,[{'metadata':item} for item in refs])
    return {key:result[key] for key in ('prompt','model','duration','ratio','resolution','mode')}|{'new_session':True,'references':refs,'asset_owner_task_id':owner}


class BridgeStore:
    """SQLite transactions serialize claims across threads and processes.

    claim_timeout bounds a webpage action, not upstream generation. Once a
    submit_id is observed, the worker must report it immediately; HTTP polling
    can then proceed independently. Expired/abandoned claims stay uncertain.
    """

    def __init__(self, directory: Path | str | None = None, *, clock: Callable[[], float] = time.time,
                 heartbeat_timeout: float = 90, claim_timeout: float = 600):
        self.directory = Path(directory) if directory is not None else ROOT / 'outputs' / 'chrome-bridge'
        self.clock = clock
        self.heartbeat_timeout = heartbeat_timeout
        self.claim_timeout = claim_timeout
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'bridge.sqlite3'
        with self._connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    worker_id TEXT,
                    claim_hash TEXT,
                    claimed_at REAL,
                    claim_expires_at REAL,
                    submit_id TEXT UNIQUE,
                    api_origin TEXT,
                    error TEXT,
                    result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_account_status ON jobs(account_id, status, sequence);
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    ready INTEGER NOT NULL,
                    page_path TEXT NOT NULL
                );
            ''')
            columns={row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'api_origin' not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN api_origin TEXT')
            if 'telemetry_json' not in columns:db.execute('ALTER TABLE jobs ADD COLUMN telemetry_json TEXT')
            if 'reference_bindings_json' not in columns:db.execute('ALTER TABLE jobs ADD COLUMN reference_bindings_json TEXT')
            if 'credit_preflight_json' not in columns:db.execute('ALTER TABLE jobs ADD COLUMN credit_preflight_json TEXT')
            if 'upstream_error_json' not in columns:db.execute('ALTER TABLE jobs ADD COLUMN upstream_error_json TEXT')
            for name,kind in (('preparation_started_at','REAL'),('detached_at','REAL'),('submission_window_closed_at','REAL')):
                if name not in columns:db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
            for name,kind in (('activation_started_at','REAL'),('submitted_at','REAL'),('confirmed_at','REAL'),('error_code','TEXT'),('claim_protocol_version','INTEGER'),('claim_request_id','TEXT')):
                if name not in columns:db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
            if 'confirmed_at' not in columns:db.execute("UPDATE jobs SET confirmed_at=COALESCE(submitted_at,updated_at) WHERE status IN ('submitted','completed') OR result_json IS NOT NULL")
            db.execute('CREATE TABLE IF NOT EXISTS claim_fences(worker_id TEXT NOT NULL,request_id TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(worker_id,request_id))')
            db.execute('CREATE TABLE IF NOT EXISTS dispatch_fences(job_id TEXT PRIMARY KEY,created_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS retired_accounts(account_id TEXT PRIMARY KEY,retired_at REAL NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_claim_request ON jobs(worker_id,claim_request_id)')
            db.execute('CREATE INDEX IF NOT EXISTS ix_jobs_expiration ON jobs(status,claim_expires_at)')
            worker_columns={r[1] for r in db.execute('PRAGMA table_info(workers)')}
            if 'protocol_version' not in worker_columns:db.execute('ALTER TABLE workers ADD COLUMN protocol_version INTEGER DEFAULT 3')
            if 'driver' not in worker_columns:db.execute("ALTER TABLE workers ADD COLUMN driver TEXT DEFAULT 'extension'")
            if 'reason' not in worker_columns:db.execute('ALTER TABLE workers ADD COLUMN reason TEXT')
            for name,kind in (('slot_index','INTEGER'),('slot_count','INTEGER'),('owner_id','TEXT')):
                if name not in worker_columns:db.execute(f'ALTER TABLE workers ADD COLUMN {name} {kind}')
            for name,kind in (('entry_ready','INTEGER'),('entry_checked_at','REAL')):
                if name not in worker_columns:db.execute(f'ALTER TABLE workers ADD COLUMN {name} {kind}')
            for name,kind in (('entry_parked','INTEGER NOT NULL DEFAULT 0'),('wakeable','INTEGER NOT NULL DEFAULT 0'),('last_entry_ready_at','REAL')):
                if name not in worker_columns:db.execute(f'ALTER TABLE workers ADD COLUMN {name} {kind}')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA busy_timeout=10000')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _expire(self, db, now: float) -> None:
        db.execute('''UPDATE jobs SET status=CASE WHEN claim_protocol_version>=5 AND activation_started_at IS NULL AND submit_id IS NULL THEN 'failed' ELSE 'uncertain' END, updated_at=?,error_code='worker_connection_lost',
                      error='Browser claim expired or disconnected. Verify the original page/task before any retry.'
                      WHERE status='claimed' AND (claim_expires_at <= ? OR NOT EXISTS (
                          SELECT 1 FROM workers WHERE workers.worker_id=jobs.worker_id
                          AND workers.account_id=jobs.account_id AND workers.last_seen > ?))''',
                   (now, now, now - self.heartbeat_timeout))

    @staticmethod
    def _public(row) -> dict:
        result = {key: row[key] for key in ('id', 'account_id', 'status', 'created_at', 'updated_at',
                                           'worker_id', 'claimed_at', 'claim_expires_at', 'submit_id', 'api_origin', 'error',
                                           'activation_started_at','submitted_at','confirmed_at','error_code','claim_protocol_version','claim_request_id','preparation_started_at','detached_at','submission_window_closed_at')}
        result['slot_released']=row['status'] in ('completed','failed') or bool(row['detached_at']) or row['status']=='uncertain' and (bool(row['submission_window_closed_at']) or bool(row['submit_id']) and row['error_code'] in ('generation_timeout','task_timeout'))
        if row['status']=='failed' and (row['claim_protocol_version'] or 0)>=5 and row['activation_started_at'] is None and row['submit_id'] is None and row['confirmed_at'] is None:
            result['generation_not_activated']=True
        result['request'] = json.loads(row['request_json'])
        result['model_name'] = IMAGE_MODEL_NAME if result['request']['model'] == IMAGE_MODEL else model_config(result['request']['model'])['name']
        result['result'] = json.loads(row['result_json']) if row['result_json'] else None
        result['telemetry'] = json.loads(row['telemetry_json']) if row['telemetry_json'] else None
        result['reference_bindings'] = json.loads(row['reference_bindings_json']) if row['reference_bindings_json'] else None
        result['credit_preflight'] = json.loads(row['credit_preflight_json']) if row['credit_preflight_json'] else None
        result['upstream_error'] = json.loads(row['upstream_error_json']) if row['upstream_error_json'] else None
        result['submission_evidence'] = (
            'upstream-ledger-reconciled' if (result['telemetry'] or {}).get('stage')=='manual_reconciliation'
            else 'native-response-observed'
        ) if row['submit_id'] else None
        # claim_hash/token never appears in public task records.
        return result

    def enqueue(self, request: dict, *, account_id: str = 'default', job_id: str | None = None) -> dict:
        account_id = _identifier(account_id, account=True)
        request = _bridge_request(request)
        now, job_id = self.clock(), _uuid(job_id) if job_id else str(uuid.uuid4())
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',(account_id,)).fetchone():
                raise BridgeError('This account was deleted; browser registration and new work are disabled.',status=410)
            existing=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if existing:
                if existing['account_id']!=account_id or existing['request_json']!=_json(request):
                    raise BridgeError('Deterministic dispatch ID conflicts with another request.')
                return self._public(existing)
            fenced=db.execute('SELECT 1 FROM dispatch_fences WHERE job_id=?',(job_id,)).fetchone()
            db.execute('INSERT INTO jobs(id,account_id,request_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (job_id, account_id, _json(request), 'failed' if fenced else 'queued', now, now))
            if fenced:db.execute("UPDATE jobs SET error_code='dispatch_failed',error='This dispatch was closed before activation.' WHERE id=?",(job_id,))
            return self._public(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def get(self, job_id: str) -> dict:
        job_id = _uuid(job_id)
        with self._connection() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise BridgeError('Chrome bridge job not found.', status=404)
            return self._public(row)

    def list_jobs(self, *, account_id: str | None = None, limit: int = 50) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100.')
        if account_id is not None:
            _identifier(account_id, account=True)
        with self._connection() as db:
            sql, values = 'SELECT * FROM jobs', []
            if account_id:
                sql += ' WHERE account_id=?'
                values.append(account_id)
            sql += ' ORDER BY sequence DESC LIMIT ?'
            values.append(limit)
            return [self._public(row) for row in db.execute(sql, values)]

    def heartbeat(self, account_id: str, worker_id: str, *, ready: bool, page_url: str, protocol_version:int=4,driver:str='extension',reason:str='',slot_index=None,slot_count=None,owner_id=None,entry_ready=None,entry_checked_at=None,retire_above=None,entry_parked=None,wakeable=None) -> dict:
        if retire_above is not None and (type(retire_above) is not int or not 0<=retire_above<=8):raise ValueError('Invalid slot limit.')
        from urllib.parse import urlsplit
        account_id, worker_id = _identifier(account_id, account=True), _identifier(worker_id)
        if not isinstance(ready, bool):
            raise ValueError('ready must be a boolean.')
        if protocol_version>=8:
            if type(entry_parked) is not bool or type(wakeable) is not bool:raise ValueError('V8 heartbeat requires explicit parking and wakeable flags.')
            if entry_parked and entry_ready:raise ValueError('A parked entry cannot report a current ready page.')
            if wakeable and not entry_parked:raise ValueError('Wakeable permission requires a parked entry.')
        elif entry_parked not in (None,False) or wakeable not in (None,False):raise ValueError('Parking evidence requires v8.')
        parked=entry_parked is True;wants_wake=wakeable is True
        ready=ready and protocol_version>=4
        if protocol_version>=7:
            if type(entry_ready) is not bool or type(entry_checked_at) not in (int,float) or not math.isfinite(entry_checked_at):raise ValueError('V7 heartbeat requires entry readiness evidence.')
            ready=ready and entry_ready and -5<=self.clock()-entry_checked_at<=120
        elif entry_ready is not None or entry_checked_at is not None:raise ValueError('Entry readiness evidence requires v7.')
        if driver not in ('extension','native'):raise ValueError('Unknown worker driver.')
        if protocol_version>=5:
            if driver!='native' or type(slot_index) is not int or type(slot_count) is not int or not 1<=slot_index<=slot_count<=8:
                raise ValueError('Native slot metadata is invalid.')
            owner_id=_identifier(owner_id)
        elif any(x is not None for x in (slot_index,slot_count,owner_id)):raise ValueError('Slot metadata requires protocol version 5.')
        if load_env().get('WORKER_MODE')=='native' and driver!='native':ready=False;reason='Native worker mode is enabled; extension dispatch is disabled.'
        parsed = urlsplit(page_url)
        if (parsed.scheme != 'https' or parsed.hostname != 'dreamina.capcut.com' or parsed.username
                or parsed.password or parsed.port not in (None, 443) or not parsed.path.startswith('/ai-tool/')):
            raise ValueError('Worker page must be an official Dreamina /ai-tool/ page.')
        now = self.clock()
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # Expire before reviving a worker, so reconnecting cannot silently
            # resurrect an abandoned claim and permit a duplicate click.
            self._expire(db, now)
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',(account_id,)).fetchone():
                raise BridgeError('This account was deleted; late heartbeats cannot recreate its nodes.',status=410)
            old = db.execute('SELECT account_id,last_entry_ready_at FROM workers WHERE worker_id=?', (worker_id,)).fetchone()
            if old and old['account_id'] != account_id:
                raise BridgeError('This worker_id is already bound to a different account_id.')
            verified_at=old['last_entry_ready_at'] if old else None
            if not parked:
                verified_at=entry_checked_at if entry_ready is True and type(entry_checked_at) in (int,float) and -5<=now-entry_checked_at<=120 else None
            lease_fresh=verified_at is not None and -5<=now-verified_at<=120
            wakeable=parked and wants_wake and lease_fresh
            ready=bool(ready or wakeable)
            if retire_above is not None and slot_index is not None and slot_index>retire_above:
                ready=False
                if not db.execute("SELECT 1 FROM jobs WHERE account_id=? AND worker_id=? AND status IN ('claimed','submitted','uncertain') LIMIT 1",(account_id,worker_id)).fetchone():
                    db.execute('DELETE FROM workers WHERE account_id=? AND worker_id=?',(account_id,worker_id))
                    return {'ok':True,'account_id':account_id,'worker_id':worker_id,'retired':True,'last_seen':now}
            if protocol_version>=5:
                duplicate=db.execute('SELECT worker_id FROM workers WHERE account_id=? AND owner_id=? AND slot_index=? AND worker_id<>? AND last_seen>?',(account_id,owner_id,slot_index,worker_id,now-self.heartbeat_timeout)).fetchone()
                if duplicate:raise BridgeError('Another live worker already owns this account slot.')
            db.execute('''INSERT INTO workers(worker_id,account_id,last_seen,ready,page_path) VALUES(?,?,?,?,?)
                          ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen,
                          ready=excluded.ready,page_path=excluded.page_path''',
                       (worker_id, account_id, now, int(ready), parsed.path))
            db.execute('UPDATE workers SET protocol_version=? WHERE worker_id=?',(protocol_version,worker_id))
            db.execute('UPDATE workers SET driver=?,reason=? WHERE worker_id=?',(driver,reason[:300],worker_id))
            db.execute('UPDATE workers SET slot_index=?,slot_count=?,owner_id=? WHERE worker_id=?',(slot_index,slot_count,owner_id,worker_id))
            db.execute('UPDATE workers SET entry_ready=?,entry_checked_at=? WHERE worker_id=?',(entry_ready,entry_checked_at,worker_id))
            db.execute('UPDATE workers SET entry_parked=?,wakeable=?,last_entry_ready_at=? WHERE worker_id=?',(parked,wakeable,verified_at,worker_id))
        return {'ok': True, 'account_id': account_id, 'worker_id': worker_id, 'last_seen': now,
                'capabilities': dict(CAPABILITIES),'ready':bool(ready),'wake_required':bool(parked and not lease_fresh)}

    def claim(self, account_id: str, worker_id: str, *, capacity: int = 1, paused: bool = False,request_id: str|None=None) -> dict | None:
        account_id, worker_id = _identifier(account_id, account=True), _identifier(worker_id)
        now = self.clock()
        if type(capacity) is not int or not 0<=capacity<=8:raise ValueError('Account capacity must be between zero and eight.')
        if request_id is not None:request_id=_uuid(request_id)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire(db, now)
            worker = db.execute('SELECT * FROM workers WHERE worker_id=? AND account_id=?', (worker_id, account_id)).fetchone()
            if not worker or not worker['ready'] or worker['last_seen'] <= now - self.heartbeat_timeout or worker['entry_parked'] and (worker['last_entry_ready_at'] is None or not -5<=now-worker['last_entry_ready_at']<=120):
                raise BridgeError('A recent ready heartbeat for this worker/account is required.')
            if paused or capacity==0:return None
            if worker['protocol_version']>=5:
                if request_id is None:raise ValueError('Native slot claims require a unique claim_request_id.')
                if db.execute('SELECT 1 FROM claim_fences WHERE worker_id=? AND request_id=?',(worker_id,request_id)).fetchone():return None
                if db.execute('SELECT 1 FROM jobs WHERE worker_id=? AND claim_request_id=?',(worker_id,request_id)).fetchone():return None
                if worker['slot_index'] is None or worker['slot_index']>capacity:return None
                if db.execute("SELECT 1 FROM jobs WHERE account_id=? AND status='uncertain' AND COALESCE(claim_protocol_version,0)<5 LIMIT 1",(account_id,)).fetchone():return None
                if db.execute("SELECT 1 FROM jobs WHERE worker_id=? AND detached_at IS NULL AND (status='claimed' OR status='submitted' OR (status='uncertain' AND submission_window_closed_at IS NULL AND (submit_id IS NULL OR COALESCE(error_code,'') NOT IN ('generation_timeout','task_timeout')))) LIMIT 1",(worker_id,)).fetchone():return None
                outstanding=db.execute("SELECT count(*) FROM jobs WHERE account_id=? AND detached_at IS NULL AND (status IN ('claimed','submitted') OR (status='uncertain' AND submission_window_closed_at IS NULL AND (submit_id IS NULL OR COALESCE(error_code,'') NOT IN ('generation_timeout','task_timeout'))))",(account_id,)).fetchone()[0]
                if worker['protocol_version']<6:
                    # Older workers have no detached-capacity protocol; preserve
                    # their conservative logical limit during rolling upgrades.
                    outstanding=db.execute("SELECT count(*) FROM jobs WHERE account_id=? AND status IN ('claimed','submitted','uncertain')",(account_id,)).fetchone()[0]
                if outstanding>=capacity:return None
            elif db.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('claimed','uncertain') LIMIT 1", (account_id,)).fetchone():return None
            row = db.execute("SELECT * FROM jobs WHERE account_id=? AND status='queued' ORDER BY sequence LIMIT 1", (account_id,)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            db.execute("UPDATE jobs SET status='claimed',worker_id=?,claim_hash=?,claimed_at=?,claim_expires_at=?,claim_protocol_version=?,claim_request_id=?,updated_at=? WHERE id=? AND status='queued'",
                       (worker_id, hashlib.sha256(token.encode()).hexdigest(), now, now + self.claim_timeout,worker['protocol_version'],request_id, now, row['id']))
            result = self._public(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
            result['claim_token'] = token
            return result

    def recover_slot(self,account_id,worker_id,request_id=None):
        account_id=_identifier(account_id,account=True);worker_id=_identifier(worker_id)
        if request_id is not None:request_id=_uuid(request_id)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');now=self.clock();self._expire(db,now)
            worker=db.execute('SELECT * FROM workers WHERE account_id=? AND worker_id=?',(account_id,worker_id)).fetchone()
            if not worker or worker['protocol_version']<5:raise BridgeError('Slot recovery requires a registered native v5 worker.',status=403)
            if request_id is None:return {'job':None,'slot_released':False,'reason':'legacy_claim_without_request_identity'}
            db.execute('INSERT OR IGNORE INTO claim_fences(worker_id,request_id,created_at) VALUES(?,?,?)',(worker_id,request_id,now))
            rows=db.execute("SELECT * FROM jobs WHERE account_id=? AND worker_id=? AND status IN ('claimed','submitted','uncertain') ORDER BY sequence",(account_id,worker_id)).fetchall()
            blocking=[r for r in rows if not self._public(r)['slot_released']]
            if not blocking:return {'job':None,'slot_released':True}
            if len(blocking)!=1:return {'job':None,'slot_released':False,'reason':'multiple_unresolved_claims'}
            row=blocking[0]
            if row['claim_request_id']!=request_id:return {'job':None,'slot_released':False,'reason':'another_claim_is_in_progress'}
            if row['status']!='claimed' or (row['claim_protocol_version'] or 0)<5 or row['activation_started_at'] is not None or row['submit_id']:
                return {'job':None,'slot_released':False,'reason':'original_outcome_requires_reconciliation'}
            token=secrets.token_urlsafe(32)
            # No activation could have occurred without the central marker.
            # Rotate ownership instead of creating a second job; old tokens fail.
            db.execute('UPDATE jobs SET claim_hash=?,credit_preflight_json=NULL,updated_at=? WHERE id=?',(hashlib.sha256(token.encode()).hexdigest(),now,row['id']))
            job=self._public(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone());job['claim_token']=token
            return {'job':job,'slot_released':False,'reason':'original_unactivated_claim_recovered'}

    def report(self, job_id: str, worker_id: str, claim_token: str, *, status: str,
               submit_id: str | None = None, error: str | None = None, api_origin: str | None = None,
               reference_bindings: dict | None = None,error_code: str | None = None,generation_not_activated: bool | None = None,upstream_ret: str | None = None,upstream_error: dict | None = None,submission_window_closed: bool | None = None) -> dict:
        job_id, worker_id = _uuid(job_id), _identifier(worker_id)
        if status not in {'submitted', 'failed', 'uncertain'}:
            raise ValueError('Invalid worker report status.')
        if submission_window_closed is not None and type(submission_window_closed) is not bool:raise ValueError('Invalid page-close proof.')
        if error_code is not None and not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',error_code):raise ValueError('Invalid error code.')
        if upstream_ret is not None and (not isinstance(upstream_ret,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',upstream_ret) or upstream_ret=='0'):
            raise ValueError('A rejection requires a nonzero upstream return code.')
        diagnostic=sanitize_upstream_error(upstream_error)
        if upstream_error is not None and diagnostic is None:raise ValueError('Invalid upstream diagnostic.')
        if diagnostic and upstream_ret is not None and diagnostic.get('ret')!=upstream_ret:
            raise ValueError('Diagnostic return code does not match this rejection.')
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError('claim_token is required.')
        if submit_id is not None:
            submit_id = _uuid(submit_id)
        if reference_bindings is not None and (status not in ('submitted','uncertain') or submit_id is None):
            raise ValueError('Reference bindings require an explicit submit_id on a submitted/uncertain report.')
        if api_origin is not None and api_origin not in API_ORIGINS:
            raise ValueError('Invalid upstream API origin.')
        if error is not None and (not isinstance(error, str) or len(error) > 1000):
            raise ValueError('error must be a string up to 1000 characters.')
        now = self.clock()
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire(db, now)
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise BridgeError('Chrome bridge job not found.', status=404)
            supplied_hash = hashlib.sha256(claim_token.encode()).hexdigest()
            if row['worker_id'] != worker_id or not row['claim_hash'] or not hmac.compare_digest(row['claim_hash'], supplied_hash):
                raise BridgeError('Report does not own this claim.', status=403)
            existing = row['status']
            if submission_window_closed and ((row['claim_protocol_version'] or 0)<6 or status!='uncertain'):raise BridgeError('Page-close evidence requires an uncertain v6 receipt.')
            # The activation RPC cannot authorize a click until this queue's
            # marker commits. Fence the negative proof in the SAME transaction
            # as the report, so a delayed activation RPC cannot win afterwards.
            if status=='uncertain' and existing!='queued' and (row['claim_protocol_version'] or 0)>=5 and row['activation_started_at'] is None and not row['submit_id'] and not submit_id and row['confirmed_at'] is None:
                status='failed';generation_not_activated=True
                error_code='activation_not_committed'
                error='The browser activation was never committed; no generation was authorized.'
            diagnostic_json=row['upstream_error_json']
            if diagnostic is not None:
                encoded=_json(diagnostic)
                if diagnostic_json and diagnostic_json!=encoded:raise BridgeError('Cannot replace the original upstream diagnostic.')
                diagnostic_json=encoded
            if existing in {'completed', 'failed'}:
                if status == existing and (submit_id is None or submit_id == row['submit_id']):
                    return self._public(row)
                raise BridgeError('A terminal job cannot be changed by a later worker report.')
            timeout_report=status=='uncertain' and error_code in ('generation_timeout','task_timeout') and (submit_id or row['submit_id'])
            if existing == 'queued' or (existing == 'submitted' and status == 'uncertain' and not timeout_report):
                raise BridgeError('Invalid state transition.')
            explicit_rejection=status=='failed' and existing in ('claimed','uncertain') and row['confirmed_at'] is None and row['error_code'] not in ('generation_timeout','task_timeout') and error_code=='upstream_rejected' and upstream_ret is not None and submit_id is not None and not (diagnostic and diagnostic.get('acceptance_conflict'))
            if status=='failed' and (row['submit_id'] or submit_id) and not explicit_rejection:
                raise BridgeError('A known upstream ID requires confirmed upstream rejection or reconciliation.')
            if status=='failed' and row['activation_started_at'] is not None and not explicit_rejection and not (generation_not_activated is True and not row['submit_id'] and not submit_id):
                raise BridgeError('An activated task requires upstream reconciliation; it cannot be discarded as a preparation failure.')
            if row['submit_id'] and submit_id and row['submit_id'] != submit_id:
                raise BridgeError('Cannot replace an already observed submit_id.')
            bindings_json=row['reference_bindings_json']
            if reference_bindings is not None:
                clean_bindings=_reference_bindings(reference_bindings,json.loads(row['request_json']))
                if bindings_json and json.loads(bindings_json)!=clean_bindings:
                    raise BridgeError('Cannot replace already observed reference bindings.')
                bindings_json=bindings_json or _json(clean_bindings)
            submit_id = submit_id or row['submit_id']
            if status == 'submitted' and not submit_id:
                raise ValueError('submitted requires the observed upstream submit_id.')
            if status == 'uncertain' and not error:
                error = 'Outcome is uncertain. Verify the original page/task before retrying.'
            try:
                db.execute('UPDATE jobs SET status=?,submit_id=?,api_origin=?,error=?,error_code=?,reference_bindings_json=?,upstream_error_json=?,submission_window_closed_at=CASE WHEN ? THEN COALESCE(submission_window_closed_at,?) ELSE submission_window_closed_at END,submitted_at=?,confirmed_at=?,updated_at=? WHERE id=?',
                           (status, submit_id, api_origin or row['api_origin'], error,error_code, bindings_json,diagnostic_json,bool(submission_window_closed),now,row['submitted_at'] if row['submitted_at'] is not None else (now if submit_id else None),row['confirmed_at'] if row['confirmed_at'] is not None else (now if status=='submitted' else None), now, job_id))
            except sqlite3.IntegrityError as exc:
                raise BridgeError('This submit_id is already associated with another bridge job.') from exc
            return self._public(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def reconcile_submitted(self, job_id: str, normalized_result: dict) -> dict:
        """Root service may call after its existing authenticated HTTP poll.

        This method does not itself make network requests. A task's original
        submit_id must match; page-only completion reports are not sufficient.
        """
        job_id = _uuid(job_id)
        if not isinstance(normalized_result, dict):
            raise ValueError('A normalized task query result is required.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise BridgeError('Chrome bridge job not found.', status=404)
            if not row['submit_id'] or normalized_result.get('id') != row['submit_id']:
                raise BridgeError('Task query result does not match the observed submit_id.')
            if row['status'] not in ('submitted', 'uncertain'):
                return self._public(row)
            upstream = normalized_result.get('status')
            state = row['status']
            if upstream == 'completed' and (normalized_result.get('videos') or normalized_result.get('images')):
                state = 'completed'
            elif upstream in ('failed', 'deleted'):
                state = 'failed'
            allowed = {'id', 'object', 'status', 'upstream_status', 'history_record_id', 'videos', 'images', 'failure', 'api_origin'}
            result = {key: value for key, value in normalized_result.items() if key in allowed}
            encoded = _json(result)
            if len(encoded.encode()) > 100_000:
                raise ValueError('Task query result exceeds the local receipt limit.')
            # Only the central, identity-verified history result permits v6
            # to detach. A client-side submit_id alone never frees the page.
            detach=(row['claim_protocol_version'] or 0)>=6 and upstream in ('processing','completed','failed','deleted')
            db.execute('UPDATE jobs SET status=?,result_json=?,updated_at=?,confirmed_at=COALESCE(confirmed_at,?),detached_at=CASE WHEN ? THEN COALESCE(detached_at,?) ELSE detached_at END,error=CASE WHEN ? IN (\'completed\',\'failed\') THEN NULL ELSE error END,error_code=CASE WHEN ? IN (\'completed\',\'failed\') THEN NULL ELSE error_code END WHERE id=?',
                       (state, encoded, self.clock(),self.clock(),detach,self.clock(),state,state, job_id))
            return self._public(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def accounts(self) -> list[dict]:
        now = self.clock()
        with self._connection() as db:
            ids = [r[0] for r in db.execute('SELECT account_id FROM workers UNION SELECT account_id FROM jobs ORDER BY account_id')]
            all_workers={};all_counts={}
            for worker in db.execute('SELECT worker_id,account_id,last_seen,ready,page_path,protocol_version,driver,reason,slot_index,slot_count,owner_id,entry_ready,entry_checked_at,entry_parked,wakeable,last_entry_ready_at FROM workers'):
                all_workers.setdefault(worker['account_id'],[]).append(dict(worker))
            for row in db.execute('SELECT account_id,status,count(*) AS n FROM jobs GROUP BY account_id,status'):
                all_counts.setdefault(row['account_id'],{})[row['status']]=row['n']
            legacy_ids={r[0] for r in db.execute("SELECT DISTINCT account_id FROM jobs WHERE status='uncertain' AND COALESCE(claim_protocol_version,0)<5")}
            blocks={r['account_id']:r['n'] for r in db.execute("SELECT account_id,count(*) AS n FROM jobs WHERE detached_at IS NULL AND (status IN ('queued','claimed','submitted') OR (status='uncertain' AND submission_window_closed_at IS NULL AND (submit_id IS NULL OR COALESCE(error_code,'') NOT IN ('generation_timeout','task_timeout')))) GROUP BY account_id")}
            result = []
            for account_id in ids:
                workers = all_workers.get(account_id,[])
                for worker in workers:
                    worker['ready'] = bool(worker['ready'])
                    worker['entry_ready']=bool(worker['entry_ready'])
                    worker['entry_parked']=bool(worker['entry_parked'])
                    worker['wakeable']=bool(worker['wakeable']) and worker['last_entry_ready_at'] is not None and -5<=now-worker['last_entry_ready_at']<=120
                    if worker['entry_parked']:worker['ready']=worker['ready'] and worker['wakeable']
                    worker['online'] = worker['last_seen'] > now - self.heartbeat_timeout
                counts = all_counts.get(account_id,{})
                live=[w for w in workers if w['online']]
                multi=bool(live) and all(w['protocol_version']>=5 for w in live)
                legacy_uncertain=account_id in legacy_ids
                blocking=blocks.get(account_id,0)
                result.append({'account_id': account_id, 'workers': workers, 'jobs': counts,'attention_tasks':counts.get('uncertain',0),'blocking_jobs':blocking,
                               'blocked_by_uncertain': legacy_uncertain or bool(counts.get('uncertain')) and not multi, 'capabilities': dict(CAPABILITIES)})
            return result

    def retire_account(self,account_id):
        """Fence late registrations after logical deletion; keep task evidence."""
        account_id=_identifier(account_id,account=True)
        with self._connection() as db:
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',(account_id,)).fetchone() and not db.execute('SELECT 1 FROM workers WHERE account_id=?',(account_id,)).fetchone():
                return {'account_id':account_id,'removed':0,'retired':True}
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('queued','claimed','submitted','uncertain') LIMIT 1",(account_id,)).fetchone():
                raise BridgeError('Unfinished browser work must be reconciled before node deletion.')
            db.execute('INSERT OR IGNORE INTO retired_accounts(account_id,retired_at) VALUES(?,?)',(account_id,self.clock()))
            removed=db.execute('DELETE FROM workers WHERE account_id=?',(account_id,)).rowcount
        return {'account_id':account_id,'removed':removed,'retired':True}

    def retire_worker(self,account_id,worker_id,*,keep_slots):
        """An authenticated worker retires only an out-of-range idle slot."""
        account_id=_identifier(account_id,account=True);worker_id=_identifier(worker_id)
        if type(keep_slots) is not int or not 0<=keep_slots<=8:raise ValueError('Invalid slot limit.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM workers WHERE worker_id=?',(worker_id,)).fetchone()
            if row and row['account_id']!=account_id:raise BridgeError('Worker account mismatch.',status=403)
            if row and (row['slot_index'] is None or row['slot_index']<=keep_slots):
                raise BridgeError('A configured slot cannot be retired by this request.')
            pending=db.execute("SELECT count(*) FROM jobs WHERE account_id=? AND worker_id=? AND status IN ('claimed','submitted','uncertain')",(account_id,worker_id)).fetchone()[0]
            if pending:return {'retired':False,'pending_jobs':pending}
            db.execute('DELETE FROM workers WHERE worker_id=? AND account_id=?',(worker_id,account_id))
        return {'retired':True,'pending_jobs':0}

    def prune_workers(self,account_id,*,keep_slots,process_stopped=False):
        """Remove obsolete registrations only; journals and job rows survive.

        process_stopped is an internal proof from the managed supervisor, never
        an HTTP field. Without it a fresh heartbeat is retained.
        """
        account_id=_identifier(account_id,account=True)
        if type(keep_slots) is not int or not 0<=keep_slots<=8 or type(process_stopped) is not bool:raise ValueError('Invalid node cleanup policy.')
        cutoff=self.clock()-self.heartbeat_timeout
        query="""SELECT worker_id FROM workers WHERE account_id=? AND
            (?=0 OR slot_index>?) AND (? OR last_seen<=?)"""
        params=(account_id,keep_slots,keep_slots,process_stopped,cutoff)
        with self._connection() as db:
            if not db.execute(query,params).fetchone():return {'removed':[],'preserved':[]}
        removed=[];preserved=[]
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute(query,params).fetchall():
                worker_id=row['worker_id']
                if db.execute("SELECT 1 FROM jobs WHERE worker_id=? AND status IN ('claimed','submitted','uncertain') LIMIT 1",(worker_id,)).fetchone():
                    preserved.append(worker_id);continue
                db.execute('DELETE FROM workers WHERE worker_id=? AND account_id=?',(worker_id,account_id));removed.append(worker_id)
        return {'removed':removed,'preserved':preserved}

    def resolve_unactivated_uncertain(self,job_id):
        """Repair only old v5+ records with the queue's durable no-click proof."""
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM jobs WHERE id=?',(_uuid(job_id),)).fetchone()
            if row is None:raise BridgeError('Job not found.',status=404)
            if row['status']=='uncertain' and (row['claim_protocol_version'] or 0)>=5 and row['activation_started_at'] is None and row['submit_id'] is None and row['confirmed_at'] is None:
                db.execute("UPDATE jobs SET status='failed',error_code='activation_not_committed',error='The browser activation was never committed; no generation was authorized.',updated_at=? WHERE id=?",(self.clock(),row['id']))
            return self._public(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())

    def expire_claims(self):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');self._expire(db,self.clock())

    def verify_claim(self,job_id,worker_id,claim_token):
        job_id=_uuid(job_id);worker_id=_identifier(worker_id,account=False)
        with self._connection() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if row is None:raise BridgeError('Job not found.',status=404)
            if row['worker_id']!=worker_id or not isinstance(claim_token,str) or not row['claim_hash'] or not hmac.compare_digest(row['claim_hash'],hashlib.sha256(claim_token.encode()).hexdigest()):
                raise BridgeError('Claim ownership mismatch.',status=403)
            return self._public(row)

    def credit_preflight(self,job_id,worker_id,claim_token,quote,balance,decision=None):
        """Durable, owned proof that a credit rejection occurred before activation."""
        from .credit_quote import quote_request_fingerprint
        job=self.verify_claim(job_id,worker_id,claim_token)
        required=quote.get('required_credits');observed=quote.get('observed_at')
        valid=lambda x:isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and 0<=x<=1_000_000_000
        if not valid(required) or not valid(balance) or not isinstance(observed,(int,float)) or isinstance(observed,bool) or not math.isfinite(observed):
            raise ValueError('Exact finite credit quote and balance are required.')
        if quote.get('source')!='native_page' or quote.get('model')!=job['request']['model'] or quote.get('request_fingerprint')!=quote_request_fingerprint(job['request']):
            raise ValueError('Credit quote does not match the claimed request.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');now=self.clock();self._expire(db,now)
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
            previous=json.loads(row['credit_preflight_json']) if row['credit_preflight_json'] else None
            if row['status']=='failed' and previous and previous.get('allowed') is False:return self._public(row)
            if row['status']!='claimed' or row['submit_id'] or row['activation_started_at'] is not None or row['claim_expires_at']<=now:
                raise BridgeError('Credit preflight is only allowed for an active unsubmitted claim.')
            if not -5<=now-observed<=60:raise ValueError('Credit quote is stale; read the current page price again.')
            if previous and previous.get('observed_at')==observed and previous.get('request_fingerprint')==quote['request_fingerprint'] and previous.get('required_credits')==required:
                return self._public(row)
            proof={'allowed':balance>=required,'required_credits':float(required),'balance':float(balance),
                   'source':'native_page','observed_at':observed,'checked_at':now,'account_id':row['account_id'],
                   'request_fingerprint':quote['request_fingerprint'],'generation_not_activated':True}
            if decision is not None:
                if not isinstance(decision,dict) or not isinstance(decision.get('allowed'),bool) or decision.get('required_credits')!=required or decision.get('balance')!=balance or decision.get('account_id')!=row['account_id']:
                    raise ValueError('Credit hold decision does not match this claim.')
                proof.update({key:decision[key] for key in ('allowed','reason','held_credits','available_credits','min_credit_balance') if key in decision})
            status='claimed' if proof['allowed'] else 'failed'
            error=None if proof['allowed'] else 'Credits or account threshold do not allow this request; no generation request was submitted.'
            code=None if proof['allowed'] else proof.get('reason','insufficient_credits')
            db.execute('UPDATE jobs SET credit_preflight_json=?,status=?,error=?,error_code=?,updated_at=? WHERE id=?',(_json(proof),status,error,code,now,row['id']))
            return self._public(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())

    def activate(self,job_id,worker_id,claim_token):
        job=self.verify_claim(job_id,worker_id,claim_token)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');now=self.clock();self._expire(db,now)
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
            # Recovery can rotate the token after verify_claim returns. Recheck
            # ownership under the same write lock that installs activation.
            if row is None:raise BridgeError('Job not found.',status=404)
            if row['worker_id']!=worker_id or not row['claim_hash'] or not hmac.compare_digest(row['claim_hash'],hashlib.sha256(claim_token.encode()).hexdigest()):
                raise BridgeError('Claim ownership mismatch.',status=403)
            if row['status']!='claimed' or row['submit_id']:raise BridgeError('Claim cannot be activated; inspect the original task.')
            if row['activation_started_at'] is not None:return {'can_activate':True,'activation_started_at':row['activation_started_at']}
            proof=json.loads(row['credit_preflight_json'] or '{}')
            if proof.get('allowed') is not True or now-proof.get('checked_at',0)>60:raise BridgeError('A fresh approved credit preflight is required before activation.')
            db.execute('UPDATE jobs SET activation_started_at=?,updated_at=? WHERE id=?',(now,now,row['id']))
            return {'can_activate':True,'activation_started_at':now}

    def start_preparation(self,job_id,worker_id,claim_token):
        """Start the v6 preparation budget after the worker holds its UI lock."""
        job=self.verify_claim(job_id,worker_id,claim_token)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');now=self.clock();self._expire(db,now)
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if row['worker_id']!=worker_id or not row['claim_hash'] or not hmac.compare_digest(row['claim_hash'],hashlib.sha256(claim_token.encode()).hexdigest()):raise BridgeError('Claim owner changed.',status=403)
            if row['status']!='claimed' or (row['claim_protocol_version'] or 0)<6 or row['activation_started_at'] is not None or row['submit_id']:raise BridgeError('Only an unactivated v6 claim may prepare.')
            started=row['preparation_started_at'] if row['preparation_started_at'] is not None else now
            db.execute('UPDATE jobs SET preparation_started_at=?,updated_at=? WHERE id=?',(started,now,job['id']))
            return {'can_prepare':True,'preparation_started_at':started}

    def bind_verified_original(self,job_id,submit_id,api_origin):
        """Operator-selected ID, already matched to upstream account/draft/time."""
        job_id=_uuid(job_id);submit_id=_uuid(submit_id)
        if api_origin not in API_ORIGINS:raise ValueError('Invalid API origin.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if row is None:raise BridgeError('Job not found.',status=404)
            if row['status']!='uncertain':raise BridgeError('Only an uncertain original job may be bound.')
            if row['submit_id'] not in (None,submit_id):raise BridgeError('Cannot replace an existing upstream ID.')
            try:db.execute('UPDATE jobs SET submit_id=?,api_origin=?,updated_at=? WHERE id=?',(submit_id,api_origin,self.clock(),job_id))
            except sqlite3.IntegrityError:raise BridgeError('This upstream ID already belongs to another task.') from None
            return self._public(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())

    def fail_unactivated(self,job_id,message='Dispatch stopped before activation.'):
        """Fence a possibly enqueued dispatch before freeing its parent quota."""
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM jobs WHERE id=?',(_uuid(job_id),)).fetchone()
            if row is None:
                db.execute('INSERT OR IGNORE INTO dispatch_fences(job_id,created_at) VALUES(?,?)',(_uuid(job_id),self.clock()))
                return True
            if row['status'] in ('completed','submitted','uncertain') or row['submit_id'] or row['activation_started_at'] is not None:return False
            if row['status']=='claimed' and (row['claim_protocol_version'] or 0)<5:return False
            db.execute("UPDATE jobs SET status='failed',error_code='dispatch_failed',error=?,updated_at=? WHERE id=?",(message,self.clock(),job_id))
            return True

    def deny_activation(self,job_id,worker_id,claim_token,error_code,message):
        """Atomically fence a native attempt before returning a negative proof.

        A request losing its response can ask again: an owned v5 failed job
        with no activation/acceptance markers remains safely fenced. A prior
        activation, uncertain state, legacy claim or changed owner never gains
        this proof merely because a later policy check rejected activation.
        """
        job_id=_uuid(job_id);worker_id=_identifier(worker_id)
        if not isinstance(claim_token,str) or not 1<=len(claim_token)<=1024:raise ValueError('claim_token is required.')
        if not isinstance(error_code,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',error_code):raise ValueError('Invalid activation error code.')
        if not isinstance(message,str) or not message.strip() or len(message)>1000:raise ValueError('Activation message must be nonempty and at most 1000 characters.')
        clean_message=str(sanitize(message))[:1000]
        supplied_hash=hashlib.sha256(claim_token.encode()).hexdigest()
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');now=self.clock()
            if isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now):raise ValueError('Invalid activation clock.')
            self._expire(db,now)
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if row is None:raise BridgeError('Job not found.',status=404)
            if row['worker_id']!=worker_id or not row['claim_hash'] or not hmac.compare_digest(row['claim_hash'],supplied_hash):
                raise BridgeError('Claim ownership mismatch.',status=403)
            if (row['claim_protocol_version'] or 0)<5 or row['status'] not in ('claimed','failed') or row['submit_id'] is not None or row['activation_started_at'] is not None or row['confirmed_at'] is not None:
                raise BridgeError('Activation outcome cannot be safely denied; preserve the original attempt.',status=409)
            if row['status']=='claimed':
                db.execute("UPDATE jobs SET status='failed',error_code=?,error=?,updated_at=? WHERE id=?",(error_code,clean_message,now,job_id))
            else:
                error_code=row['error_code'] or error_code;clean_message=row['error'] or clean_message
            return {'can_activate':False,'generation_not_activated':True,'job_id':job_id,'error_code':error_code,'message':clean_message}

    def deadline(self,job_id,error_code,message):
        if error_code not in ('queue_timeout','preparation_timeout','submission_unconfirmed','generation_timeout','task_timeout'):raise ValueError('Unknown task deadline.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT * FROM jobs WHERE id=?',(_uuid(job_id),)).fetchone()
            if row is None:raise BridgeError('Job not found.',status=404)
            if row['status'] in ('completed','failed'):return self._public(row)
            safe=row['status']=='queued' or row['status']=='claimed' and (row['claim_protocol_version'] or 0)>=5 and row['activation_started_at'] is None and not row['submit_id']
            state='failed' if safe else 'uncertain'
            if row['error_code']!=error_code or row['status']!=state:
                db.execute('UPDATE jobs SET status=?,error_code=?,error=?,updated_at=? WHERE id=?',(state,error_code,message,self.clock(),row['id']))
            return self._public(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())

    def telemetry(self,job_id,worker_id,claim_token,values):
        job=self.verify_claim(job_id,worker_id,claim_token)
        if job['status'] in ('completed','failed'):return job
        clean=sanitize(values)
        if len(_json(clean).encode())>100_000:raise ValueError('Telemetry too large.')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE');now=self.clock();self._expire(db,now)
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
            current=self._public(row)
            if current['status'] in ('completed','failed'):return current
            previous=current.get('telemetry') or {}
            if previous.get('observed_at',0)>clean.get('observed_at',0):return current
            merged=dict(previous,**clean);fresh=clean.get('observed_at',0)>previous.get('observed_at',0)
            db.execute("UPDATE jobs SET telemetry_json=?,claim_expires_at=CASE WHEN status='claimed' AND claim_expires_at>? AND ? THEN ? ELSE claim_expires_at END WHERE id=?",
                       (_json(merged),now,fresh,now+self.claim_timeout,job['id']))
        return self.get(job['id'])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class QueueInput(StrictModel):
    account_id: str = Field(default='default', pattern=ACCOUNT_PATTERN)
    request: dict


class WorkerInput(StrictModel):
    account_id: str = Field(default='default', pattern=ACCOUNT_PATTERN)
    worker_id: str = Field(pattern=WORKER_PATTERN)


class ClaimInput(WorkerInput):
    claim_request_id:str|None=Field(default=None,pattern=r'^[0-9a-fA-F-]{36}$')


class HeartbeatInput(WorkerInput):
    ready: StrictBool
    page_url: str = Field(max_length=2048)
    protocol_version:int=Field(default=3,ge=1,le=8)
    entry_ready:StrictBool|None=None
    entry_checked_at:float|None=None
    entry_parked:StrictBool|None=None
    wakeable:StrictBool|None=None
    driver:str='extension'
    reason:str=Field(default='',max_length=300)
    slot_index:int|None=Field(default=None,ge=1,le=8,strict=True)
    slot_count:int|None=Field(default=None,ge=1,le=8,strict=True)
    owner_id:str|None=Field(default=None,pattern=WORKER_PATTERN)


class ReferenceBindingInput(StrictModel):
    kind: Literal['image','video']
    resource_id: str = Field(min_length=1,max_length=1024,pattern=RESOURCE_ID_PATTERN,strict=True)


class ReportInput(StrictModel):
    worker_id: str = Field(pattern=WORKER_PATTERN)
    claim_token: str = Field(min_length=1, max_length=128)
    status: str
    submit_id: str | None = None
    api_origin: str | None = None
    error: str | None = Field(default=None, max_length=1000)
    error_code:str|None=Field(default=None,pattern=r'^[a-z][a-z0-9_]{0,79}$')
    generation_not_activated:StrictBool|None=None
    upstream_ret:str|None=Field(default=None,min_length=1,max_length=40,pattern=r'^[A-Za-z0-9_-]+$')
    upstream_error:dict|None=Field(default=None,max_length=16)
    submission_window_closed:StrictBool|None=None
    reference_bindings: dict[Annotated[str,Field(max_length=64,pattern=ASSET_PATTERN)],ReferenceBindingInput] | None = Field(default=None,min_length=1,max_length=50)


class TelemetryInput(StrictModel):
    worker_id: str = Field(pattern=WORKER_PATTERN)
    claim_token: str = Field(min_length=1,max_length=128)
    stage: str = Field(min_length=1,max_length=80)
    observed_at: float
    progress: int | None = Field(default=None,ge=0,le=99)
    progress_source: str | None = None
    conversation_id: str | None = Field(default=None,max_length=128)
    session_path: str | None = Field(default=None,max_length=300)
    detail: dict | None = None


class ClaimIdentity(StrictModel):
    worker_id: str = Field(pattern=WORKER_PATTERN)
    claim_token: str = Field(min_length=1,max_length=128)


class CreditPreflightInput(ClaimIdentity):
    generation_not_activated: StrictBool
    required_credits: float = Field(ge=0,le=1_000_000_000,allow_inf_nan=False,strict=True)
    source: Literal['native_page']
    observed_at: float = Field(allow_inf_nan=False,strict=True)
    model: str = Field(max_length=120)
    request_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


def create_chrome_bridge_router(authorize: Callable, *, store: BridgeStore | None = None,
                                pairing_key_loader: Callable[[], str] | None = None,
                                extension_id_loader: Callable[[], str] | None = None,
                                telemetry_sink: Callable | None = None, credit_checker: Callable | None = None,
                                account_limits_loader: Callable | None = None,activation_checker: Callable | None = None,
                                preparation_checker: Callable | None = None,submission_turn_checker: Callable | None = None,
                                report_sink: Callable | None = None) -> APIRouter:
    """Create routes without importing server.py or changing its authorization.

    Extensions use X-Chrome-Bridge-Key. CORS accepts only the exact configured
    chrome-extension://CHROME_BRIDGE_EXTENSION_ID, without credentials/wildcards.
    Requests without Origin still require the bridge key (e.g. extension worker
    implementations that omit Origin); ordinary websites are always rejected.
    """
    store = store or BridgeStore()
    pairing_key_loader = pairing_key_loader or (lambda: load_env().get('CHROME_BRIDGE_KEY', ''))
    extension_id_loader = extension_id_loader or (lambda: load_env().get('CHROME_BRIDGE_EXTENSION_ID', ''))

    def cors_origin(request: Request) -> str | None:
        value = extension_id_loader().strip()
        origin = request.headers.get('origin')
        if EXTENSION_PATTERN.fullmatch(value) and origin == f'chrome-extension://{value}':
            return origin
        return None

    class ChromeRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def handler(request: Request):
                from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
                from fastapi.exceptions import RequestValidationError
                try:
                    response = await original(request)
                except HTTPException as error:
                    response = await http_exception_handler(request, error)
                except RequestValidationError as error:
                    # Never echo claim tokens or non-finite input values in errors.
                    response = JSONResponse({'detail':[{k:item[k] for k in ('loc','type','msg') if k in item}
                                                       for item in error.errors()]},status_code=422)
                origin = cors_origin(request)
                if origin:
                    response.headers['Access-Control-Allow-Origin'] = origin
                    response.headers['Vary'] = 'Origin'
                return response
            return handler

    router = APIRouter(prefix=PREFIX, tags=['Chrome bridge'], route_class=ChromeRoute)
    # Exposed for root integration (HTTP polling/reconciliation); not an endpoint.
    router.bridge_store = store

    def bridge_auth(request: Request):
        origin = request.headers.get('origin')
        if origin and not cors_origin(request):
            raise HTTPException(403, 'Origin is not the configured Chrome extension.')
        expected = pairing_key_loader()
        if not expected or len(expected) < 24:
            raise HTTPException(503, 'Configure a random CHROME_BRIDGE_KEY of at least 24 characters.')
        supplied = request.headers.get('x-chrome-bridge-key', '')
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, 'Invalid Chrome bridge pairing key.')

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BridgeError as error:
            raise HTTPException(error.status, str(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    def limits(account_id):
        if not account_limits_loader:return {'enabled':True,'concurrency_limit':1}
        try:return account_limits_loader(account_id)
        except Exception:return {'enabled':False,'concurrency_limit':0}

    @router.options('/{path:path}', include_in_schema=False)
    def preflight(path: str, request: Request):
        origin = cors_origin(request)
        if not origin:
            raise HTTPException(403, 'Origin is not the configured Chrome extension.')
        requested = {item.strip().lower() for item in request.headers.get('access-control-request-headers', '').split(',') if item.strip()}
        if requested - {'content-type', 'x-chrome-bridge-key','x-chrome-claim-token','x-chrome-worker-id'}:
            raise HTTPException(403, 'Unsupported bridge request headers.')
        if request.headers.get('access-control-request-method', 'POST').upper() not in {'POST', 'GET'}:
            raise HTTPException(403, 'Unsupported bridge request method.')
        return Response(status_code=204, headers={'Access-Control-Allow-Origin': origin,
                    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, X-Chrome-Bridge-Key, X-Chrome-Claim-Token, X-Chrome-Worker-Id',
                    'Access-Control-Max-Age': '600', 'Vary': 'Origin'})

    @router.post('/jobs', status_code=202)
    def enqueue(body: QueueInput, _=Depends(authorize)):
        return call(store.enqueue, body.request, account_id=body.account_id)

    @router.get('/jobs')
    def jobs(account_id: str | None = None, limit: int = 50, _=Depends(authorize)):
        return {'jobs': call(store.list_jobs, account_id=account_id, limit=limit)}

    @router.get('/jobs/{job_id}')
    def job(job_id: str, _=Depends(authorize)):
        return call(store.get, job_id)

    @router.get('/accounts')
    def accounts(_=Depends(authorize)):
        return {'accounts': call(store.accounts), 'capabilities': dict(CAPABILITIES)}

    @router.post('/heartbeat')
    def heartbeat(body: HeartbeatInput, _=Depends(bridge_auth)):
        policy=limits(body.account_id);data=body.model_dump();data['ready']=body.ready and policy.get('enabled',False)
        if body.protocol_version>=8:data['wakeable']=bool(body.wakeable and policy.get('enabled',False))
        if policy.get('deleted'):raise HTTPException(410,'This account has been deleted; its browser nodes cannot reconnect.')
        data['retire_above']=policy.get('concurrency_limit',1)
        if body.protocol_version>=5 and body.slot_index and body.slot_index>policy.get('concurrency_limit',0):data['ready']=False;data['wakeable']=False
        result=call(store.heartbeat,**data)
        return result|{'concurrency_limit':policy.get('concurrency_limit',1),'scheduling_paused':policy.get('scheduling_paused',False)}

    @router.post('/workers/retire')
    def retire_worker(body:WorkerInput,_=Depends(bridge_auth)):
        policy=limits(body.account_id)
        if policy.get('deleted'):return {'retired':True,'pending_jobs':0}
        return call(store.retire_worker,body.account_id,body.worker_id,keep_slots=policy.get('concurrency_limit',1))

    @router.post('/claim')
    def claim(body: ClaimInput, _=Depends(bridge_auth)):
        policy=limits(body.account_id)
        if not policy.get('enabled',False):return {'job':None,'reason':'account_unavailable'}
        return {'job': call(store.claim, body.account_id, body.worker_id,capacity=policy.get('concurrency_limit',1),request_id=body.claim_request_id)}

    @router.post('/slots/recover')
    def recover_slot(body:ClaimInput,_=Depends(bridge_auth)):
        result=call(store.recover_slot,body.account_id,body.worker_id,body.claim_request_id)
        if result.get('job') and telemetry_sink:
            telemetry_sink(result['job'],{'stage':'claim_recovered','observed_at':time.time(),'detail':{'original_job_id':result['job']['id'],'generation_not_activated':True}})
        return result

    @router.post('/jobs/{job_id}/report')
    async def report(job_id: str, body: ReportInput, _=Depends(bridge_auth)):
        result=await asyncio.to_thread(call,store.report, job_id, body.worker_id, body.claim_token, status=body.status,
                    submit_id=body.submit_id, error=body.error, api_origin=body.api_origin,
                    reference_bindings={key:value.model_dump() for key,value in body.reference_bindings.items()} if body.reference_bindings is not None else None,
                    error_code=body.error_code,generation_not_activated=body.generation_not_activated,upstream_ret=body.upstream_ret,upstream_error=body.upstream_error,submission_window_closed=body.submission_window_closed)
        if report_sink:await report_sink(result)
        return result

    @router.post('/jobs/{job_id}/telemetry')
    def telemetry(job_id:str,body:TelemetryInput,_=Depends(bridge_auth)):
        if body.progress_source not in (None,'page'):raise HTTPException(422,'Unknown progress source.')
        if body.progress is not None and body.progress_source!='page':raise HTTPException(422,'Page progress requires a source.')
        if abs(time.time()-body.observed_at)>3600:raise HTTPException(422,'Stale observation timestamp.')
        values=body.model_dump(exclude_none=True,exclude={'worker_id','claim_token'})
        job=call(store.telemetry,job_id,body.worker_id,body.claim_token,values)
        if telemetry_sink:telemetry_sink(job,values)
        return {'ok':True,'status':job['status']}

    @router.post('/jobs/{job_id}/status')
    def worker_status(job_id:str,body:ClaimIdentity,_=Depends(bridge_auth)):
        job=call(store.verify_claim,job_id,body.worker_id,body.claim_token)
        return {key:job.get(key) for key in ('status','submit_id','reference_bindings','error_code','activation_started_at','credit_preflight','slot_released','submission_window_closed_at')}

    @router.post('/jobs/{job_id}/activate')
    async def activate(job_id:str,body:ClaimIdentity,_=Depends(bridge_auth)):
        job=await asyncio.to_thread(call,store.verify_claim,job_id,body.worker_id,body.claim_token)
        if not activation_checker:raise HTTPException(503,'Central activation checks are unavailable.')
        try:return await activation_checker(job,body.worker_id,body.claim_token)
        except BridgeError as error:raise HTTPException(error.status,str(error)) from error

    @router.post('/jobs/{job_id}/prepare')
    async def prepare(job_id:str,body:ClaimIdentity,_=Depends(bridge_auth)):
        job=await asyncio.to_thread(call,store.verify_claim,job_id,body.worker_id,body.claim_token)
        if not preparation_checker:raise HTTPException(503,'Central preparation checks are unavailable.')
        try:return await preparation_checker(job,body.worker_id,body.claim_token)
        except BridgeError as error:raise HTTPException(error.status,str(error)) from error

    @router.post('/jobs/{job_id}/submission-turn')
    async def submission_turn(job_id:str,body:ClaimIdentity,_=Depends(bridge_auth)):
        job=await asyncio.to_thread(call,store.verify_claim,job_id,body.worker_id,body.claim_token)
        if not submission_turn_checker:raise HTTPException(503,'Central pacing is unavailable.')
        try:return await submission_turn_checker(job,body.worker_id,body.claim_token)
        except BridgeError as error:raise HTTPException(error.status,str(error)) from error

    @router.post('/jobs/{job_id}/preflight')
    async def credit_check(job_id:str,body:CreditPreflightInput,_=Depends(bridge_auth)):
        if not body.generation_not_activated:raise HTTPException(409,'Cannot change accounts after generation activation.')
        job=await asyncio.to_thread(call,store.verify_claim,job_id,body.worker_id,body.claim_token)
        if job.get('credit_preflight',{} ) and job['status']=='failed' and job['credit_preflight'].get('allowed') is False:
            return job['credit_preflight']
        if job['status']!='claimed' or job.get('submit_id'):raise HTTPException(409,'Task is not an unsubmitted active claim.')
        if not credit_checker:raise HTTPException(503,'Credit preflight is unavailable; no generation should be submitted.')
        quote=body.model_dump(exclude={'worker_id','claim_token','generation_not_activated'})
        try:return await credit_checker(job,body.worker_id,body.claim_token,quote)
        except BridgeError as error:
            detail={'code':'proxy_error','message':str(error)} if getattr(error,'code',None)=='proxy_error' else str(error)
            raise HTTPException(error.status,detail) from error
        except (ValueError,TypeError) as error:raise HTTPException(422,str(error)) from error
        except Exception:raise HTTPException(503,'Current credit balance could not be verified; no generation was submitted.') from None

    @router.get('/jobs/{job_id}/assets/{asset_id}')
    def asset(job_id:str,asset_id:str,request:Request,_=Depends(bridge_auth)):
        from .video_inputs import asset_path_for_task
        job=call(store.verify_claim,job_id,request.headers.get('x-chrome-worker-id',''),request.headers.get('x-chrome-claim-token',''))
        if job['status']!='claimed':raise HTTPException(409,'Assets are only available during the assigned submission.')
        match=next((a for a in job['request'].get('references',[]) if a['asset_id']==asset_id),None)
        if not match:raise HTTPException(404,'Asset not found.')
        path=asset_path_for_task(ROOT/'outputs'/'platform-inputs',job['request']['asset_owner_task_id'],match)
        if not path.is_file():raise HTTPException(404,'Asset content unavailable.')
        if path.stat().st_size!=match['size_bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=match['sha256']:raise HTTPException(409,'Asset integrity mismatch.')
        return FileResponse(path,media_type=match['mime_type'],filename=match['name'],headers={'Cache-Control':'no-store'})

    return router
