"""Persistent account, API-key and task control plane (SQLite or PostgreSQL).

Credentials are encrypted at rest; API/session tokens are retained only as hashes.
Scheduling is serialized with a database transaction, including across processes.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Callable
import uuid

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (BigInteger, Boolean, Column, Float, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint, create_engine, delete, event,
                        func, insert, select, update, inspect as sa_inspect)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from .config import ROOT, Settings
from .audit import sanitize
from .migrations import schema_migration_lock
from .proxy_store import ProxyStoreMixin,register_proxy_tables


class PlatformError(Exception):
    def __init__(self, message: str, *, code: str = 'invalid_request', status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


metadata = MetaData()
proxies,proxy_events,proxy_changes=register_proxy_tables(metadata)
accounts = Table('platform_accounts', metadata,
    Column('id', String(64), primary_key=True), Column('name', String(120), nullable=False),
    Column('region', String(8), nullable=False), Column('enabled', Boolean, nullable=False),
    Column('priority', Integer, nullable=False), Column('max_concurrent', Integer, nullable=False),
    Column('max_inflight', Integer, nullable=False, server_default='1'),
    Column('min_credit_balance', Float, nullable=False, server_default='0'),
    Column('capabilities_json', Text, nullable=False), Column('secret_encrypted', Text),
    Column('status', String(40), nullable=False), Column('balance', Float), Column('deleted_at', Float),
    Column('last_checked_at', Float), Column('last_used_at', Float),
    Column('identity_digest',String(64)),Column('browser_profile',String(128)),Column('last_login_at',Float),
    Column('proxy_id',String(64),ForeignKey('platform_proxies.id')),Column('proxy_bound_at',Float),Column('proxy_exit_ip_at_bind',String(64)),
    Column('browser_timezone',String(80)),Column('browser_locale',String(20)),Column('proxy_migration_error',String(80)),
    Column('created_at', Float, nullable=False), Column('updated_at', Float, nullable=False))
api_keys = Table('platform_api_keys', metadata,
    Column('id', String(64), primary_key=True), Column('name', String(120), nullable=False),
    Column('prefix', String(32), nullable=False), Column('key_hash', String(64), unique=True, nullable=False),
    Column('enabled', Boolean, nullable=False), Column('expires_at', Float), Column('deleted_at', Float),
    Column('allowed_models_json', Text, nullable=False), Column('account_ids_json', Text, nullable=False),
    Column('max_concurrent', Integer, nullable=False), Column('request_limit', Integer),
    Column('used_requests', Integer, nullable=False), Column('rate_limit_per_minute', Integer, nullable=False),
    Column('last_used_at', Float), Column('created_at', Float, nullable=False))
tasks = Table('platform_tasks', metadata,
    Column('id', String(64), primary_key=True), Column('kind', String(16), nullable=False),
    Column('model', String(120), nullable=False), Column('status', String(32), nullable=False),
    Column('account_id', String(64), ForeignKey('platform_accounts.id'), nullable=False),
    Column('key_id', String(64), ForeignKey('platform_api_keys.id'), nullable=False),
    Column('request_json', Text, nullable=False), Column('request_hash', String(64), nullable=False),
    Column('idempotency_key', String(200)), Column('dispatch_claimed', Boolean, nullable=False),
    Column('bridge_job_id', String(64)), Column('submit_id', String(128)),
    Column('result_json', Text), Column('error_json', Text), Column('progress', Integer, nullable=False),
    Column('output_path', Text), Column('created_at', Float, nullable=False),
    Column('updated_at', Float, nullable=False), Column('completed_at', Float),
    Column('request_detail_json', Text), Column('progress_details_json', Text),
    Column('conversation_id', String(128)), Column('billing_status', String(40)),
    Column('billing_details_json', Text), Column('usage_json', Text), Column('usage_details_json', Text),
    UniqueConstraint('key_id', 'idempotency_key', name='uq_platform_key_idempotency'))
admins = Table('platform_admins', metadata,
    Column('username', String(80), primary_key=True), Column('password_hash', Text, nullable=False),
    Column('created_at', Float, nullable=False))
sessions = Table('platform_admin_sessions', metadata,
    Column('token_hash', String(64), primary_key=True), Column('username', String(80), nullable=False),
    Column('csrf_token', String(128), nullable=False), Column('expires_at', Float, nullable=False))
login_attempts = Table('platform_login_attempts', metadata,
    Column('id', String(64), primary_key=True), Column('count', Integer, nullable=False),
    Column('window_start', Float, nullable=False))
options = Table('platform_options', metadata,
    Column('name', String(80), primary_key=True), Column('value_json', Text, nullable=False))
schedule_guard = Table('platform_schedule_guard', metadata, Column('id', Integer, primary_key=True))
account_schedule = Table('platform_account_schedule', metadata,
    Column('account_id', String(64), ForeignKey('platform_accounts.id'), primary_key=True),
    Column('last_sequence', BigInteger, nullable=False))
task_events = Table('platform_task_events',metadata,
    Column('id',String(64),primary_key=True),Column('task_id',String(64),ForeignKey('platform_tasks.id'),nullable=False,index=True),
    Column('created_at',Float,nullable=False),Column('stage',String(80),nullable=False),Column('detail_json',Text,nullable=False))
account_model_health = Table('platform_account_model_health',metadata,
    Column('account_id',String(64),ForeignKey('platform_accounts.id'),primary_key=True),
    Column('model',String(120),primary_key=True),Column('reason',String(80),nullable=False),
    Column('blocked_until',Float,nullable=False),Column('failure_count',Integer,nullable=False),
    Column('updated_at',Float,nullable=False))
credit_holds = Table('platform_credit_holds',metadata,
    Column('job_id',String(64),primary_key=True),Column('task_id',String(64),ForeignKey('platform_tasks.id'),nullable=False,index=True),
    Column('account_id',String(64),ForeignKey('platform_accounts.id'),nullable=False,index=True),
    Column('amount',Float,nullable=False),Column('request_fingerprint',String(64),nullable=False),
    Column('created_at',Float,nullable=False),Column('updated_at',Float,nullable=False),
    Column('released_at',Float),Column('release_reason',String(80)))
submission_turns = Table('platform_submission_turns',metadata,
    Column('job_id',String(64),primary_key=True),Column('task_id',String(64),ForeignKey('platform_tasks.id'),nullable=False),
    Column('account_id',String(64),ForeignKey('platform_accounts.id'),nullable=False),Column('egress_group',String(80),nullable=False),
    Column('granted_at',Float,nullable=False),Column('expires_at',Float,nullable=False),Column('activated_at',Float),Column('finished_at',Float),
    Column('ip_interval',Float,nullable=False),Column('account_interval',Float,nullable=False))
submission_gates = Table('platform_submission_gates',metadata,
    Column('scope',String(160),primary_key=True),Column('holder_job_id',String(64)),
    Column('next_allowed_at',Float,nullable=False),Column('updated_at',Float,nullable=False))
submission_risk_events = Table('platform_submission_risk_events',metadata,
    Column('event_id',String(128),primary_key=True),Column('account_id',String(64),ForeignKey('platform_accounts.id'),nullable=False),
    Column('model',String(120),nullable=False),Column('blocked_until',Float,nullable=False),Column('created_at',Float,nullable=False))

ACTIVE_STATES = ('queued', 'claimed', 'submitted', 'in_progress', 'uncertain')
TERMINAL_STATES = ('completed', 'failed', 'cancelled')
ACCOUNT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
SECRET_FIELDS = {'session_id', 'cookie', 'workspace_id', 'proxy', 'api_origin', 'ms_token', 'device_id', 'signing'}


def _background_query_only(row):
    """Only a bounded known-ID timeout may release browser/login ownership."""
    if row['status']!='uncertain' or not isinstance(row['submit_id'],str) or not row['submit_id'].strip():return False
    try:error=json.loads(row['error_json'] or '{}')
    except (ValueError,TypeError,RecursionError):return False
    return isinstance(error,dict) and error.get('code') in ('generation_timeout','task_timeout')


def _live_slot_capacity(entry):
    live=[w for w in entry.get('workers',[]) if w.get('online')]
    if not live:return 0
    if any(not isinstance(w.get('protocol_version'),int) or w['protocol_version']<5 for w in live):return 1
    slots={(w.get('owner_id'),w.get('slot_index')) for w in live
           if isinstance(w.get('owner_id'),str) and w['owner_id'] and type(w.get('slot_index')) is int and 1<=w['slot_index']<=8}
    return len(slots)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _string(value, name, maximum=120):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PlatformError(f'{name} must be a nonempty string of at most {maximum} characters.')
    return value.strip()


def _integer(value, name, minimum=1, maximum=10000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PlatformError(f'{name} must be an integer between {minimum} and {maximum}.')
    return value


def _strings(value, name):
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, str) or not x or len(x) > 120 for x in value):
        raise PlatformError(f'{name} must be a list of strings.')
    return list(dict.fromkeys(value))


def _time_value(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise PlatformError(f'{name} must be a positive Unix timestamp or null.')
    return float(value)


class PlatformStore(ProxyStoreMixin):
    def __init__(self, database_url: str | None = None, *, encryption_key: str | bytes | None = None,
                 key_file: Path | str | None = None, worker_status: Callable | None = None,
                 clock: Callable = time.time, account_capacity_limit: int = 8, account_inflight_limit: int = 100, account_proxy_required: bool = False):
        if type(account_proxy_required) is not bool:raise ValueError('account_proxy_required must be boolean.')
        self.account_proxy_required=account_proxy_required
        if type(account_capacity_limit) is not int or not 1<=account_capacity_limit<=8:
            raise ValueError('account_capacity_limit must be an integer between 1 and 8.')
        self.account_capacity_limit=account_capacity_limit
        if type(account_inflight_limit) is not int or not 1<=account_inflight_limit<=100:
            raise ValueError('account_inflight_limit must be an integer between 1 and 100.')
        self.account_inflight_limit=account_inflight_limit
        self.clock, self.worker_status = clock, worker_status
        self._lock = threading.RLock()
        self._limits_cache={}
        default_db = ROOT / 'outputs' / 'platform.db'
        default_db.parent.mkdir(parents=True, exist_ok=True)
        url = database_url or os.environ.get('DATABASE_URL') or 'sqlite:///' + default_db.as_posix()
        if url.startswith('postgres://'):
            url = 'postgresql+psycopg://' + url[len('postgres://'):]
        elif url.startswith('postgresql://'):
            url = 'postgresql+psycopg://' + url[len('postgresql://'):]
        kwargs = {'pool_pre_ping': True}
        if url.startswith('sqlite'):
            kwargs['connect_args'] = {'check_same_thread': False, 'timeout': 20}
            if ':memory:' in url:
                kwargs['poolclass'] = StaticPool
        self.engine = create_engine(url, **kwargs)
        self.storage_backend = self.engine.dialect.name
        if self.storage_backend not in ('sqlite', 'postgresql'):
            raise ValueError('DATABASE_URL must use SQLite or PostgreSQL.')
        if self.storage_backend == 'sqlite':
            @event.listens_for(self.engine, 'connect')
            def pragmas(connection, _):
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA busy_timeout=20000')
                connection.execute('PRAGMA journal_mode=WAL')
        key = encryption_key or os.environ.get('PLATFORM_ENCRYPTION_KEY')
        if not key:
            path = Path(key_file or os.environ.get('PLATFORM_KEY_FILE', default_db.parent / 'platform-encryption.key'))
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                try:
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as handle:
                        handle.write(Fernet.generate_key())
                except FileExistsError:
                    pass
            key = path.read_bytes().strip()
        if isinstance(key, str):
            key = key.encode()
        try:
            self._fernet = Fernet(key)
            self._profile_master_key=base64.urlsafe_b64decode(key)
        except (ValueError, TypeError) as exc:
            raise ValueError('PLATFORM_ENCRYPTION_KEY must be a Fernet key.') from exc
        with schema_migration_lock(self.engine):
            metadata.create_all(self.engine)
            existing={c['name'] for c in sa_inspect(self.engine).get_columns('platform_tasks')}
            additions={'request_detail_json':'TEXT','progress_details_json':'TEXT','conversation_id':'VARCHAR(128)',
                       'billing_status':'VARCHAR(40)','billing_details_json':'TEXT','usage_json':'TEXT','usage_details_json':'TEXT'}
            with self.engine.begin() as conn:
                for column,sql_type in additions.items():
                    if column not in existing:conn.exec_driver_sql(f'ALTER TABLE platform_tasks ADD COLUMN {column} {sql_type}')
            account_columns={c['name'] for c in sa_inspect(self.engine).get_columns('platform_accounts')}
            with self.engine.begin() as conn:
                for column,sql_type in {'identity_digest':'VARCHAR(64)','browser_profile':'VARCHAR(128)','last_login_at':'FLOAT','deleted_at':'FLOAT','min_credit_balance':'FLOAT NOT NULL DEFAULT 0',
                        'proxy_id':'VARCHAR(64)','proxy_bound_at':'FLOAT','proxy_exit_ip_at_bind':'VARCHAR(64)','browser_timezone':'VARCHAR(80)','browser_locale':'VARCHAR(20)','proxy_migration_error':'VARCHAR(80)'}.items():
                    if column not in account_columns:conn.exec_driver_sql(f'ALTER TABLE platform_accounts ADD COLUMN {column} {sql_type}')
                conn.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_account_identity ON platform_accounts(identity_digest)')
                if 'max_inflight' not in account_columns:
                    conn.exec_driver_sql('ALTER TABLE platform_accounts ADD COLUMN max_inflight INTEGER NOT NULL DEFAULT 1')
                    # Existing admission limits are retained once; later changes to
                    # browser tabs must not silently change upstream concurrency.
                    conn.exec_driver_sql('UPDATE platform_accounts SET max_inflight=max_concurrent')
                for name,table,columns in (
                    ('ix_tasks_account_status','platform_tasks','account_id,status'),
                    ('ix_tasks_key_status','platform_tasks','key_id,status'),
                    ('ix_tasks_key_created','platform_tasks','key_id,created_at'),
                    ('ix_tasks_status_id','platform_tasks','status,id'),
                    ('ix_tasks_bridge_job','platform_tasks','bridge_job_id'),
                    ('ix_events_task_created','platform_task_events','task_id,created_at,id'),
                    ('ix_holds_account_released','platform_credit_holds','account_id,released_at'),
                ):
                    conn.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})')
            key_columns={c['name'] for c in sa_inspect(self.engine).get_columns('platform_api_keys')}
            proxy_columns={c['name'] for c in sa_inspect(self.engine).get_columns('platform_proxies')}
            if 'ip_change_policy' not in proxy_columns:
                with self.engine.begin() as conn:conn.exec_driver_sql("ALTER TABLE platform_proxies ADD COLUMN ip_change_policy TEXT NOT NULL DEFAULT 'warn'")
            if 'deleted_at' not in key_columns:
                with self.engine.begin() as conn:conn.exec_driver_sql('ALTER TABLE platform_api_keys ADD COLUMN deleted_at FLOAT')
            try:
                with self.engine.begin() as conn:
                    if conn.execute(select(schedule_guard.c.id).where(schedule_guard.c.id == 1)).first() is None:
                        conn.execute(insert(schedule_guard).values(id=1))
            except IntegrityError:
                pass
            self._migrate_legacy_proxies()

    @contextmanager
    def transaction(self):
        with self._lock, self.engine.connect() as conn:
            try:
                if self.storage_backend == 'sqlite':
                    conn.exec_driver_sql('BEGIN IMMEDIATE')
                else:
                    conn.begin()
                    conn.execute(select(schedule_guard.c.id).where(schedule_guard.c.id == 1).with_for_update())
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _encrypt(self, values: dict) -> str:
        return self._fernet.encrypt(_json(values).encode()).decode()

    def _decrypt(self, value: str | None) -> dict:
        if not value:
            return {}
        try:
            return json.loads(self._fernet.decrypt(value.encode()))
        except (InvalidToken, ValueError) as exc:
            raise PlatformError('Account secrets cannot be decrypted with this server key.', code='credential_key_mismatch', status=503) from exc

    def _workers(self) -> dict:
        if self.worker_status is None:
            return {}
        try:
            records = self.worker_status()
            if isinstance(records, dict):
                records = records.get('accounts', records.get('items', []))
            return {item['account_id']: item for item in records}
        except Exception:
            raise PlatformError('Worker status is temporarily unavailable; retry the same request.',code='worker_status_unavailable',status=503) from None

    @staticmethod
    def _detached_workers(entry):
        live=[w for w in entry.get('workers',[]) if w.get('online')]
        return bool(live) and _live_slot_capacity(entry)>0 and all(
            type(w.get('protocol_version')) is int and w['protocol_version']>=6
            and w.get('driver')=='native' and type(w.get('slot_index')) is int
            and 1<=w['slot_index']<=8 and isinstance(w.get('owner_id'),str) and w['owner_id']
            for w in live)

    def _inflight_capacity(self,row,entry):
        limit=min(row['max_inflight'],self.account_inflight_limit)
        if self.worker_status is None:return limit
        if entry.get('blocked_by_uncertain'):return 0
        slots=min(row['max_concurrent'],self.account_capacity_limit,_live_slot_capacity(entry))
        if not slots:return 0
        return limit if self._detached_workers(entry) else min(limit,slots)

    def _account(self, row) -> dict:
        value = {key: row[key] for key in ('id', 'name', 'region', 'enabled', 'priority', 'max_concurrent', 'max_inflight', 'min_credit_balance',
                    'status', 'balance', 'last_checked_at', 'last_used_at', 'created_at', 'updated_at','deleted_at')}
        value['deleted']=row['deleted_at'] is not None
        value['capabilities'] = json.loads(row['capabilities_json'])
        credential = self._decrypt(row['secret_encrypted'])
        value['secret_configured'] = bool(credential.get('session_id') or credential.get('cookie'))
        value['browser_profile_saved']=bool(row['browser_profile'])
        value['last_login_at']=row['last_login_at']
        value['identity_hint']=row['identity_digest'][-8:] if row['identity_digest'] else None
        value['effective_max_concurrent']=min(row['max_concurrent'],self.account_capacity_limit)
        value['effective_max_inflight']=min(row['max_inflight'],self.account_inflight_limit)
        minimum=float(row['min_credit_balance'] or 0)
        below=minimum>0 and row['balance'] is not None and row['balance']<minimum
        value['scheduling_paused']=bool(below)
        value['scheduling_pause_reason']='below_credit_threshold' if below else None
        return value

    def list_accounts(self, *, include_deleted=False, _account_id=None) -> list[dict]:
        workers = self._workers()
        now=self.clock()
        with self.engine.connect() as conn:
            query=select(accounts).order_by(accounts.c.priority.desc(), accounts.c.created_at)
            if not include_deleted:query=query.where(accounts.c.deleted_at.is_(None))
            if _account_id is not None:query=query.where(accounts.c.id==_account_id)
            rows = conn.execute(query).mappings().all()
            active_query=select(tasks.c.account_id,tasks.c.status,tasks.c.submit_id,tasks.c.error_json).where(tasks.c.status.in_(ACTIVE_STATES))
            if _account_id is not None:active_query=active_query.where(tasks.c.account_id==_account_id)
            active_rows=conn.execute(active_query).mappings().all()
            counts=Counter(row['account_id'] for row in active_rows)
            blocking=Counter(row['account_id'] for row in active_rows if not _background_query_only(row))
            held=dict(conn.execute(select(credit_holds.c.account_id,func.sum(credit_holds.c.amount)).where(credit_holds.c.released_at.is_(None)).group_by(credit_holds.c.account_id)).all())
            risk_until=dict(conn.execute(select(submission_risk_events.c.account_id,func.max(submission_risk_events.c.blocked_until))
                .where(submission_risk_events.c.blocked_until>now).group_by(submission_risk_events.c.account_id)).all())
            egress_fields={r['id']:self.account_egress_public_fields(conn,r) for r in rows}
        result = []
        for row in rows:
            item = self._account(row)
            item.update(egress_fields[row['id']])
            if item.get('proxy_scheduling_error'):
                item['scheduling_paused']=True
                if not item['scheduling_pause_reason']:item['scheduling_pause_reason']=item['proxy_scheduling_error']
            item['risk_cooldown_until']=risk_until.get(row['id'])
            if item['risk_cooldown_until'] is not None:
                item['scheduling_paused']=True
                if not item['scheduling_pause_reason']:item['scheduling_pause_reason']='upstream_risk_cooldown'
            item['reserved_credits']=float(held.get(row['id'],0) or 0)
            item['available_credits']=max(float(row['balance'])-item['reserved_credits'],0) if row['balance'] is not None else None
            registered = workers.get(row['id'], {})
            item['current_slot_capacity']=min(item['effective_max_concurrent'],_live_slot_capacity(registered)) if self.worker_status is not None else item['effective_max_concurrent']
            item['current_inflight_capacity']=self._inflight_capacity(row,registered)
            item['detached_generation_supported']=self._detached_workers(registered) if self.worker_status is not None else False
            online = [w for w in registered.get('workers', []) if w.get('online')]
            item.update(online=bool(online), ready=any(w.get('ready') for w in online),
                        active_tasks=counts.get(row['id'], 0), blocking_tasks=blocking.get(row['id'],0), worker_count=len(online),
                        blocked_by_uncertain=bool(registered.get('blocked_by_uncertain')))
            result.append(item)
        return result

    def get_account(self, account_id: str, *, include_deleted=False) -> dict:
        for item in self.list_accounts(include_deleted=include_deleted,_account_id=account_id):
            if item['id'] == account_id:
                return item
        raise PlatformError('Account not found.', code='not_found', status=404)

    def get_account_limits(self,account_id):
        """Small heartbeat/claim policy; never decrypt other accounts."""
        now=self.clock()
        cached=self._limits_cache.get(account_id)
        if cached and 0<=now-cached[0]<2:return dict(cached[1])
        with self.engine.connect() as conn:
            row=conn.execute(select(accounts).where(accounts.c.id==account_id,accounts.c.deleted_at.is_(None))).mappings().first()
            if row is None:return {'enabled':False,'concurrency_limit':0,'scheduling_paused':True,'deleted':True}
            paused=bool(row['min_credit_balance']>0 and (row['balance'] is None or row['balance']<row['min_credit_balance']))
            paused|=bool(conn.execute(select(submission_risk_events.c.event_id).where(submission_risk_events.c.account_id==account_id,submission_risk_events.c.blocked_until>now).limit(1)).first())
            paused|=bool(self.proxy_scheduling_reason(conn,row))
            result={'enabled':bool(row['enabled']) and row['status'] not in ('deleted','deleting','disabled','login_pending','login_required','verification_required','expired','error','proxy_changing'),
                    'concurrency_limit':min(row['max_concurrent'],self.account_capacity_limit),'scheduling_paused':bool(paused)}
            self._limits_cache[account_id]=(now,result)
            return dict(result)

    def worker_registry_policies(self):
        """Minimal lifecycle policy; no session decryption or task details."""
        with self.engine.connect() as conn:
            rows=conn.execute(select(accounts.c.id,accounts.c.deleted_at,accounts.c.max_concurrent)).mappings().all()
        return [{'account_id':row['id'],'deleted':row['deleted_at'] is not None,
                 'keep_slots':min(row['max_concurrent'],self.account_capacity_limit)} for row in rows]

    def _account_values(self, data: dict, existing=None) -> dict:
        allowed = {'id', 'name', 'region', 'enabled', 'priority', 'max_concurrent', 'max_inflight', 'min_credit_balance', 'capabilities'} | SECRET_FIELDS
        if not isinstance(data, dict) or set(data) - allowed:
            raise PlatformError('Unknown account configuration fields.')
        values = {}
        if existing is None:
            account_id = data.get('id') or 'acct_' + secrets.token_hex(5)
            if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
                raise PlatformError('Account ID may contain letters, numbers, hyphens and underscores.')
            values.update(id=account_id, name=data.get('name', account_id), region='US', enabled=True,
                          priority=0, max_concurrent=1, max_inflight=1, min_credit_balance=0, capabilities_json='[]', status='unverified',
                          created_at=self.clock(), updated_at=self.clock())
        elif 'id' in data and data['id'] != existing['id']:
            raise PlatformError('Account ID cannot be changed.')
        if 'name' in data or existing is None:
            values['name'] = _string(data.get('name', values.get('name')), 'name')
        if 'region' in data:
            region = str(data['region']).upper()
            if region not in ('US', 'SG', 'HK', 'JP'):
                raise PlatformError('region must be US, SG, HK or JP.')
            values['region'] = region
        if 'enabled' in data:
            if not isinstance(data['enabled'], bool):
                raise PlatformError('enabled must be boolean.')
            values['enabled'] = data['enabled']
        if 'priority' in data:
            values['priority'] = _integer(data['priority'], 'priority', -1000, 1000)
        if 'max_concurrent' in data:
            values['max_concurrent'] = _integer(data['max_concurrent'], 'max_concurrent', 1, 8)
        if 'max_inflight' in data:
            values['max_inflight'] = _integer(data['max_inflight'], 'max_inflight', 1, 100)
        if 'min_credit_balance' in data:
            value=data['min_credit_balance']
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<=value<=1_000_000_000 or not math.isfinite(value):
                raise PlatformError('min_credit_balance must be a finite number between 0 and 1000000000.')
            values['min_credit_balance']=float(value)
        if 'capabilities' in data:
            values['capabilities_json'] = _json(_strings(data['capabilities'], 'capabilities'))
        secret_changes = {key: data[key] for key in SECRET_FIELDS if key in data}
        if secret_changes:
            current = self._decrypt(existing['secret_encrypted']) if existing else {}
            for key, value in secret_changes.items():
                if value is not None and not isinstance(value, str):
                    raise PlatformError(f'{key} must be a string.')
                if value and ('\r' in value or '\n' in value):
                    raise PlatformError(f'{key} must not contain newlines.')
                if value:
                    current[key] = value
                else:
                    current.pop(key, None)
            values['secret_encrypted'] = self._encrypt(current) if current else None
            values['status'] = 'unverified'
            values['balance'] = None
        values['updated_at'] = self.clock()
        return values

    def create_account(self, data: dict) -> dict:
        values = self._account_values(data)
        try:
            with self.transaction() as conn:
                conn.execute(insert(accounts).values(**values))
        except IntegrityError as exc:
            raise PlatformError('Account ID already exists.', code='conflict', status=409) from exc
        return self.get_account(values['id'])

    def update_account(self, account_id: str, data: dict) -> dict:
        with self.transaction() as conn:
            row = conn.execute(select(accounts).where(accounts.c.id == account_id)).mappings().first()
            if row is None or row['deleted_at'] is not None:
                raise PlatformError('Account not found.', code='not_found', status=404)
            if row['status']=='deleting':
                raise PlatformError('Account deletion is in progress.',code='account_deleting',status=409)
            if conn.execute(select(proxy_changes.c.id).where(proxy_changes.c.account_id==account_id,proxy_changes.c.state.in_(('pending','checking','restarting')))).first():
                raise PlatformError('Finish the existing proxy change before editing this account.',code='proxy_change_in_progress',status=409)
            if row['status']=='login_pending' and set(data)&SECRET_FIELDS:
                raise PlatformError('Finish or cancel the active login before editing credentials.',code='login_busy',status=409)
            conn.execute(update(accounts).where(accounts.c.id == account_id).values(**self._account_values(data, row)))
        self._limits_cache.pop(account_id,None)
        return self.get_account(account_id)

    def _account_delete_ready(self,conn,row,workers):
        if conn.execute(select(proxy_changes.c.id).where(proxy_changes.c.account_id==row['id'],proxy_changes.c.state.in_(('pending','checking','restarting')))).first():
            raise PlatformError('Finish the existing proxy change before deleting this account.',code='proxy_change_in_progress',status=409)
        if row['status']=='login_pending':
            raise PlatformError('Finish or cancel the active login before deleting this account.',code='login_busy',status=409)
        active=conn.execute(select(func.count()).select_from(tasks).where(tasks.c.account_id==row['id'],tasks.c.status.in_(ACTIVE_STATES))).scalar_one()
        jobs=(workers.get(row['id']) or {}).get('jobs') or {}
        if active or any(jobs.get(state,0) for state in ('queued','claimed','submitted','uncertain')):
            raise PlatformError('This account has unfinished tasks; finish or reconcile them before deletion.',code='account_busy',status=409)

    def begin_account_delete(self,account_id):
        """Stop new reservations before awaiting the graceful worker shutdown."""
        workers=self._workers()
        with self.transaction() as conn:
            row=conn.execute(select(accounts).where(accounts.c.id==account_id)).mappings().first()
            if row is None:raise PlatformError('Account not found.',code='not_found',status=404)
            if row['deleted_at'] is not None:return {'already_deleted':True}
            self._account_delete_ready(conn,row,workers)
            previous={'enabled':row['enabled'],'status':row['status']}
            conn.execute(update(accounts).where(accounts.c.id==account_id).values(enabled=False,status='deleting',updated_at=self.clock()))
            self._limits_cache.pop(account_id,None)
            return previous

    def cancel_account_delete(self,account_id,previous):
        """Only restore the transaction's own pending state, never a deleted row."""
        with self.transaction() as conn:
            status=previous.get('status','disabled')
            # A process restart can leave an interrupted delete. A failed retry
            # keeps it disabled rather than inventing its previous enabled state.
            if status=='deleting':status='disabled'
            conn.execute(update(accounts).where(accounts.c.id==account_id,accounts.c.deleted_at.is_(None),accounts.c.status=='deleting').values(
                enabled=bool(previous.get('enabled')) if previous.get('status')!='deleting' else False,
                status=status,updated_at=self.clock()))

    def delete_account(self,account_id):
        """Logical deletion keeps task ownership and historical download credentials."""
        workers=self._workers()
        with self.transaction() as conn:
            row=conn.execute(select(accounts).where(accounts.c.id==account_id)).mappings().first()
            if row is None:raise PlatformError('Account not found.',code='not_found',status=404)
            if row['deleted_at'] is None:
                if row['status']!='deleting':raise PlatformError('Stop the account worker before deletion.',code='account_delete_not_prepared',status=409)
                self._account_delete_ready(conn,row,workers)
                deleted_at=self.clock()
                conn.execute(update(accounts).where(accounts.c.id==account_id).values(enabled=False,status='deleted',deleted_at=deleted_at,identity_digest=None,updated_at=deleted_at))
            else:deleted_at=row['deleted_at']
        self._limits_cache.pop(account_id,None)
        return {'id':account_id,'enabled':False,'status':'deleted','deleted':True,'deleted_at':deleted_at,'historical_data_retained':True}

    def record_account_check(self, account_id: str, *, status: str, balance: float | None = None) -> dict:
        if status not in ('ready', 'unverified', 'login_required', 'login_pending', 'error', 'disabled'):
            raise PlatformError('Invalid account health status.')
        if balance is not None and (isinstance(balance, bool) or not isinstance(balance, (int, float)) or not math.isfinite(balance) or balance < 0):
            raise PlatformError('Invalid credit balance.')
        with self.transaction() as conn:
            current=conn.execute(select(accounts.c.status,accounts.c.deleted_at).where(accounts.c.id==account_id)).first()
            if current is None or current.deleted_at is not None:raise PlatformError('Account not found.',code='not_found',status=404)
            if current.status=='deleting':raise PlatformError('Account deletion is in progress.',code='account_deleting',status=409)
            if current.status=='login_pending':raise PlatformError('An interactive login owns this account.',code='login_busy',status=409)
            result = conn.execute(update(accounts).where(accounts.c.id == account_id).values(
                status=status, balance=balance, last_checked_at=self.clock(), updated_at=self.clock()))
            if not result.rowcount:
                raise PlatformError('Account not found.', code='not_found', status=404)
        return self.get_account(account_id)

    def reserve_task_credits(self,task_id,job_id,*,required_credits,balance,request_fingerprint):
        """Atomically guard concurrent native activations using conservative holds.

        A hold is an internal safety budget, not an upstream charge. It does not
        expire while an outcome is uncertain. Fresh balances can already include
        an earlier debit, so subtracting active holds may under-use credit; it
        must never silently over-commit it.
        """
        valid=lambda value:isinstance(value,(int,float)) and not isinstance(value,bool) and 0<=value<=1_000_000_000 and math.isfinite(value)
        if not valid(required_credits) or not valid(balance):
            raise PlatformError('A finite exact credit quote and balance are required.')
        if not isinstance(job_id,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',job_id):
            raise PlatformError('A valid claimed job ID is required for a credit hold.')
        if not isinstance(request_fingerprint,str) or not re.fullmatch(r'[a-f0-9]{64}',request_fingerprint):
            raise PlatformError('A request fingerprint is required for a credit hold.')
        now=self.clock()
        with self.transaction() as conn:
            task=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if task is None:raise PlatformError('Task not found.',code='not_found',status=404)
            if task['bridge_job_id']!=job_id or task['submit_id'] or task['status'] not in ('queued','claimed'):
                raise PlatformError('Only the current unsubmitted task can reserve credits.',code='unsafe_credit_reservation',status=409)
            account=conn.execute(select(accounts).where(accounts.c.id==task['account_id'])).mappings().one()
            if account['deleted_at'] is not None or not account['enabled'] or account['status'] in ('deleting','deleted','login_pending','login_required','verification_required','expired','error','disabled'):
                raise PlatformError('The account cannot accept a new activation.',code='account_unavailable',status=409)
            existing=conn.execute(select(credit_holds).where(credit_holds.c.job_id==job_id)).mappings().first()
            if existing and (existing['task_id']!=task_id or existing['account_id']!=task['account_id'] or existing['request_fingerprint']!=request_fingerprint):
                raise PlatformError('The credit hold belongs to a different task or request.',code='credit_hold_conflict',status=409)
            held=conn.execute(select(func.coalesce(func.sum(credit_holds.c.amount),0)).where(
                credit_holds.c.account_id==task['account_id'],credit_holds.c.released_at.is_(None),credit_holds.c.job_id!=job_id)).scalar_one()
            held=float(held);available=max(float(balance)-held,0);minimum=float(account['min_credit_balance'] or 0)
            reason=('below_credit_threshold' if minimum>0 and balance<minimum else
                    'insufficient_credits' if required_credits>balance else
                    'credits_reserved' if required_credits>available else None)
            allowed=reason is None
            # Store the same verified balance that made this decision; no manual
            # enabled flag is changed when the threshold pauses or resumes.
            conn.execute(update(accounts).where(accounts.c.id==account['id']).values(balance=float(balance),last_checked_at=now,updated_at=now))
            if allowed:
                values={'amount':float(required_credits),'updated_at':now,'released_at':None,'release_reason':None}
                if existing:conn.execute(update(credit_holds).where(credit_holds.c.job_id==job_id).values(**values))
                else:conn.execute(insert(credit_holds).values(job_id=job_id,task_id=task_id,account_id=task['account_id'],
                    request_fingerprint=request_fingerprint,created_at=now,**values))
            elif existing and existing['released_at'] is None:
                # Repricing rejected before activation releases only this owned
                # attempt, never another concurrent job's reservation.
                conn.execute(update(credit_holds).where(credit_holds.c.job_id==job_id).values(
                    released_at=now,release_reason='preflight_rejected',updated_at=now))
            return {'allowed':allowed,'reason':reason,'account_id':account['id'],
                    'required_credits':float(required_credits),'balance':float(balance),'held_credits':held,
                    'available_credits':available,'min_credit_balance':minimum,'checked_at':now}

    def release_task_credits(self,task_id,job_id=None,*,reason='safe_before_submission'):
        """Explicit release after a proved terminal/no-submit result; no TTL path."""
        reason=_string(reason,'credit hold release reason',80)
        with self.transaction() as conn:
            task=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if task is None:raise PlatformError('Task not found.',code='not_found',status=404)
            if task['status'] not in TERMINAL_STATES and (task['submit_id'] or task['status']=='uncertain'):
                raise PlatformError('An unresolved possible charge must retain its credit hold.',code='credit_hold_uncertain',status=409)
            query=update(credit_holds).where(credit_holds.c.task_id==task_id,credit_holds.c.released_at.is_(None))
            if job_id is not None:query=query.where(credit_holds.c.job_id==job_id)
            result=conn.execute(query.values(released_at=self.clock(),release_reason=reason,updated_at=self.clock()))
            return result.rowcount

    def active_credit_holds(self,account_id=None):
        query=select(credit_holds).where(credit_holds.c.released_at.is_(None))
        if account_id is not None:query=query.where(credit_holds.c.account_id==account_id)
        with self.engine.connect() as conn:return [dict(row) for row in conn.execute(query).mappings()]

    def abort_browser_login(self,account_id):
        with self.transaction() as conn:
            conn.execute(update(accounts).where(accounts.c.id==account_id,accounts.c.status=='login_pending').values(status='login_required',updated_at=self.clock()))
        return self.get_account(account_id)

    def begin_browser_login(self,account_id):
        with self.transaction() as conn:
            row=conn.execute(select(accounts).where(accounts.c.id==account_id)).mappings().first()
            if not row or row['deleted_at'] is not None:raise PlatformError('Account not found.',code='not_found',status=404)
            if row['status']=='deleting':raise PlatformError('Account deletion is in progress.',code='account_deleting',status=409)
            if row['status']=='proxy_changing' or conn.execute(select(proxy_changes.c.id).where(proxy_changes.c.account_id==account_id,proxy_changes.c.state.in_(('pending','checking','restarting')))).first():raise PlatformError('Finish the proxy change before starting login.',code='proxy_change_in_progress',status=409)
            unfinished=conn.execute(select(tasks.c.status,tasks.c.submit_id,tasks.c.error_json).where(tasks.c.account_id==account_id,tasks.c.status.in_(ACTIVE_STATES))).mappings().all()
            busy=any(not _background_query_only(task) for task in unfinished)
            if busy:raise PlatformError('This account has unfinished tasks; finish or reconcile them before login.',code='account_busy',status=409)
            if unfinished and not row['identity_digest']:
                raise PlatformError('The saved account identity is unavailable; reconcile existing tasks before logging in again.',code='identity_unverified',status=409)
            if row['status']=='login_pending':raise PlatformError('A login session already owns this account.',code='login_busy',status=409)
            conn.execute(update(accounts).where(accounts.c.id==account_id).values(status='login_pending',updated_at=self.clock()))
        return self.get_account(account_id)

    def complete_browser_login(self,account_id,*,credentials,identity,browser_profile,balance=None,persist_state=None):
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity):raise PlatformError('Upstream identity is missing.',code='identity_missing',status=409)
        if not isinstance(credentials,dict) or not credentials.get('session_id'):raise PlatformError('A confirmed session cookie is required.',code='session_missing',status=409)
        digest=hashlib.sha256(('dreamina:'+identity).encode()).hexdigest()
        with self.transaction() as conn:
            row=conn.execute(select(accounts).where(accounts.c.id==account_id)).mappings().first()
            if not row or row['deleted_at'] is not None:raise PlatformError('Account not found.',status=404)
            if row['status']!='login_pending':raise PlatformError('Account no longer belongs to this login session.',code='login_conflict',status=409)
            if row['identity_digest'] and row['identity_digest']!=digest:raise PlatformError('The signed-in website account differs from this saved account. Sign into the original account.',code='identity_mismatch',status=409)
            duplicate=conn.execute(select(accounts.c.id).where(accounts.c.identity_digest==digest,accounts.c.id!=account_id)).first()
            if duplicate:raise PlatformError('This website account is already registered. Use its existing account entry.',code='duplicate_identity',status=409)
            if persist_state:persist_state()
            secret=self._decrypt(row['secret_encrypted']);secret.update({k:v for k,v in credentials.items() if k in SECRET_FIELDS})
            conn.execute(update(accounts).where(accounts.c.id==account_id).values(secret_encrypted=self._encrypt(secret),
                identity_digest=digest,browser_profile=browser_profile,status='ready',balance=balance,last_login_at=self.clock(),last_checked_at=self.clock(),updated_at=self.clock()))
        return self.get_account(account_id)

    def profile_cipher_key(self,account_id):
        if not ACCOUNT_ID.fullmatch(account_id):raise PlatformError('Invalid account ID.')
        return base64.urlsafe_b64encode(hmac.new(self._profile_master_key,('browser-profile:'+account_id).encode(),hashlib.sha256).digest())

    def recover_pending_logins(self):
        with self.transaction() as conn:
            conn.execute(update(accounts).where(accounts.c.status=='login_pending').values(status='login_required',updated_at=self.clock()))

    def settings_for_account(self, account_id: str, *, existing_task=False) -> Settings:
        with self.engine.connect() as conn:
            row = conn.execute(select(accounts).where(accounts.c.id == account_id)).mappings().first()
        if row is None:
            raise PlatformError('Account not found.', code='not_found', status=404)
        values = self._decrypt(row['secret_encrypted'])
        egress=self.resolve_account_egress(account_id,for_existing_task=existing_task)
        values.pop('proxy',None)
        result=Settings(region=row['region'],proxy=egress.httpx_proxy or '', **{k: v for k, v in values.items() if k in SECRET_FIELDS})
        result.egress=egress;result.account_id=account_id
        return result

    def block_account_model(self,account_id,model,reason='account_plan_required',cooldown_seconds=900):
        self.get_account(account_id);model=_string(model,'model')
        if reason!='account_plan_required':raise PlatformError('Unsupported model block reason.')
        cooldown_seconds=_integer(cooldown_seconds,'cooldown_seconds',60,86400)
        with self.transaction() as conn:
            row=conn.execute(select(account_model_health).where(account_model_health.c.account_id==account_id,account_model_health.c.model==model)).mappings().first()
            values={'reason':reason,'blocked_until':self.clock()+cooldown_seconds,'failure_count':(row['failure_count'] if row else 0)+1,'updated_at':self.clock()}
            if row:conn.execute(update(account_model_health).where(account_model_health.c.account_id==account_id,account_model_health.c.model==model).values(**values))
            else:conn.execute(insert(account_model_health).values(account_id=account_id,model=model,**values))
        return {'account_id':account_id,'model':model,**values}

    def account_model_blocks(self,account_id=None):
        query=select(account_model_health).join(accounts,accounts.c.id==account_model_health.c.account_id).where(account_model_health.c.blocked_until>self.clock(),accounts.c.deleted_at.is_(None))
        if account_id is not None:
            self.get_account(account_id);query=query.where(account_model_health.c.account_id==account_id)
        with self.engine.connect() as conn:return [dict(row) for row in conn.execute(query).mappings()]

    def clear_account_model_block(self,account_id,model):
        self.get_account(account_id);model=_string(model,'model')
        with self.transaction() as conn:
            conn.execute(delete(account_model_health).where(account_model_health.c.account_id==account_id,account_model_health.c.model==model))
        return {'account_id':account_id,'model':model,'blocked':False}

    def import_default(self, settings: Settings) -> dict | None:
        with self.engine.connect() as conn:
            exists = conn.execute(select(accounts.c.id,accounts.c.deleted_at).where(accounts.c.id == 'default')).first()
        if exists:
            return None if exists.deleted_at is not None else self.get_account('default')
        if not settings.cookie and not settings.session_id:
            return None
        data = {key: getattr(settings, key, '') for key in SECRET_FIELDS}
        data.update(id='default', name='默认 Dreamina 账号', region=settings.region,
                    capabilities=['seedance-2.0-mini'], enabled=True)
        return self.create_account(data)

    @staticmethod
    def _key(row) -> dict:
        value = {key: row[key] for key in ('id', 'name', 'prefix', 'enabled', 'expires_at', 'max_concurrent',
                      'request_limit', 'used_requests', 'rate_limit_per_minute', 'last_used_at', 'created_at','deleted_at')}
        value['deleted']=row['deleted_at'] is not None
        value['allowed_models'] = json.loads(row['allowed_models_json'])
        value['account_ids'] = json.loads(row['account_ids_json'])
        return value

    def _key_values(self, data: dict) -> dict:
        allowed = {'name', 'enabled', 'expires_at', 'allowed_models', 'account_ids', 'max_concurrent', 'request_limit', 'rate_limit_per_minute'}
        if not isinstance(data, dict) or set(data) - allowed:
            raise PlatformError('Unknown API key configuration fields.')
        values = {}
        if 'name' in data:
            values['name'] = _string(data['name'], 'name')
        if 'enabled' in data:
            if not isinstance(data['enabled'], bool):
                raise PlatformError('enabled must be boolean.')
            values['enabled'] = data['enabled']
        if 'expires_at' in data:
            values['expires_at'] = _time_value(data['expires_at'], 'expires_at')
        for field in ('allowed_models', 'account_ids'):
            if field in data:
                values[field + '_json'] = _json(_strings(data[field], field))
        if 'max_concurrent' in data:
            values['max_concurrent'] = _integer(data['max_concurrent'], 'max_concurrent', 1, 100)
        if 'request_limit' in data:
            values['request_limit'] = None if data['request_limit'] is None else _integer(data['request_limit'], 'request_limit', 0, 10**9)
        if 'rate_limit_per_minute' in data:
            values['rate_limit_per_minute'] = _integer(data['rate_limit_per_minute'], 'rate_limit_per_minute', 1, 10000)
        return values

    def list_keys(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [self._key(row) for row in conn.execute(select(api_keys).where(api_keys.c.deleted_at.is_(None)).order_by(api_keys.c.created_at.desc())).mappings()]

    def create_key(self, data: dict) -> dict:
        values = dict(name='API key', enabled=True, expires_at=None, deleted_at=None, allowed_models_json='[]', account_ids_json='[]',
                      max_concurrent=1, request_limit=None, rate_limit_per_minute=60)
        values.update(self._key_values(data))
        raw = 'sk-dr-' + secrets.token_urlsafe(32)
        values.update(id='key_' + uuid.uuid4().hex, prefix=raw[:14], key_hash=_hash(raw), used_requests=0, last_used_at=None, created_at=self.clock())
        with self.transaction() as conn:
            allowed_ids = json.loads(values['account_ids_json'])
            if allowed_ids:
                found = set(conn.execute(select(accounts.c.id).where(accounts.c.id.in_(allowed_ids),accounts.c.deleted_at.is_(None))).scalars())
                if found != set(allowed_ids):
                    raise PlatformError('An assigned account does not exist.')
            conn.execute(insert(api_keys).values(**values))
        return {'key': raw, 'item': self._key(values)}

    def update_key(self, key_id: str, data: dict) -> dict:
        values = self._key_values(data)
        with self.transaction() as conn:
            row = conn.execute(select(api_keys).where(api_keys.c.id == key_id)).mappings().first()
            if row is None or row['deleted_at'] is not None:
                raise PlatformError('API key not found.', code='not_found', status=404)
            if 'account_ids_json' in values:
                allowed_ids = json.loads(values['account_ids_json'])
                found = set(conn.execute(select(accounts.c.id).where(accounts.c.id.in_(allowed_ids),accounts.c.deleted_at.is_(None))).scalars()) if allowed_ids else set()
                # Preserve an existing restriction to a deleted account; silently
                # dropping its ID would broaden the key's pool to all accounts.
                found.update(set(allowed_ids)&set(json.loads(row['account_ids_json'])))
                if found != set(allowed_ids):
                    raise PlatformError('An assigned account does not exist.')
            conn.execute(update(api_keys).where(api_keys.c.id == key_id).values(**values))
            return self._key(dict(row, **values))

    def delete_key(self,key_id):
        with self.transaction() as conn:
            row=conn.execute(select(api_keys).where(api_keys.c.id==key_id)).mappings().first()
            if row is None:raise PlatformError('API key not found.',code='not_found',status=404)
            values={'enabled':False,'deleted_at':row['deleted_at'] if row['deleted_at'] is not None else self.clock()}
            conn.execute(update(api_keys).where(api_keys.c.id==key_id).values(**values))
            return self._key(dict(row,**values))|{'historical_data_retained':True}

    def authenticate_key(self, raw: str) -> dict:
        if not isinstance(raw, str) or not raw.startswith('sk-dr-') or len(raw) > 256:
            raise PlatformError('Invalid API key.', code='invalid_api_key', status=401)
        digest = _hash(raw)
        with self.engine.connect() as conn:
            row = conn.execute(select(api_keys).where(api_keys.c.key_hash == digest)).mappings().first()
        if row is None or row['deleted_at'] is not None or not hmac.compare_digest(row['key_hash'], digest) or not row['enabled'] or (row['expires_at'] is not None and row['expires_at'] <= self.clock()):
            raise PlatformError('Invalid or expired API key.', code='invalid_api_key', status=401)
        return self._key(row)

    @staticmethod
    def _task(row) -> dict:
        value = {key: row[key] for key in ('id', 'kind', 'model', 'status', 'account_id', 'key_id', 'bridge_job_id',
                    'submit_id', 'progress', 'output_path', 'created_at', 'updated_at', 'completed_at', 'dispatch_claimed')}
        value['request'] = json.loads(row['request_json'])
        value['prompt'] = value['request'].get('prompt', '')
        value['result'] = json.loads(row['result_json']) if row['result_json'] else None
        value['error'] = json.loads(row['error_json']) if row['error_json'] else None
        for key in ('request_detail','progress_details','billing_details','usage','usage_details'):
            value[key]=json.loads(row[key+'_json']) if row[key+'_json'] else None
        value['conversation_id']=row['conversation_id'];value['billing_status']=row['billing_status']
        if value['status']!='completed' and (not value['progress_details'] or value['progress_details'].get('value') is None):value['progress']=None
        return value

    def _available_accounts(self,conn,key,model,now,worker_state,account_id=None,excluded=()):
        query = select(accounts).where(accounts.c.enabled.is_(True),accounts.c.deleted_at.is_(None))
        allowed_accounts = json.loads(key['account_ids_json'])
        if allowed_accounts:
            query = query.where(accounts.c.id.in_(allowed_accounts))
        if account_id:
            if allowed_accounts and account_id not in allowed_accounts:
                raise PlatformError('This API key cannot use the requested account.', code='account_not_allowed', status=403)
            query = query.where(accounts.c.id == account_id)
        candidates = conn.execute(query).mappings().all()
        loads = dict(conn.execute(select(tasks.c.account_id, func.count()).where(tasks.c.status.in_(ACTIVE_STATES)).group_by(tasks.c.account_id)).all())
        blocked=set(conn.execute(select(account_model_health.c.account_id).where(account_model_health.c.model==model,account_model_health.c.blocked_until>now,account_model_health.c.account_id.in_([r['id'] for r in candidates]))).scalars())
        blocked.update(conn.execute(select(submission_risk_events.c.account_id).where(submission_risk_events.c.blocked_until>now,
            submission_risk_events.c.account_id.in_([r['id'] for r in candidates]))).scalars())
        eligible = []
        for row in candidates:
            if row['id'] in blocked or row['id'] in excluded:continue
            if self.proxy_scheduling_reason(conn,row):continue
            if model not in json.loads(row['capabilities_json']):
                continue
            if row['status'] in ('login_required', 'login_pending', 'verification_required', 'expired', 'error', 'disabled', 'deleting', 'deleted') or not row['secret_encrypted']:
                continue
            if row['min_credit_balance']>0 and row['balance'] is not None and row['balance']<row['min_credit_balance']:
                continue
            credential = self._decrypt(row['secret_encrypted'])
            if not credential.get('session_id') and not credential.get('cookie'):
                continue
            entry=worker_state.get(row['id'],{})
            limit=self._inflight_capacity(row,entry)
            if self.worker_status is not None:
                ready=any(w.get('online') and w.get('ready') for w in entry.get('workers', []))
                entry_fresh=any(w.get('online') and (w.get('protocol_version') or 0)>=7 and w.get('entry_ready') is True
                    and type(w.get('entry_checked_at')) in (int,float) and -5<=now-w['entry_checked_at']<=120 for w in entry.get('workers',[]))
                busy_detached=self._detached_workers(entry) and entry_fresh and loads.get(row['id'],0)>0
                if entry.get('blocked_by_uncertain') or not (ready or busy_detached):
                    continue
            if loads.get(row['id'],0)>=limit:continue
            eligible.append(row)
        turns = dict(conn.execute(select(account_schedule.c.account_id, account_schedule.c.last_sequence)).all())
        eligible.sort(key=lambda row: (loads.get(row['id'], 0), turns.get(row['id'], 0), -row['priority'], row['created_at'], row['id']))
        return eligible,turns,candidates

    def reserve_task(self, principal: dict, kind: str, model: str, request: dict, *,
                     idempotency_key: str | None = None, account_id: str | None = None) -> dict:
        if kind not in ('video', 'image'):
            raise PlatformError('kind must be video or image.')
        model = _string(model, 'model')
        if not isinstance(request, dict):
            raise PlatformError('Request must be an object.')
        encoded = _json(request)
        if len(encoded.encode()) > 2_000_000:
            raise PlatformError('Task request is too large.', status=413)
        if idempotency_key is not None:
            idempotency_key = _string(idempotency_key, 'Idempotency-Key', 200)
        request_hash = _hash(_json({'kind': kind, 'model': model, 'request': request, 'account_id': account_id}))
        now, worker_state = self.clock(), self._workers()
        with self.transaction() as conn:
            key = conn.execute(select(api_keys).where(api_keys.c.id == principal.get('id'))).mappings().first()
            if key is None or key['deleted_at'] is not None or not key['enabled'] or key['expires_at'] is not None and key['expires_at'] <= now:
                raise PlatformError('Invalid or expired API key.', code='invalid_api_key', status=401)
            if idempotency_key:
                previous = conn.execute(select(tasks).where(tasks.c.key_id == key['id'], tasks.c.idempotency_key == idempotency_key)).mappings().first()
                if previous:
                    if previous['request_hash'] != request_hash:
                        raise PlatformError('Idempotency-Key was already used for a different request.', code='idempotency_conflict', status=409)
                    return dict(self._task(previous), _created=False)
            allowed_models = json.loads(key['allowed_models_json'])
            if allowed_models and model not in allowed_models:
                raise PlatformError('This API key cannot use the requested model.', code='model_not_allowed', status=403)
            if key['request_limit'] is not None and key['used_requests'] >= key['request_limit']:
                raise PlatformError('API key request quota exhausted.', code='insufficient_quota', status=429)
            active = conn.execute(select(func.count()).select_from(tasks).where(tasks.c.key_id == key['id'], tasks.c.status.in_(ACTIVE_STATES))).scalar_one()
            if active >= key['max_concurrent']:
                raise PlatformError('API key concurrency limit reached.', code='concurrency_limit', status=429)
            recent = conn.execute(select(func.count()).select_from(tasks).where(tasks.c.key_id == key['id'], tasks.c.created_at > now - 60)).scalar_one()
            if recent >= key['rate_limit_per_minute']:
                raise PlatformError('API key rate limit reached.', code='rate_limit_exceeded', status=429)
            eligible,turns,_=self._available_accounts(conn,key,model,now,worker_state,account_id)
            if not eligible:
                raise PlatformError('No enabled, ready account with available capacity supports this model; check model cooldown and account availability.', code='no_available_account', status=503)
            selected=eligible[0]
            values = dict(id=('video_' if kind == 'video' else 'img_') + uuid.uuid4().hex,
                          kind=kind, model=model, status='queued', account_id=selected['id'], key_id=key['id'],
                          request_json=encoded, request_hash=request_hash, idempotency_key=idempotency_key,
                          dispatch_claimed=False, progress=0, created_at=now, updated_at=now)
            conn.execute(insert(tasks).values(**values))
            conn.execute(update(api_keys).where(api_keys.c.id == key['id']).values(used_requests=key['used_requests'] + 1, last_used_at=now))
            conn.execute(update(accounts).where(accounts.c.id == selected['id']).values(last_used_at=now))
            next_turn = max(turns.values(), default=0) + 1
            if selected['id'] in turns:
                conn.execute(update(account_schedule).where(account_schedule.c.account_id == selected['id']).values(last_sequence=next_turn))
            else:
                conn.execute(insert(account_schedule).values(account_id=selected['id'], last_sequence=next_turn))
            row = conn.execute(select(tasks).where(tasks.c.id == values['id'])).mappings().one()
            return dict(self._task(row), _created=True)

    def reassign_insufficient_credits(self,task_id,job,*,wait_seconds=600):
        proof=job.get('credit_preflight') or {}
        if job.get('status')!='failed' or job.get('submit_id') or job.get('activation_started_at') is not None or proof.get('allowed') is not False or proof.get('generation_not_activated') is not True or proof.get('source')!='native_page':
            raise PlatformError('Only a confirmed credit rejection before submission may switch accounts.',code='unsafe_account_switch',status=409)
        required,balance=proof.get('required_credits'),proof.get('balance')
        valid=lambda n:isinstance(n,(int,float)) and not isinstance(n,bool) and math.isfinite(n) and n>=0
        reason=proof.get('reason') or 'insufficient_credits'
        available=proof.get('available_credits',balance);minimum=proof.get('min_credit_balance',0);held=proof.get('held_credits',0)
        rejected=(valid(required) and valid(balance) and (
            reason=='insufficient_credits' and required>balance or
            reason=='below_credit_threshold' and valid(minimum) and minimum>0 and balance<minimum or
            reason=='credits_reserved' and valid(available) and valid(held) and held>0 and required<=balance and required>available and available==max(balance-held,0)))
        if not rejected:
            raise PlatformError('Credit rejection proof is invalid.',code='unsafe_account_switch',status=409)
        now,worker_state=self.clock(),self._workers()
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().one()
            if row['status'] in TERMINAL_STATES or row['bridge_job_id']!=job['id']:return self._task(row)
            if row['status']=='uncertain' or row['submit_id'] or row['account_id']!=job['account_id'] or proof.get('account_id')!=row['account_id']:
                raise PlatformError('Task already submitted or account mismatch.',code='unsafe_account_switch',status=409)
            conn.execute(update(credit_holds).where(credit_holds.c.task_id==task_id,credit_holds.c.job_id==job['id'],credit_holds.c.released_at.is_(None)).values(
                released_at=now,release_reason='safe_preflight_rejection',updated_at=now))
            detail=json.loads(row['request_detail_json'] or '{}');attempts=list(detail.get('credit_attempts') or [])
            if not any(a.get('bridge_job_id')==job['id'] for a in attempts):
                attempts.append({'account_id':row['account_id'],'bridge_job_id':job['id'],**proof})
            detail['credit_attempts']=attempts
            excluded={a['account_id'] for a in attempts};key=conn.execute(select(api_keys).where(api_keys.c.id==row['key_id'])).mappings().one()
            stage='credit_failover_exhausted';error=None
            if not key['enabled'] or key['deleted_at'] is not None or key['expires_at'] is not None and key['expires_at']<=now:
                error={'code':'api_key_revoked','message':'The API key is no longer active; no new account will submit this task.'}
                eligible=[];candidates=[];turns={}
            elif json.loads(key['allowed_models_json']) and row['model'] not in json.loads(key['allowed_models_json']):
                error={'code':'model_not_allowed','message':'The API key no longer permits this model; no generation submitted.'}
                eligible=[];candidates=[];turns={}
            else:eligible,turns,candidates=self._available_accounts(conn,key,row['model'],now,worker_state,excluded=excluded)
            if eligible:
                target=eligible[0];new_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-credit:'+task_id+':'+job['id']+':'+target['id']))
                detail['credit_failover_pending']=True;detail.pop('credit_waiting_noted',None)
                change={'from_account_id':row['account_id'],'to_account_id':target['id'],'old_bridge_job_id':job['id'],
                        'new_bridge_job_id':new_id,'reason':reason,'attempt':len(attempts)}
                conn.execute(update(tasks).where(tasks.c.id==task_id).values(account_id=target['id'],bridge_job_id=new_id,status='queued',
                    dispatch_claimed=True,progress=0,progress_details_json=None,conversation_id=None,error_json=None,
                    request_detail_json=_json(detail),updated_at=now))
                sequence=max(turns.values(),default=0)+1
                if target['id'] in turns:conn.execute(update(account_schedule).where(account_schedule.c.account_id==target['id']).values(last_sequence=sequence))
                else:conn.execute(insert(account_schedule).values(account_id=target['id'],last_sequence=sequence))
                conn.execute(update(accounts).where(accounts.c.id==target['id']).values(last_used_at=now))
                conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=now,stage='account_reassigned',detail_json=_json(change)))
            else:
                remaining=[a for a in candidates if a['id'] not in excluded and row['model'] in json.loads(a['capabilities_json'])
                    and a['status'] not in ('deleting','deleted','login_pending','login_required','verification_required','expired','error','disabled')
                    and not (a['min_credit_balance']>0 and a['balance'] is not None and a['balance']<a['min_credit_balance'])]
                started=attempts[0].get('checked_at',now)
                if error is None and remaining and len(excluded)<100 and now-started<wait_seconds:
                    stage='waiting_for_credit_account'
                    if not detail.get('credit_waiting_noted'):
                        conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=now,stage=stage,detail_json=_json({'tried_account_ids':sorted(excluded)})))
                    detail['credit_waiting_noted']=True
                    conn.execute(update(tasks).where(tasks.c.id==task_id).values(status='queued',error_json=None,progress_details_json=_json({'source':'page','stage':stage,'observed_at':now}),request_detail_json=_json(detail),updated_at=now))
                else:
                    terminal_code=('credit_threshold_paused' if reason=='below_credit_threshold' else reason) if not remaining else 'no_available_credit_account'
                    error=error or {'code':terminal_code,
                                   'message':'No eligible account currently has sufficient unreserved credit above its configured threshold. No generation was submitted.'}
                    conn.execute(update(tasks).where(tasks.c.id==task_id).values(status='failed',error_json=_json(error),request_detail_json=_json(detail),completed_at=now,updated_at=now))
                    conn.execute(update(credit_holds).where(credit_holds.c.task_id==task_id,credit_holds.c.released_at.is_(None)).values(
                        released_at=now,release_reason='task_failed',updated_at=now))
                    conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=now,stage=stage,detail_json=_json({'tried_account_ids':sorted(excluded),'error':error})))
        return self.get_task(task_id)

    def finish_credit_failover_dispatch(self,task_id,job_id):
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().one()
            if row['bridge_job_id']!=job_id:return
            detail=json.loads(row['request_detail_json'] or '{}')
            if detail.pop('credit_failover_pending',False):
                conn.execute(update(tasks).where(tasks.c.id==task_id).values(request_detail_json=_json(detail),updated_at=self.clock()))

    def reassign_unavailable_task(self,task_id,job,reason):
        """Reassign an individually fenced, never-activated queued request."""
        if reason!='upstream_risk_cooldown' and not str(reason).startswith('proxy_'):
            raise PlatformError('This reason does not permit pre-submission reassignment.',code='unsafe_account_switch',status=409)
        if job.get('status')!='failed' or job.get('activation_started_at') is not None or job.get('submit_id') or job.get('confirmed_at') is not None:
            raise PlatformError('Only a fenced unactivated job can change account.',code='unsafe_account_switch',status=409)
        now,workers=self.clock(),self._workers()
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().one()
            if row['status'] in TERMINAL_STATES or row['bridge_job_id']!=job['id']:return self._task(row)
            if row['status'] not in ('queued','claimed') or row['submit_id'] or row['account_id']!=job['account_id']:
                raise PlatformError('The original task may already be submitted.',code='unsafe_account_switch',status=409)
            key=conn.execute(select(api_keys).where(api_keys.c.id==row['key_id'])).mappings().one()
            if not key['enabled'] or key['deleted_at'] is not None or key['expires_at'] is not None and key['expires_at']<=now or json.loads(key['allowed_models_json']) and row['model'] not in json.loads(key['allowed_models_json']):
                eligible=[];turns={}
            else:
                detail=json.loads(row['request_detail_json'] or '{}');attempts=list(detail.get('availability_attempts') or [])
                excluded={a['account_id'] for a in attempts}|{row['account_id']}
                eligible,turns,_=self._available_accounts(conn,key,row['model'],now,workers,excluded=excluded)
            detail=json.loads(row['request_detail_json'] or '{}');attempts=list(detail.get('availability_attempts') or [])
            attempts.append({'account_id':row['account_id'],'bridge_job_id':job['id'],'reason':reason,'at':now,'generation_not_activated':True})
            detail['availability_attempts']=attempts
            conn.execute(update(credit_holds).where(credit_holds.c.task_id==task_id,credit_holds.c.job_id==job['id'],credit_holds.c.released_at.is_(None)).values(released_at=now,updated_at=now,release_reason='safe_availability_reassignment'))
            if eligible:
                target=eligible[0];new_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-availability:'+task_id+':'+job['id']+':'+target['id']))
                detail['credit_failover_pending']=True
                conn.execute(update(tasks).where(tasks.c.id==task_id).values(account_id=target['id'],bridge_job_id=new_id,status='queued',dispatch_claimed=True,
                    progress=0,progress_details_json=None,conversation_id=None,error_json=None,request_detail_json=_json(detail),updated_at=now))
                sequence=max(turns.values(),default=0)+1
                if target['id'] in turns:conn.execute(update(account_schedule).where(account_schedule.c.account_id==target['id']).values(last_sequence=sequence))
                else:conn.execute(insert(account_schedule).values(account_id=target['id'],last_sequence=sequence))
                conn.execute(update(accounts).where(accounts.c.id==target['id']).values(last_used_at=now))
                event={'from_account_id':row['account_id'],'to_account_id':target['id'],'old_bridge_job_id':job['id'],'new_bridge_job_id':new_id,'reason':reason,'generation_not_activated':True}
                stage='availability_reassigned'
            else:
                error={'code':'no_available_account','message':'No permitted account is currently available; the request was not submitted.'}
                conn.execute(update(tasks).where(tasks.c.id==task_id).values(status='failed',completed_at=now,error_json=_json(error),request_detail_json=_json(detail),updated_at=now))
                event={'reason':reason,'generation_not_activated':True};stage='availability_failover_exhausted'
            conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=now,stage=stage,detail_json=_json(event)))
        return self.get_task(task_id)

    def current_availability_reason(self,account_id):
        with self.engine.connect() as conn:
            row=conn.execute(select(accounts).where(accounts.c.id==account_id)).mappings().first()
            if row is None:return None
            if conn.execute(select(submission_risk_events.c.event_id).where(submission_risk_events.c.account_id==account_id,submission_risk_events.c.blocked_until>self.clock()).limit(1)).first():return 'upstream_risk_cooldown'
            return self.proxy_scheduling_reason(conn,row)

    def _activation_error(self,conn,row,now,worker_state):
        """Fresh permissions/capacity; the already reserved parent is excluded."""
        key=conn.execute(select(api_keys).where(api_keys.c.id==row['key_id'])).mappings().first()
        if key is None or key['deleted_at'] is not None or not key['enabled'] or key['expires_at'] is not None and key['expires_at']<=now:
            return {'code':'api_key_revoked','message':'The API key is no longer active; no new generation will be submitted.'}
        if json.loads(key['allowed_models_json']) and row['model'] not in json.loads(key['allowed_models_json']):
            return {'code':'model_not_allowed','message':'The API key no longer permits this model.'}
        if json.loads(key['account_ids_json']) and row['account_id'] not in json.loads(key['account_ids_json']):
            return {'code':'account_not_allowed','message':'The API key no longer permits this account.'}
        account=conn.execute(select(accounts).where(accounts.c.id==row['account_id'])).mappings().first()
        if account is None or account['deleted_at'] is not None or not account['enabled'] or account['status'] in ('deleting','deleted','disabled','login_pending','login_required','verification_required','expired','error') or not account['secret_encrypted']:
            return {'code':'account_unavailable','message':'The assigned account cannot accept a new generation.'}
        if row['model'] not in json.loads(account['capabilities_json']):
            return {'code':'model_not_allowed','message':'The assigned account no longer supports this model.'}
        proxy_reason=self.proxy_scheduling_reason(conn,account)
        if proxy_reason:return {'code':proxy_reason,'message':'The account proxy is unavailable; no direct fallback is allowed.'}
        risk=conn.execute(select(submission_risk_events.c.event_id).where(submission_risk_events.c.account_id==row['account_id'],
            submission_risk_events.c.blocked_until>now).limit(1)).first()
        if risk:return {'code':'upstream_risk_cooldown','message':'The assigned account is cooling down after an upstream risk rejection; no new generation will be submitted.'}
        blocked=conn.execute(select(account_model_health.c.blocked_until).where(account_model_health.c.account_id==row['account_id'],account_model_health.c.model==row['model'],account_model_health.c.blocked_until>now)).first()
        if blocked:return {'code':'account_model_cooldown','message':'The account/model is paused; no new generation will be submitted.'}
        entry=worker_state.get(row['account_id'],{})
        limit=self._inflight_capacity(account,entry)
        other_account=conn.execute(select(func.count()).select_from(tasks).where(tasks.c.account_id==row['account_id'],tasks.c.id!=row['id'],tasks.c.status.in_(ACTIVE_STATES))).scalar_one()
        other_key=conn.execute(select(func.count()).select_from(tasks).where(tasks.c.key_id==row['key_id'],tasks.c.id!=row['id'],tasks.c.status.in_(ACTIVE_STATES))).scalar_one()
        if other_account>=limit or other_key>=key['max_concurrent']:
            return {'code':'retry_capacity_unavailable','message':'Current concurrency limits do not permit this generation attempt.'}
        # used_requests and rate counters belong to the logical request, not attempts.
        return None

    def validate_task_activation(self,task_id,job_id):
        now,workers=self.clock(),self._workers()
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if row is None:raise PlatformError('Task not found.',code='not_found',status=404)
            if row['bridge_job_id']!=job_id or row['submit_id'] or row['status'] not in ('queued','claimed'):
                raise PlatformError('Only the current unsubmitted attempt may activate.',code='unsafe_activation',status=409)
            error=self._activation_error(conn,row,now,workers)
            if error:raise PlatformError(error['message'],code=error['code'],status=409)
            return self._task(row)

    @staticmethod
    def _turn_scopes(account_id,egress_group):
        return ('egress:'+egress_group,'account:'+account_id)

    @staticmethod
    def _turn_response(row,*,ready=True,wait_seconds=0,reason=None):
        return {'ready':ready,'wait_seconds':float(wait_seconds),'job_id':row['job_id'],
                'granted_at':row['granted_at'],'expires_at':row['expires_at'],
                'activated_at':row['activated_at'],'finished_at':row['finished_at'],'reason':reason}

    def _turn_parent(self,conn,task_id,job_id,now,workers):
        row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
        if row is None:raise PlatformError('Task not found.',code='not_found',status=404)
        if row['bridge_job_id']!=job_id or row['submit_id'] or row['status'] not in ('queued','claimed'):
            raise PlatformError('Only the current unsubmitted attempt may reserve a submission turn.',code='unsafe_submission_turn',status=409)
        error=self._activation_error(conn,row,now,workers)
        if error:raise PlatformError(error['message'],code=error['code'],status=409)
        return row

    def reserve_submission_turn(self,task_id,job_id,egress_group='primary',*,ip_interval=3,account_interval=5,lease_seconds=90):
        """Reserve one exclusive current turn; never hand out future permits.

        A pre-activation lease can expire safely. A committed turn cannot be
        reclaimed by time alone: an authenticated outcome must finish it.
        """
        if not isinstance(egress_group,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',egress_group):
            raise PlatformError('Invalid egress group.')
        if not isinstance(job_id,str) or not re.fullmatch(r'[a-fA-F0-9-]{36}',job_id):raise PlatformError('Invalid submission job ID.')
        for value,label,low,high in ((ip_interval,'ip_interval',3,300),(account_interval,'account_interval',5,300),(lease_seconds,'lease_seconds',5,90)):
            if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:raise PlatformError('Invalid '+label+'.')
        now,workers=self.clock(),self._workers()
        with self.transaction() as conn:
            parent=self._turn_parent(conn,task_id,job_id,now,workers)
            previous=conn.execute(select(submission_turns).where(submission_turns.c.job_id==job_id)).mappings().first()
            if previous and (previous['task_id']!=task_id or previous['account_id']!=parent['account_id'] or previous['egress_group']!=egress_group):
                raise PlatformError('Submission turn identity cannot change.',code='submission_turn_conflict',status=409)
            if previous and previous['finished_at'] is not None:return self._turn_response(previous,ready=False,reason='already_finished')
            if previous and previous['activated_at'] is not None:return self._turn_response(previous,ready=False,reason='already_activated')
            scopes=self._turn_scopes(parent['account_id'],egress_group);wait=0.0;blocked=False;own=True
            for scope in scopes:
                gate=conn.execute(select(submission_gates).where(submission_gates.c.scope==scope)).mappings().first()
                if gate is None:
                    conn.execute(insert(submission_gates).values(scope=scope,holder_job_id=None,next_allowed_at=0,updated_at=now));own=False;continue
                wait=max(wait,gate['next_allowed_at']-now)
                holder=gate['holder_job_id']
                if holder!=job_id:own=False
                if holder and holder!=job_id:
                    turn=conn.execute(select(submission_turns).where(submission_turns.c.job_id==holder)).mappings().first()
                    if turn is not None and scope.startswith('egress:') and turn['activated_at'] is not None:
                        # Repair legacy receipt-held exit gates. Only pacing is
                        # released; the account gate and task credit stay held.
                        until=max(gate['next_allowed_at'],turn['activated_at']+turn['ip_interval'])
                        conn.execute(update(submission_gates).where(submission_gates.c.scope==scope).values(holder_job_id=None,next_allowed_at=until,updated_at=now))
                        wait=max(wait,until-now)
                    elif turn is None or turn['activated_at'] is not None and turn['finished_at'] is None:
                        # Inconsistent or possibly active ownership fails closed.
                        blocked=True;wait=max(wait,1)
                    elif turn['finished_at'] is None and turn['expires_at']>now:
                        blocked=True;wait=max(wait,min(5,turn['expires_at']-now))
                    else:conn.execute(update(submission_gates).where(submission_gates.c.scope==scope).values(holder_job_id=None,updated_at=now))
            if blocked or wait>0:
                return {'ready':False,'wait_seconds':max(0.1,wait),'job_id':job_id,'granted_at':None,'expires_at':None,
                        'activated_at':None,'finished_at':None,'reason':'submission_turn_busy' if blocked else 'submission_spacing'}
            if own and previous and previous['expires_at']>now:return self._turn_response(previous)
            values={'task_id':task_id,'account_id':parent['account_id'],'egress_group':egress_group,'granted_at':now,'expires_at':now+lease_seconds,
                    'activated_at':None,'finished_at':None,'ip_interval':float(ip_interval),'account_interval':float(account_interval)}
            if previous:conn.execute(update(submission_turns).where(submission_turns.c.job_id==job_id).values(**values))
            else:conn.execute(insert(submission_turns).values(job_id=job_id,**values))
            for scope in scopes:conn.execute(update(submission_gates).where(submission_gates.c.scope==scope).values(holder_job_id=job_id,updated_at=now))
            return self._turn_response({'job_id':job_id,**values})

    def commit_submission_turn(self,task_id,job_id):
        """Fence a permit immediately before the separate bridge activation.

        The runtime must finish this fence after a definite negative bridge
        activation or observed submission outcome. Failure to acknowledge it
        blocks later turns conservatively instead of allowing a duplicate.
        """
        now,workers=self.clock(),self._workers()
        with self.transaction() as conn:
            parent=self._turn_parent(conn,task_id,job_id,now,workers)
            turn=conn.execute(select(submission_turns).where(submission_turns.c.job_id==job_id)).mappings().first()
            if turn is None:return {'ready':False,'wait_seconds':0,'reason':'submission_turn_required','job_id':job_id}
            if turn['task_id']!=task_id or turn['account_id']!=parent['account_id']:raise PlatformError('Submission turn identity mismatch.',code='submission_turn_conflict',status=409)
            if turn['finished_at'] is not None:return self._turn_response(turn,ready=False,reason='already_finished')
            if turn['activated_at'] is not None:return self._turn_response(turn)
            scopes=self._turn_scopes(turn['account_id'],turn['egress_group'])
            for scope in scopes:
                gate=conn.execute(select(submission_gates).where(submission_gates.c.scope==scope)).mappings().first()
                if gate is None or gate['holder_job_id']!=job_id:return self._turn_response(turn,ready=False,reason='submission_turn_lost')
            if turn['expires_at']<=now:return self._turn_response(turn,ready=False,reason='submission_turn_expired')
            conn.execute(update(submission_turns).where(submission_turns.c.job_id==job_id).values(activated_at=now))
            for scope,interval in zip(scopes,(turn['ip_interval'],turn['account_interval'])):
                conn.execute(update(submission_gates).where(submission_gates.c.scope==scope).values(
                    next_allowed_at=now+interval,holder_job_id=None if scope.startswith('egress:') else job_id,updated_at=now))
            return self._turn_response(dict(turn,activated_at=now))

    def finish_submission_turn(self,job_id):
        """Record an authenticated response/explicit denial; idempotent.

        Callers must verify the bridge receipt/claim first. This is deliberately
        not an expiry cleanup API: an activated unknown result remains gated.
        """
        now=self.clock()
        with self.engine.connect() as conn:
            current=conn.execute(select(submission_turns.c.finished_at).where(submission_turns.c.job_id==job_id)).first()
        if current is None:return {'finished':False,'reason':'no_turn','job_id':job_id}
        if current[0] is not None:return {'finished':True,'finished_at':current[0],'job_id':job_id}
        with self.transaction() as conn:
            turn=conn.execute(select(submission_turns).where(submission_turns.c.job_id==job_id)).mappings().first()
            if turn is None:return {'finished':False,'reason':'no_turn','job_id':job_id}
            if turn['finished_at'] is not None:return {'finished':True,'finished_at':turn['finished_at'],'job_id':job_id}
            conn.execute(update(submission_turns).where(submission_turns.c.job_id==job_id).values(finished_at=now))
            for scope,interval in zip(self._turn_scopes(turn['account_id'],turn['egress_group']),(turn['ip_interval'],turn['account_interval'])):
                gate=conn.execute(select(submission_gates).where(submission_gates.c.scope==scope)).mappings().first()
                if gate and gate['holder_job_id']==job_id:
                    anchor=turn['activated_at'] if turn['activated_at'] is not None else now
                    conn.execute(update(submission_gates).where(submission_gates.c.scope==scope).values(holder_job_id=None,next_allowed_at=max(gate['next_allowed_at'],anchor+interval),updated_at=now))
            return {'finished':True,'finished_at':now,'job_id':job_id}

    def release_submission_gate(self,scope,*,expected_job_id,note):
        """Operator pacing-only release after the runtime verified its proof."""
        scope=_string(scope,'scope',160);note=_string(note,'note',500)
        with self.transaction() as conn:
            gate=conn.execute(select(submission_gates).where(submission_gates.c.scope==scope)).mappings().first()
            if gate is None:raise PlatformError('Submission gate not found.',code='not_found',status=404)
            if gate['holder_job_id']!=expected_job_id:raise PlatformError('The gate owner changed; refresh before releasing.',code='gate_owner_changed',status=409)
            if expected_job_id is None:return {'released':True,'scope':scope,'already_free':True}
            turn=conn.execute(select(submission_turns).where(submission_turns.c.job_id==expected_job_id)).mappings().first()
            if turn is None:raise PlatformError('Gate ownership is incomplete; preserve it for review.',code='gate_proof_missing',status=409)
            now=self.clock();interval=turn['ip_interval'] if scope.startswith('egress:') else turn['account_interval']
            conn.execute(update(submission_gates).where(submission_gates.c.scope==scope).values(holder_job_id=None,next_allowed_at=max(gate['next_allowed_at'],now+interval),updated_at=now))
            conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=turn['task_id'],created_at=now,stage='operator_gate_release',
                detail_json=_json(sanitize({'scope':scope,'job_id':expected_job_id,'note':note,'credit_released':False,'task_resubmitted':False}))))
        return {'released':True,'scope':scope,'task_id':turn['task_id'],'credit_released':False,'task_resubmitted':False}

    def clear_risk_cooldown(self,account_id,*,note):
        note=_string(note,'note',500);now=self.clock()
        with self.transaction() as conn:
            if not conn.execute(select(accounts.c.id).where(accounts.c.id==account_id,accounts.c.deleted_at.is_(None))).first():raise PlatformError('Account not found.',code='not_found',status=404)
            changed=conn.execute(update(submission_risk_events).where(submission_risk_events.c.account_id==account_id,submission_risk_events.c.blocked_until>now).values(blocked_until=now)).rowcount
            conn.execute(delete(account_model_health).where(account_model_health.c.account_id==account_id,account_model_health.c.reason=='upstream_risk_cooldown'))
            last=conn.execute(select(tasks.c.id).where(tasks.c.account_id==account_id).order_by(tasks.c.created_at.desc()).limit(1)).first()
            if last:conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=last[0],created_at=now,stage='operator_risk_cooldown_clear',detail_json=_json(sanitize({'account_id':account_id,'note':note,'cleared_events':changed}))))
        return {'account_id':account_id,'cleared':True,'cleared_events':changed}

    def submission_gate_status(self):
        now=self.clock()
        with self.engine.connect() as conn:
            rows=conn.execute(select(submission_gates,submission_turns.c.activated_at,submission_turns.c.expires_at,submission_turns.c.task_id)
                .outerjoin(submission_turns,submission_gates.c.holder_job_id==submission_turns.c.job_id)).mappings().all()
        return [{'scope':r['scope'],'job_id':r['holder_job_id'],'task_id':r['task_id'],'activated_at':r['activated_at'],
                 'expires_at':r['expires_at'],'next_allowed_at':r['next_allowed_at'],
                 'awaiting_receipt':r['holder_job_id'] is not None and r['activated_at'] is not None,
                 'wait_seconds':max(0,r['next_allowed_at']-now)} for r in rows]

    def record_submission_risk(self,account_id,model,event_id,*,base_seconds=60,max_seconds=900):
        """Back off all new account submissions; never reassign its task.

        Individual evidence retains the model, but the exponential counter and
        active cooldown are account-wide. Model permission resets cannot erase
        a risk cooldown or bypass it by selecting another model.
        """
        event_id=_string(event_id,'risk event ID',128);model=_string(model,'model',120)
        _integer(base_seconds,'base_seconds',1,3600);_integer(max_seconds,'max_seconds',base_seconds,86400)
        now=self.clock()
        with self.transaction() as conn:
            previous=conn.execute(select(submission_risk_events).where(submission_risk_events.c.event_id==event_id)).mappings().first()
            if previous:
                if previous['account_id']!=account_id or previous['model']!=model:raise PlatformError('Risk event identity mismatch.')
                return {'blocked_until':previous['blocked_until'],'recorded':False}
            if conn.execute(select(accounts.c.id).where(accounts.c.id==account_id)).first() is None:raise PlatformError('Account not found.',code='not_found',status=404)
            recent=conn.execute(select(func.count()).select_from(submission_risk_events).where(
                submission_risk_events.c.account_id==account_id,submission_risk_events.c.created_at>now-86400)).scalar_one()
            duration=min(max_seconds,base_seconds*2**min(recent,16));until=now+duration
            health=conn.execute(select(account_model_health).where(account_model_health.c.account_id==account_id,account_model_health.c.model==model)).mappings().first()
            health_until=max(until,health['blocked_until']) if health else until
            health_reason=health['reason'] if health and health['blocked_until']>now and health['reason']!='upstream_risk_cooldown' else 'upstream_risk_cooldown'
            values={'reason':health_reason,'blocked_until':health_until,'failure_count':(health['failure_count'] if health else 0)+1,'updated_at':now}
            if health:conn.execute(update(account_model_health).where(account_model_health.c.account_id==account_id,account_model_health.c.model==model).values(**values))
            else:conn.execute(insert(account_model_health).values(account_id=account_id,model=model,**values))
            conn.execute(insert(submission_risk_events).values(event_id=event_id,account_id=account_id,model=model,blocked_until=until,created_at=now))
            return {'blocked_until':until,'cooldown_seconds':until-now,'recorded':True}

    @staticmethod
    def _retry_terminal(conn,row,detail,error,now):
        retry=dict(detail.get('submission_retry') or {})
        retry.update(stage='exhausted',next_retry_at=None)
        detail=dict(detail,submission_retry=retry);detail.pop('submission_retry_pending',None)
        identifiers={}
        current=next((a for a in reversed(detail.get('submission_attempts') or []) if a.get('bridge_job_id')==row['bridge_job_id']),None)
        if current and not row['submit_id']:identifiers['submit_id']=current.get('submit_id')
        conn.execute(update(tasks).where(tasks.c.id==row['id']).values(status='failed',error_json=_json(sanitize(error)),
            completed_at=now,updated_at=now,request_detail_json=_json(sanitize(detail)),**identifiers))
        conn.execute(update(credit_holds).where(credit_holds.c.task_id==row['id'],credit_holds.c.released_at.is_(None)).values(released_at=now,release_reason='retry_stopped',updated_at=now))
        conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=row['id'],created_at=now,stage='submission_retry_stopped',detail_json=_json(sanitize(error))))

    def schedule_submission_retry(self,task_id,job,*,delay_seconds,max_attempts=2,total_timeout=900):
        """One new native job after explicit unaccepted transient rejection only."""
        from .credit_quote import quote_request_fingerprint
        from .upstream_errors import retry_proof,sanitize_upstream_error
        _integer(max_attempts,'max_attempts',1,2)
        if isinstance(delay_seconds,bool) or not isinstance(delay_seconds,(int,float)) or not math.isfinite(delay_seconds) or not 0<=delay_seconds<=300:
            raise PlatformError('Invalid submission retry delay.')
        _integer(total_timeout,'total_timeout',1,7200)
        if not isinstance(job,dict):raise PlatformError('Invalid retry evidence.',code='unsafe_submission_retry',status=409)
        now,workers=self.clock(),self._workers()
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if row is None:raise PlatformError('Task not found.',code='not_found',status=404)
            if row['status'] in TERMINAL_STATES or row['bridge_job_id']!=job.get('id'):return self._task(row)
            proof=job.get('credit_preflight') or {};required=proof.get('required_credits')
            valid_amount=isinstance(required,(int,float)) and not isinstance(required,bool) and math.isfinite(required) and 0<=required<=1_000_000_000
            valid=(job.get('status')=='failed' and job.get('error_code')=='upstream_rejected' and retry_proof(job.get('upstream_error'))
                and type(job.get('claim_protocol_version')) is int and job['claim_protocol_version']>=5
                and job.get('confirmed_at') is None and type(job.get('activation_started_at')) in (int,float) and math.isfinite(job['activation_started_at'])
                and row['status'] in ('queued','claimed') and row['account_id']==job.get('account_id')
                and isinstance(job.get('submit_id'),str) and bool(re.fullmatch(r'[0-9a-fA-F-]{36}',job['submit_id'])) and row['submit_id'] in (None,job.get('submit_id'))
                and proof.get('allowed') is True and proof.get('source')=='native_page' and valid_amount)
            if valid:
                try:valid=proof.get('request_fingerprint')==quote_request_fingerprint(job['request'])
                except (ValueError,KeyError,TypeError):valid=False
            hold=conn.execute(select(credit_holds).where(credit_holds.c.job_id==job.get('id'),credit_holds.c.task_id==task_id)).mappings().first()
            valid=valid and hold is not None and hold['account_id']==row['account_id'] and hold['amount']==required and hold['request_fingerprint']==proof.get('request_fingerprint') and hold['released_at'] is None
            if not valid:raise PlatformError('No safe proof permits another generation.',code='unsafe_submission_retry',status=409)
            detail=json.loads(row['request_detail_json'] or '{}');attempts=list(detail.get('submission_attempts') or [])
            if any(not isinstance(a,dict) for a in attempts):raise PlatformError('Invalid attempt history.',code='unsafe_submission_retry',status=409)
            retry=dict(detail.get('submission_retry') or {});attempt=len(attempts)+1
            ceiling=retry.get('quote_ceiling',required)
            if not isinstance(ceiling,(int,float)) or isinstance(ceiling,bool) or not math.isfinite(ceiling) or ceiling<0 or required>ceiling:
                raise PlatformError('Retry price evidence exceeds the original budget.',code='unsafe_submission_retry',status=409)
            diagnostic=sanitize_upstream_error(job['upstream_error'])
            original_error={'code':'upstream_rejected','message':job.get('error') or 'The upstream rejected this request.','upstream_error':diagnostic}
            attempts.append({'attempt':attempt,'bridge_job_id':job['id'],'account_id':row['account_id'],'submit_id':job['submit_id'],
                'conversation_id':row['conversation_id'],'status':'failed','error':original_error,'quote_credits':float(required),
                'started_at':job.get('created_at',row['created_at']),'ended_at':now})
            detail['submission_attempts']=attempts
            reason=diagnostic.get('error_code') or diagnostic.get('ret') or 'transient_rejection'
            retry.update(attempt=attempt,max_attempts=max_attempts,quote_ceiling=float(ceiling),reason=reason)
            detail['submission_retry']=retry
            error=self._activation_error(conn,row,now,workers)
            if now-row['created_at']+delay_seconds>=total_timeout:error={'code':'task_timeout','message':'The original task deadline leaves no time for another attempt.'}
            elif attempt>=max_attempts:error={'code':'submission_retry_exhausted','message':'The allowed generation attempts have been exhausted.','last_error':original_error}
            if error:
                self._retry_terminal(conn,row,detail,error,now)
            else:
                new_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-submission-retry:'+task_id+':'+job['id']))
                retry.update(attempt=attempt+1,stage='waiting',next_retry_at=now+delay_seconds,scheduled_at=now)
                detail['submission_retry_pending']=True
                conn.execute(update(credit_holds).where(credit_holds.c.job_id==job['id'],credit_holds.c.task_id==task_id,credit_holds.c.released_at.is_(None)).values(released_at=now,release_reason='explicit_not_accepted',updated_at=now))
                conn.execute(update(tasks).where(tasks.c.id==task_id).values(status='queued',bridge_job_id=new_id,submit_id=None,conversation_id=None,
                    result_json=None,error_json=None,progress=0,progress_details_json=None,output_path=None,completed_at=None,
                    billing_status=None,billing_details_json=None,usage_json=None,usage_details_json=None,
                    dispatch_claimed=True,request_detail_json=_json(sanitize(detail)),updated_at=now))
                conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=now,stage='submission_retry_scheduled',
                    detail_json=_json({'attempt':attempt+1,'old_bridge_job_id':job['id'],'new_bridge_job_id':new_id,'next_retry_at':now+delay_seconds,'reason':reason,'quote_ceiling':float(ceiling)})))
            return self._task(conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().one())

    def validate_submission_retry(self,task_id,*,total_timeout=900):
        _integer(total_timeout,'total_timeout',1,7200)
        now,workers=self.clock(),self._workers()
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if row is None:raise PlatformError('Task not found.',code='not_found',status=404)
            detail=json.loads(row['request_detail_json'] or '{}')
            if row['status'] in TERMINAL_STATES or not detail.get('submission_retry_pending'):return self._task(row)
            error=self._activation_error(conn,row,now,workers)
            if now-row['created_at']>=total_timeout:error={'code':'task_timeout','message':'The original task deadline expired before retry dispatch.'}
            if error:self._retry_terminal(conn,row,detail,error,now)
            return self._task(conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().one())

    def finish_submission_retry_dispatch(self,task_id,job_id):
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if row is None:raise PlatformError('Task not found.',code='not_found',status=404)
            if row['bridge_job_id']!=job_id or row['status'] in TERMINAL_STATES:return self._task(row)
            detail=json.loads(row['request_detail_json'] or '{}')
            if detail.pop('submission_retry_pending',False):
                retry=dict(detail.get('submission_retry') or {});retry.update(stage='preparing',next_retry_at=None,started_at=self.clock());detail['submission_retry']=retry
                conn.execute(update(tasks).where(tasks.c.id==task_id).values(request_detail_json=_json(sanitize(detail)),updated_at=self.clock()))
            return self._task(conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().one())

    def claim_dispatch(self, task_id: str) -> bool:
        with self.transaction() as conn:
            result = conn.execute(update(tasks).where(tasks.c.id == task_id, tasks.c.status == 'queued', tasks.c.dispatch_claimed.is_(False)).values(dispatch_claimed=True, updated_at=self.clock()))
            return result.rowcount == 1

    def fail_missing_initial_dispatch(self,task_id,dispatch_id):
        """Close a queued admission that never persisted its initial queue ID.

        The runtime fences the deterministic bridge ID first. Recheck identity
        and state in this transaction so a racing real dispatch is preserved.
        """
        now=self.clock()
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if row is None:raise PlatformError('Task not found.',code='not_found',status=404)
            expected=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-'+row['kind']+'-dispatch:'+task_id))
            if row['kind'] not in ('video','image') or dispatch_id!=expected:
                raise PlatformError('Initial dispatch identity does not match this task.',code='invalid_dispatch_identity',status=409)
            if row['status']!='queued' or row['bridge_job_id'] is not None or row['submit_id'] is not None:return self._task(row)
            error={'code':'dispatch_failed','message':'The initial dispatch was interrupted before reaching the browser queue; no generation was activated.'}
            conn.execute(update(tasks).where(tasks.c.id==task_id).values(status='failed',bridge_job_id=dispatch_id,dispatch_claimed=True,
                error_json=_json(error),completed_at=now,updated_at=now))
            conn.execute(update(credit_holds).where(credit_holds.c.task_id==task_id,credit_holds.c.released_at.is_(None)).values(
                released_at=now,release_reason='missing_initial_dispatch',updated_at=now))
            conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=now,stage='dispatch_failed',
                detail_json=_json({'generation_not_activated':True,'bridge_job_id':dispatch_id,'reason':'missing_initial_dispatch'})))
            return self._task(conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().one())

    def get_task(self, task_id: str, principal: dict | None = None) -> dict:
        query = select(tasks).where(tasks.c.id == task_id)
        if principal is not None:
            query = query.where(tasks.c.key_id == principal.get('id'))
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().first()
        if row is None:
            raise PlatformError('Task not found.', code='not_found', status=404)
        return self._task(row)

    def list_tasks(self, principal: dict | None = None, *, kind=None, status=None, limit=50, account_id=None) -> list[dict]:
        _integer(limit, 'limit', 1, 500)
        query = select(tasks).order_by(tasks.c.created_at.desc(), tasks.c.id.desc()).limit(limit)
        if principal is not None:
            query = query.where(tasks.c.key_id == principal.get('id'))
        for column, value in ((tasks.c.kind, kind), (tasks.c.status, status), (tasks.c.account_id, account_id)):
            if value:
                query = query.where(column == value)
        with self.engine.connect() as conn:
            return [self._task(row) for row in conn.execute(query).mappings()]

    def reconciliation_tasks(self, *, after_id: str | None = None, limit: int = 100) -> list[dict]:
        limit = _integer(limit, 'limit', 1, 500)
        query = select(tasks).where(tasks.c.status.in_(ACTIVE_STATES),
            tasks.c.bridge_job_id.is_not(None) | (tasks.c.status=='queued'))
        if after_id:
            query = query.where(tasks.c.id > after_id)
        query = query.order_by(tasks.c.id).limit(limit)
        with self.engine.connect() as conn:
            return [self._task(row) for row in conn.execute(query).mappings()]

    def update_task(self, task_id: str, *, expected_bridge_job_id=None, **fields) -> dict:
        allowed = {'status', 'bridge_job_id', 'submit_id', 'result', 'error', 'progress', 'output_path', 'completed_at',
                   'request_detail','progress_details','conversation_id','billing_status','billing_details','usage','usage_details'}
        if set(fields) - allowed:
            raise PlatformError('Immutable or unknown task fields cannot be changed.')
        # The frequent status/ID/progress observation can be answered with a
        # snapshot when nothing changes. Do not acquire BEGIN IMMEDIATE or the
        # PostgreSQL scheduler row lock just to record the same observation.
        observation_fields={'submit_id','status','progress','error','progress_details','conversation_id'}
        if set(fields)<=observation_fields:
            with self.engine.connect() as connection:
                snapshot=connection.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
            if snapshot is not None:
                same=True
                for name,value in fields.items():
                    if name=='progress' and (type(value) is not int or not 0<=value<=100):same=False;break
                    if name=='progress_details':
                        if not isinstance(value,dict) or value.get('source') not in ('page','upstream','terminal') or not isinstance(value.get('observed_at'),(int,float)):
                            same=False;break
                        previous=json.loads(snapshot['progress_details_json'] or 'null')
                        current=dict(sanitize(value))
                        if fields.get('progress') is not None:current['value']=fields['progress']
                        same=isinstance(previous,dict) and {k:v for k,v in current.items() if k!='observed_at'}=={k:v for k,v in previous.items() if k!='observed_at'}
                    elif name=='error':same=snapshot['error_json']==(_json(value) if value is not None else None)
                    else:same=snapshot[name]==value
                    if not same:break
                if same:return self._task(snapshot)
        with self.transaction() as conn:
            row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
            if row is None:
                raise PlatformError('Task not found.', code='not_found', status=404)
            if expected_bridge_job_id is not None and row['bridge_job_id']!=expected_bridge_job_id:
                return self._task(row)
            status = fields.get('status', row['status'])
            if status not in ACTIVE_STATES + TERMINAL_STATES:
                raise PlatformError('Invalid task status.')
            if row['status'] in TERMINAL_STATES and status != row['status']:
                raise PlatformError('Terminal tasks cannot be resubmitted.', code='invalid_transition', status=409)
            if row['status'] != 'queued' and status == 'queued':
                raise PlatformError('A task cannot be returned to the queue after dispatch.', code='invalid_transition', status=409)
            values = {'updated_at': self.clock()}
            for field,allowed_sources in [('usage',('upstream',)),('billing_status',('upstream','billing_api'))]:
                evidence=fields.get('usage_details' if field=='usage' else 'billing_details')
                if fields.get(field) is not None and (not isinstance(evidence,dict) or evidence.get('source') not in allowed_sources or not isinstance(evidence.get('observed_at'),(int,float))):
                    raise PlatformError(f'{field} requires explicit upstream evidence.')
            if fields.get('progress_details') is not None:
                evidence=fields['progress_details']
                if not isinstance(evidence,dict) or evidence.get('source') not in ('page','upstream','terminal') or not isinstance(evidence.get('observed_at'),(int,float)):
                    raise PlatformError('Progress requires its observation source and timestamp.')
                if fields.get('progress') is not None:evidence['value']=fields['progress']
            for key, value in fields.items():
                if key in ('result', 'error','request_detail','progress_details','billing_details','usage','usage_details'):
                    if key in ('request_detail','progress_details','billing_details','usage_details'):value=sanitize(value)
                    if key == 'result' and row['status'] == 'completed' and isinstance(value, dict) and row['result_json']:
                        old_result = json.loads(row['result_json'])
                        old_images = old_result.get('images', [])
                        value = dict(value)
                        if isinstance(value.get('images'), list):
                            value['images'] = [dict(entry, **({'_local_image': old_images[i]['_local_image']}
                                if i < len(old_images) and isinstance(old_images[i], dict) and old_images[i].get('_local_image') and not entry.get('_local_image') else {}))
                                for i, entry in enumerate(value['images'])]
                    text = _json(value) if value is not None else None
                    if text and len(text.encode()) > 4_000_000:
                        raise PlatformError('Task result is too large.')
                    values[key + '_json'] = text
                elif key == 'progress':
                    if value is not None:values[key] = _integer(value, 'progress', 0, 100)
                elif key=='conversation_id':
                    if value is not None and (not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value)):
                        raise PlatformError('Invalid conversation ID.')
                    values[key]=value
                else:
                    values[key] = value
            for field in ('submit_id', 'bridge_job_id'):
                if row[field] and field in fields and row[field] != fields[field]:
                    raise PlatformError('An observed task identifier cannot be replaced.', code='invalid_transition', status=409)
            if status in TERMINAL_STATES and not row['completed_at']:
                values.setdefault('completed_at', self.clock())
            if status == 'completed':
                values['progress'] = 100
            if status in TERMINAL_STATES:
                detail=json.loads(values.get('request_detail_json',row['request_detail_json']) or '{}')
                if isinstance(detail.get('submission_retry'),dict):
                    retry=dict(detail['submission_retry']);attempts=list(detail.get('submission_attempts') or [])
                    if row['bridge_job_id'] and not any(a.get('bridge_job_id')==row['bridge_job_id'] for a in attempts):
                        hold=conn.execute(select(credit_holds.c.amount).where(credit_holds.c.task_id==task_id,credit_holds.c.job_id==row['bridge_job_id'])).first()
                        error=fields.get('error',json.loads(row['error_json']) if row['error_json'] else None)
                        attempts.append({'attempt':retry.get('attempt',len(attempts)+1),'bridge_job_id':row['bridge_job_id'],'account_id':row['account_id'],
                            'submit_id':fields.get('submit_id',row['submit_id']),'conversation_id':fields.get('conversation_id',row['conversation_id']),
                            'status':status,'error':error,'quote_credits':hold[0] if hold else None,
                            'started_at':retry.get('started_at',retry.get('scheduled_at',row['created_at'])),'ended_at':values.get('completed_at',row['completed_at'] or self.clock())})
                    retry.update(stage='completed' if status=='completed' else 'exhausted',next_retry_at=None)
                    detail.update(submission_retry=retry,submission_attempts=attempts);detail.pop('submission_retry_pending',None)
                    values['request_detail_json']=_json(sanitize(detail))
            # A poll with identical content is a read, not another write lease.
            if 'progress_details_json' in values and values['progress_details_json'] and row['progress_details_json']:
                new_progress=json.loads(values['progress_details_json']);old_progress=json.loads(row['progress_details_json'])
                if {k:v for k,v in new_progress.items() if k!='observed_at'}=={k:v for k,v in old_progress.items() if k!='observed_at'}:
                    values['progress_details_json']=row['progress_details_json']
            if all(k=='updated_at' or row[k]==v for k,v in values.items()):return self._task(row)
            conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
            if status in TERMINAL_STATES:
                conn.execute(update(credit_holds).where(credit_holds.c.task_id==task_id,credit_holds.c.released_at.is_(None)).values(
                    released_at=self.clock(),release_reason='task_'+status,updated_at=self.clock()))
            if status!=row['status']:
                conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=self.clock(),stage=status,
                    detail_json=_json(sanitize({'from':row['status'],'to':status,'error':fields.get('error')}))))
        return self.get_task(task_id)

    def append_event(self, task_id, stage, detail, *, expected_bridge_job_id=None):
        stage=_string(stage,'stage',80)
        self.get_task(task_id)
        cleaned=sanitize(detail);encoded=_json(cleaned)
        if len(encoded.encode())>150_000:raise PlatformError('Event detail is too large.')
        event_row={'id':uuid.uuid4().hex,'task_id':task_id,'created_at':self.clock(),'stage':stage,'detail_json':encoded}
        with self.transaction() as conn:
            if expected_bridge_job_id is not None:
                row=conn.execute(select(tasks.c.bridge_job_id).where(tasks.c.id==task_id)).first()
                if row is None or row[0]!=expected_bridge_job_id:return None
            conn.execute(insert(task_events).values(**event_row))
        return {k:v for k,v in event_row.items() if k!='detail_json'}|{'detail':cleaned}

    def record_bridge_observation(self,bridge_job_id,values):
        """Fence late callbacks against the current attempt in one transaction."""
        if not isinstance(values,dict):raise PlatformError('Observation must be an object.')
        stage=_string(values.get('stage'),'stage',80)
        observed=values.get('observed_at')
        if isinstance(observed,bool) or not isinstance(observed,(int,float)) or not math.isfinite(observed):
            raise PlatformError('Observation requires a finite timestamp.')
        clean=sanitize(dict(values,bridge_job_id=bridge_job_id));encoded=_json(clean)
        if len(encoded.encode())>150_000:raise PlatformError('Event detail is too large.')
        with self.transaction() as conn:
            row=conn.execute(select(tasks).where(tasks.c.bridge_job_id==bridge_job_id)).mappings().first()
            if row is None:return None
            # A terminal attempt's late telemetry is not authoritative anymore.
            if row['status'] in TERMINAL_STATES:return self._task(row)
            conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=row['id'],created_at=self.clock(),stage=stage,detail_json=encoded))
            patch={}
            conversation=values.get('conversation_id')
            if conversation is not None:
                if not isinstance(conversation,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',conversation):
                    raise PlatformError('Invalid conversation ID.')
                patch['conversation_id']=conversation
            previous=json.loads(row['progress_details_json'] or '{}')
            if values.get('progress') is not None and observed>=previous.get('observed_at',0):
                progress=_integer(values['progress'],'progress',0,100)
                patch.update(progress=progress,progress_details_json=_json({'source':'page','observed_at':observed,'stage':stage,'value':progress}))
            if patch:conn.execute(update(tasks).where(tasks.c.id==row['id']).values(updated_at=self.clock(),**patch))
            current=conn.execute(select(tasks).where(tasks.c.id==row['id'])).mappings().one()
            return self._task(current)

    def list_events(self, task_id, *, limit=100):
        self.get_task(task_id);limit=_integer(limit,'limit',1,500)
        with self.engine.connect() as conn:
            rows=conn.execute(select(task_events).where(task_events.c.task_id==task_id).order_by(task_events.c.created_at,task_events.c.id).limit(limit)).mappings()
            return [{k:r[k] for k in ('id','task_id','created_at','stage')}|{'detail':json.loads(r['detail_json'])} for r in rows]

    def task_for_bridge(self,bridge_job_id):
        with self.engine.connect() as conn:
            row=conn.execute(select(tasks).where(tasks.c.bridge_job_id==bridge_job_id)).mappings().first()
            return self._task(row) if row else None

    def dashboard(self) -> dict:
        now = self.clock()
        midnight = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        account_list = self.list_accounts()
        with self.engine.connect() as conn:
            counts = dict(conn.execute(select(tasks.c.status, func.count()).group_by(tasks.c.status)).all())
            key_count = conn.execute(select(func.count()).select_from(api_keys).where(api_keys.c.deleted_at.is_(None))).scalar_one()
            today = conn.execute(select(func.count()).select_from(tasks).where(tasks.c.created_at >= midnight)).scalar_one()
            model_counts = [{'model': model, 'count': count} for model, count in conn.execute(select(tasks.c.model, func.count()).group_by(tasks.c.model))]
            recent = conn.execute(select(tasks.c.created_at, tasks.c.status).where(tasks.c.created_at >= midnight - 6 * 86400)).all()
        done, failed = counts.get('completed', 0), counts.get('failed', 0)
        daily = []
        for index in range(6, -1, -1):
            start = midnight - index * 86400
            matching = [status for created, status in recent if start <= created < start + 86400]
            daily.append({'date': datetime.fromtimestamp(start, timezone.utc).date().isoformat(), 'requests': len(matching),
                          'completed': matching.count('completed'), 'failed': matching.count('failed')})
        return {'summary': {'accounts_total': len(account_list), 'accounts_enabled': sum(x['enabled'] for x in account_list),
                'accounts_online': sum(x['online'] for x in account_list), 'keys_total': key_count,
                'tasks_total': sum(counts.values()), 'tasks_active': sum(counts.get(s, 0) for s in ACTIVE_STATES),
                'tasks_completed': done, 'tasks_failed': failed, 'success_rate': round(done * 100 / (done + failed), 1) if done + failed else None,
                'requests_today': today}, 'recent_tasks': self.list_tasks(limit=10), 'daily_usage': daily, 'model_usage': model_counts}

    def get_settings(self) -> dict:
        result = {'platform_name': 'Dreamina Gateway', 'default_image_timeout': 180, 'registration_enabled': False,
                  'storage_backend': self.storage_backend,'account_proxy_required':self.account_proxy_required}
        with self.engine.connect() as conn:
            for row in conn.execute(select(options)).mappings():
                result[row['name']] = json.loads(row['value_json'])
        return result

    def update_settings(self, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - {'platform_name', 'default_image_timeout'}:
            raise PlatformError('Only platform_name and default_image_timeout are editable.')
        if 'platform_name' in values:
            values['platform_name'] = _string(values['platform_name'], 'platform_name', 80)
        if 'default_image_timeout' in values:
            values['default_image_timeout'] = _integer(values['default_image_timeout'], 'default_image_timeout', 10, 1800)
        with self.transaction() as conn:
            for name, value in values.items():
                if conn.execute(select(options.c.name).where(options.c.name == name)).first():
                    conn.execute(update(options).where(options.c.name == name).values(value_json=_json(value)))
                else:
                    conn.execute(insert(options).values(name=name, value_json=_json(value)))
        return self.get_settings()

    @staticmethod
    def _password(password: str, salt: bytes | None = None) -> str:
        salt = salt or secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 310_000)
        return 'pbkdf2_sha256$310000$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(derived).decode()

    @classmethod
    def _password_matches(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, iterations, salt, digest = encoded.split('$')
            if algorithm != 'pbkdf2_sha256' or iterations != '310000':
                return False
            actual = cls._password(password, base64.b64decode(salt))
            return hmac.compare_digest(actual, encoded)
        except (ValueError, TypeError):
            return False

    def bootstrap_admin(self, username: str, password: str, *, minimum_length: int = 12) -> bool:
        username = _string(username, 'username', 80)
        if type(minimum_length) is not int or not 8 <= minimum_length <= 128:
            raise PlatformError('Administrator minimum password length must be between 8 and 128.')
        if not isinstance(password, str) or len(password) < minimum_length or len(password) > 1024:
            raise PlatformError(f'Administrator password must contain {minimum_length} to 1024 characters.')
        with self.engine.connect() as conn:
            if conn.execute(select(admins.c.username)).first():return False
        encoded=self._password(password)
        with self.transaction() as conn:
            if conn.execute(select(admins.c.username)).first():
                return False
            conn.execute(insert(admins).values(username=username, password_hash=encoded, created_at=self.clock()))
        return True

    def login_admin(self, username: str, password: str, *, client_id: str = '', lifetime: int = 28800) -> dict:
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 1024:
            raise PlatformError('Invalid administrator credentials.', code='invalid_credentials', status=401)
        now, attempt_id = self.clock(), _hash(client_id + '|' + username)
        with self.transaction() as conn:
            attempt = conn.execute(select(login_attempts).where(login_attempts.c.id == attempt_id)).mappings().first()
            if attempt and attempt['window_start'] > now - 60 and attempt['count'] >= 10:
                raise PlatformError('Too many login attempts; try later.', code='rate_limit_exceeded', status=429)
            if attempt and attempt['window_start'] > now - 60:
                conn.execute(update(login_attempts).where(login_attempts.c.id == attempt_id).values(count=attempt['count'] + 1))
            elif attempt:
                conn.execute(update(login_attempts).where(login_attempts.c.id == attempt_id).values(count=1, window_start=now))
            else:
                conn.execute(insert(login_attempts).values(id=attempt_id, count=1, window_start=now))
            row = conn.execute(select(admins).where(admins.c.username == username)).mappings().first()
            encoded=row['password_hash'] if row else None
            conn.execute(delete(login_attempts).where(login_attempts.c.window_start<now-86400))
        # Expensive work is deliberately outside the scheduler transaction.
        # Missing users pay the same PBKDF2 cost; no timing shortcut.
        candidate=encoded or 'pbkdf2_sha256$310000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
        matches=self._password_matches(password,candidate);valid=bool(encoded and matches)
        if valid:
            with self.transaction() as conn:
                current=conn.execute(select(admins.c.password_hash).where(admins.c.username==username)).scalar_one_or_none()
                if current!=encoded:raise PlatformError('Invalid administrator credentials.',code='invalid_credentials',status=401)
                conn.execute(delete(login_attempts).where(login_attempts.c.id == attempt_id))
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                conn.execute(delete(sessions).where(sessions.c.expires_at <= now))
                conn.execute(insert(sessions).values(token_hash=_hash(token), username=username, csrf_token=csrf, expires_at=now + lifetime))
        if not valid:
            raise PlatformError('Invalid administrator credentials.', code='invalid_credentials', status=401)
        return {'token': token, 'user': {'username': username}, 'csrf_token': csrf, 'expires_at': now + lifetime}

    def authenticate_admin(self, token: str) -> dict:
        if not isinstance(token, str) or not token or len(token) > 256:
            raise PlatformError('Administrator login required.', code='authentication_required', status=401)
        with self.engine.connect() as conn:
            row = conn.execute(select(sessions).where(sessions.c.token_hash == _hash(token), sessions.c.expires_at > self.clock())).mappings().first()
        if row is None:
            raise PlatformError('Administrator login required.', code='authentication_required', status=401)
        return {'user': {'username': row['username']}, 'csrf_token': row['csrf_token'], 'expires_at': row['expires_at']}

    def logout_admin(self, token: str) -> None:
        with self.transaction() as conn:
            conn.execute(delete(sessions).where(sessions.c.token_hash == _hash(token or '')))
