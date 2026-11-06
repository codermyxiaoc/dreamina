"""Encrypted proxy inventory and durable account binding transitions."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import uuid

from sqlalchemy import Boolean,Column,Float,Integer,String,Table,Text,func,insert,select,update
from cryptography.fernet import InvalidToken

from .egress import Egress,EgressError,ProxySpec,direct_egress,parse_proxy_url,valid_timezone


def register_proxy_tables(metadata):
    proxies=Table('platform_proxies',metadata,
        Column('id',String(64),primary_key=True),Column('name',String(120),nullable=False),Column('notes',Text,nullable=False),
        Column('protocol',String(16),nullable=False),Column('host',String(253),nullable=False),Column('port',Integer,nullable=False),
        Column('username',Text,nullable=False),Column('password_encrypted',Text),Column('fingerprint',String(64),unique=True),
        Column('status',String(20),nullable=False),Column('max_accounts',Integer,nullable=False),Column('expires_at',Float),
        Column('expiry_warn_days',Integer,nullable=False),Column('backup_proxy_id',String(64)),Column('fallback_mode',String(16),nullable=False),
        Column('ip_change_policy',String(16),nullable=False,server_default='warn'),
        Column('exit_ip',String(64)),Column('country',String(2)),Column('region',String(120)),Column('city',String(120)),Column('timezone',String(80)),
        Column('latency_ms',Float),Column('last_checked_at',Float),Column('last_error',String(80)),Column('consecutive_failures',Integer,nullable=False),
        Column('consecutive_successes',Integer,nullable=False),Column('created_at',Float,nullable=False),Column('updated_at',Float,nullable=False),Column('deleted_at',Float))
    events=Table('platform_proxy_events',metadata,Column('id',String(64),primary_key=True),Column('proxy_id',String(64)),
        Column('account_id',String(64)),Column('action',String(80),nullable=False),Column('detail_json',Text,nullable=False),Column('created_at',Float,nullable=False))
    changes=Table('platform_proxy_changes',metadata,Column('id',String(64),primary_key=True),Column('account_id',String(64),nullable=False),
        Column('proxy_id',String(64)),Column('state',String(20),nullable=False),Column('previous_json',Text,nullable=False),
        Column('allow_capacity',Boolean,nullable=False),Column('confirm_country',Boolean,nullable=False),Column('created_at',Float,nullable=False),Column('updated_at',Float,nullable=False))
    return proxies,events,changes


def _db():
    from .platform_store import accounts,tasks,ACTIVE_STATES,PlatformError,proxies,proxy_events,proxy_changes
    return accounts,tasks,ACTIVE_STATES,PlatformError,proxies,proxy_events,proxy_changes


def _error(message,code='invalid_proxy',status=400):
    raise _db()[3](message,code=code,status=status)


def _text(value,label,maximum=120,empty=False):
    if not isinstance(value,str) or len(value)>maximum or (not empty and not value.strip()) or any(ord(c)<32 and c not in '\n\t' for c in value):_error('Invalid '+label+'.')
    return value.strip()


def _int(value,label,lo,hi):
    if type(value) is not int or not lo<=value<=hi:_error('Invalid '+label+'.')
    return value


class ProxyStoreMixin:
    def _proxy_fingerprint(self,spec):
        # A deterministic keyed digest permits deduplication without turning
        # a database-only leak into a password-guessing oracle for known hosts.
        return hmac.new(self._profile_master_key,b'proxy-identity\0'+spec.canonical_url.encode('utf-8'),hashlib.sha256).hexdigest()

    def _proxy_event(self,conn,action,*,proxy_id=None,account_id=None,detail=None):
        from .audit import sanitize
        events=_db()[5]
        conn.execute(insert(events).values(id=uuid.uuid4().hex,proxy_id=proxy_id,account_id=account_id,action=action,
            detail_json=json.dumps(sanitize(detail or {}),ensure_ascii=False),created_at=self.clock()))

    def _proxy_spec(self,row):
        try:password=self._fernet.decrypt(row['password_encrypted'].encode()).decode() if row['password_encrypted'] else ''
        except (InvalidToken,ValueError,UnicodeError):_error('Proxy credentials cannot be decrypted with this server key.','proxy_key_mismatch',503)
        return ProxySpec(row['protocol'],row['host'],row['port'],row['username'],password)

    def _proxy_public(self,row,count=0,historical_count=0):
        value={k:row[k] for k in ('id','name','notes','protocol','host','port','status','max_accounts','expires_at','expiry_warn_days','backup_proxy_id','fallback_mode','ip_change_policy',
            'exit_ip','country','region','city','timezone','latency_ms','last_checked_at','last_error','consecutive_failures','consecutive_successes','created_at','updated_at','deleted_at')}
        value.update(has_username=bool(row['username']),has_password=bool(row['password_encrypted']),bound_accounts_count=count,historical_bindings_count=historical_count,
                     expired=row['expires_at'] is not None and row['expires_at']<=self.clock(),
                     expires_soon=row['expires_at'] is not None and self.clock()<row['expires_at']<=self.clock()+row['expiry_warn_days']*86400,
                     browser_supported=not(row['protocol'] in ('socks5','socks5h') and row['username']),
                     ip_known=bool(row['exit_ip']) and row['last_error'] is None,
                     warning_code='proxy_ip_unavailable' if row['last_error']=='proxy_ip_unavailable' else None)
        return value

    def list_proxies(self):
        a,_,_,_,p,_,_=_db()
        with self.engine.connect() as conn:
            counts=dict(conn.execute(select(a.c.proxy_id,func.count()).where(a.c.proxy_id.is_not(None),a.c.deleted_at.is_(None)).group_by(a.c.proxy_id)).all())
            historical=dict(conn.execute(select(a.c.proxy_id,func.count()).where(a.c.proxy_id.is_not(None),a.c.deleted_at.is_not(None)).group_by(a.c.proxy_id)).all())
            return [self._proxy_public(r,counts.get(r['id'],0),historical.get(r['id'],0)) for r in conn.execute(select(p).where(p.c.deleted_at.is_(None)).order_by(p.c.created_at,p.c.id)).mappings()]

    def get_proxy(self,proxy_id):
        a,_,_,_,p,_,_=_db()
        with self.engine.connect() as conn:
            row=conn.execute(select(p).where(p.c.id==proxy_id,p.c.deleted_at.is_(None))).mappings().first()
            if row is None:_error('Proxy not found.','proxy_not_found',404)
            count=conn.execute(select(func.count()).select_from(a).where(a.c.proxy_id==proxy_id,a.c.deleted_at.is_(None))).scalar_one()
            historical=conn.execute(select(func.count()).select_from(a).where(a.c.proxy_id==proxy_id,a.c.deleted_at.is_not(None))).scalar_one()
            return self._proxy_public(row,count,historical)

    def proxy_accounts(self,proxy_id):
        a,_,_,_,_,_,_=_db();self.get_proxy(proxy_id)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(a.c.id,a.c.name,a.c.enabled,a.c.status,a.c.proxy_exit_ip_at_bind).where(a.c.proxy_id==proxy_id,a.c.deleted_at.is_(None))).mappings()]

    def _proxy_values(self,data,existing=None):
        allowed={'name','url','notes','max_accounts','expires_at','expiry_warn_days','backup_proxy_id','fallback_mode','status','ip_change_policy'}
        if not isinstance(data,dict) or set(data)-allowed:_error('Unknown proxy configuration fields.')
        now=self.clock();v={}
        if existing is None:
            if 'url' not in data:_error('A proxy URL is required.')
            v.update(id='proxy_'+uuid.uuid4().hex,name='Proxy',notes='',status='active',max_accounts=3,expires_at=None,expiry_warn_days=3,
                     backup_proxy_id=None,fallback_mode='none',ip_change_policy='warn',consecutive_failures=0,consecutive_successes=0,created_at=now,updated_at=now)
        if 'url' in data:
            try:spec=parse_proxy_url(data['url'])
            except EgressError as e:_error(str(e),e.code,e.status)
            v.update(protocol=spec.protocol,host=spec.host,port=spec.port,username=spec.username,
                password_encrypted=self._fernet.encrypt(spec.password.encode()).decode() if spec.password else None,fingerprint=self._proxy_fingerprint(spec),
                exit_ip=None,country=None,region=None,city=None,timezone=None,latency_ms=None,last_checked_at=None,last_error=None,
                consecutive_failures=0,consecutive_successes=0)
        for name,maxlen in (('name',120),('notes',2000)):
            if name in data:v[name]=_text(data[name],name,maxlen,empty=name=='notes')
        for name,lo,hi in (('max_accounts',1,1000),('expiry_warn_days',0,365)):
            if name in data:v[name]=_int(data[name],name,lo,hi)
        if 'expires_at' in data:
            x=data['expires_at']
            if x is not None and (type(x) not in (int,float) or not math.isfinite(x) or x<=0):_error('Invalid proxy expiry time.')
            v['expires_at']=x
        if 'status' in data:
            if data['status'] not in ('active','disabled'):_error('Proxy status must be active or disabled.')
            v['status']=data['status']
        if 'ip_change_policy' in data:
            if data['ip_change_policy'] not in ('warn','pause'):_error('IP change policy must be warn or pause.')
            v['ip_change_policy']=data['ip_change_policy']
        if 'fallback_mode' in data:
            if data['fallback_mode'] not in ('none','proxy'):_error('Fallback must be none or proxy; direct fallback is not supported.')
            v['fallback_mode']=data['fallback_mode']
        if 'backup_proxy_id' in data:
            x=data['backup_proxy_id']
            if x is not None and (not isinstance(x,str) or len(x)>64):_error('Invalid backup proxy identity.')
            v['backup_proxy_id']=x
        combined=dict(existing or {},**v)
        if combined['fallback_mode']=='proxy' and not combined.get('backup_proxy_id'):_error('Proxy fallback requires a backup proxy.')
        v['updated_at']=now;return v

    def _check_backup(self,conn,proxy_id,backup_id):
        p=_db()[4];seen={proxy_id};current=backup_id
        while current:
            if current in seen:_error('Backup proxy chain cannot contain a cycle.','proxy_backup_cycle')
            seen.add(current)
            row=conn.execute(select(p.c.backup_proxy_id,p.c.deleted_at).where(p.c.id==current)).first()
            if row is None or row.deleted_at is not None:_error('Backup proxy not found.','proxy_not_found',404)
            current=row.backup_proxy_id

    def _create_proxy_in_transaction(self,conn,values):
        a,_,_,_,p,_,_=_db()
        same=conn.execute(select(p).where(p.c.fingerprint==values['fingerprint'],p.c.deleted_at.is_(None))).mappings().first()
        if same:
            count=conn.execute(select(func.count()).select_from(a).where(a.c.proxy_id==same['id'],a.c.deleted_at.is_(None))).scalar_one()
            historical=conn.execute(select(func.count()).select_from(a).where(a.c.proxy_id==same['id'],a.c.deleted_at.is_not(None))).scalar_one()
            return dict(self._proxy_public(same,count,historical),deduplicated=True)
        self._check_backup(conn,values['id'],values.get('backup_proxy_id'))
        conn.execute(insert(p).values(**values));self._proxy_event(conn,'proxy_created',proxy_id=values['id'])
        return self._proxy_public(conn.execute(select(p).where(p.c.id==values['id'])).mappings().one())

    def create_proxy(self,data):
        values=self._proxy_values(data)
        with self.transaction() as conn:return self._create_proxy_in_transaction(conn,values)

    def batch_create_proxies(self,lines):
        if not isinstance(lines,str) or len(lines)>200_000:_error('Proxy batch must be at most 200000 characters.')
        entries=[s.strip() for s in lines.splitlines() if s.strip()]
        if not 1<=len(entries)<=200:_error('Import between 1 and 200 proxy lines.')
        # Validate all lines before any writes; no error may echo a credential.
        for index,line in enumerate(entries):
            try:parse_proxy_url(line)
            except EgressError:_error(f'Proxy on line {index+1} is invalid.','invalid_proxy')
        values=[self._proxy_values({'url':line,'name':'Imported proxy '+str(i+1)}) for i,line in enumerate(entries)]
        with self.transaction() as conn:return {'items':[self._create_proxy_in_transaction(conn,value) for value in values]}

    def update_proxy(self,proxy_id,data):
        a,_,_,_,p,_,_=_db()
        with self.transaction() as conn:
            row=conn.execute(select(p).where(p.c.id==proxy_id,p.c.deleted_at.is_(None))).mappings().first()
            if row is None:_error('Proxy not found.','proxy_not_found',404)
            v=self._proxy_values(data,row)
            if v.get('fingerprint')==row['fingerprint']:
                for key in ('protocol','host','port','username','password_encrypted','fingerprint','exit_ip','country','region','city','timezone','latency_ms','last_checked_at','last_error','consecutive_failures','consecutive_successes'):v.pop(key,None)
            if 'fingerprint' in v and v['fingerprint']!=row['fingerprint']:
                if conn.execute(select(a.c.id).where(a.c.proxy_id==proxy_id)).first():_error('Create a new proxy and use the account proxy-change flow before changing a bound endpoint.','proxy_in_use',409)
                if conn.execute(select(p.c.id).where(p.c.fingerprint==v['fingerprint'],p.c.id!=proxy_id,p.c.deleted_at.is_(None))).first():_error('This proxy already exists.','proxy_duplicate',409)
            self._check_backup(conn,proxy_id,v.get('backup_proxy_id',row['backup_proxy_id']))
            conn.execute(update(p).where(p.c.id==proxy_id).values(**v));self._proxy_event(conn,'proxy_updated',proxy_id=proxy_id,detail={'fields':sorted(data.keys()-{'url'})})
        return self.get_proxy(proxy_id)

    def delete_proxy(self,proxy_id):
        a,_,_,_,p,_,_= _db()
        with self.transaction() as conn:
            row=conn.execute(select(p).where(p.c.id==proxy_id)).mappings().first()
            if row is None:_error('Proxy not found.','proxy_not_found',404)
            if conn.execute(select(a.c.id).where(a.c.proxy_id==proxy_id)).first() or conn.execute(select(p.c.id).where(p.c.backup_proxy_id==proxy_id,p.c.deleted_at.is_(None))).first():_error('A bound, historically referenced or backup proxy cannot be deleted.','proxy_in_use',409)
            conn.execute(update(p).where(p.c.id==proxy_id).values(status='disabled',deleted_at=row['deleted_at'] or self.clock(),fingerprint=None,updated_at=self.clock()))
            self._proxy_event(conn,'proxy_deleted',proxy_id=proxy_id)
        return {'id':proxy_id,'deleted':True}

    def proxy_probe_spec(self,proxy_id):
        p=_db()[4]
        with self.engine.connect() as conn:
            row=conn.execute(select(p).where(p.c.id==proxy_id,p.c.deleted_at.is_(None))).mappings().first()
            if row is None:_error('Proxy not found.','proxy_not_found',404)
            return self._proxy_spec(row)

    def proxy_probe_context(self,proxy_id):
        p=_db()[4]
        with self.engine.connect() as conn:
            row=conn.execute(select(p).where(p.c.id==proxy_id,p.c.deleted_at.is_(None))).mappings().first()
            if row is None:_error('Proxy not found.','proxy_not_found',404)
            return self._proxy_spec(row),(row['fingerprint'],row['updated_at'])

    def record_proxy_probe(self,proxy_id,result,*,expected_revision=None):
        a,_,_,_,p,_,_=_db()
        if not isinstance(result,dict) or type(result.get('ok')) is not bool:_error('Invalid proxy probe result.')
        now=self.clock();v={'last_checked_at':now,'updated_at':now}
        with self.transaction() as conn:
            row=conn.execute(select(p).where(p.c.id==proxy_id,p.c.deleted_at.is_(None))).mappings().first()
            if row is None:_error('Proxy not found.','proxy_not_found',404)
            if expected_revision is not None and (row['fingerprint'],row['updated_at'])!=tuple(expected_revision):_error('Proxy changed during its probe; retry the current configuration.','proxy_probe_stale',409)
            if result['ok']:
                try:ip=str(ipaddress.ip_address(result['exit_ip'])) if result.get('exit_ip') is not None else None
                except (ValueError,KeyError,TypeError):_error('Probe did not return a valid exit IP.')
                if ip is None and result.get('ip_known') is not False:_error('Probe did not return a valid exit IP.')
                latency=result.get('latency_ms')
                if type(latency) not in (int,float) or not math.isfinite(latency) or latency<0:_error('Probe latency is invalid.')
                v.update(latency_ms=float(latency),last_error=None if ip else 'proxy_ip_unavailable',consecutive_failures=0,consecutive_successes=row['consecutive_successes']+1)
                if ip:v['exit_ip']=ip
                if ip and row['exit_ip'] and ip!=row['exit_ip']:v.update(country=None,region=None,city=None,timezone=None)
                country=result.get('country')
                if ip and country is not None:
                    if not isinstance(country,str) or len(country)!=2 or not country.isascii() or not country.isalpha():_error('Probe country must be a two-letter code.')
                    v['country']=country.upper()
                for field in ('region','city'):
                    if ip and result.get(field) is not None:v[field]=_text(result[field],field,120,empty=True)
                if ip and result.get('timezone') is not None:
                    try:v['timezone']=valid_timezone(result['timezone'])
                    except EgressError:_error('Probe returned an invalid timezone.')
                if row['status']=='failing' and v['consecutive_successes']>=2:v['status']='active'
                if ip and row['exit_ip'] and row['exit_ip']!=ip:self._proxy_event(conn,'proxy_exit_changed',proxy_id=proxy_id,detail={'previous_ip':row['exit_ip'],'exit_ip':ip,'policy':row['ip_change_policy']})
            else:
                code=result.get('error_code','proxy_probe_failed')
                if code not in ('proxy_connect_failed','proxy_auth_failed','proxy_probe_failed','proxy_ip_unavailable','proxy_target_unavailable','proxy_dns_failed','proxy_dependency_missing'):code='proxy_probe_failed'
                v.update(last_error=code,consecutive_failures=row['consecutive_failures']+1,consecutive_successes=0)
                if row['status']!='disabled' and v['consecutive_failures']>=3:v['status']='failing'
            conn.execute(update(p).where(p.c.id==proxy_id).values(**v));self._proxy_event(conn,'proxy_probe',proxy_id=proxy_id,detail={'ok':result['ok'],'error_code':v.get('last_error')})
        return self.get_proxy(proxy_id)

    def _proxy_unavailable(self,row,*,binding=False):
        if row is None or row['deleted_at'] is not None:return 'proxy_missing'
        if row['status']=='disabled':return 'proxy_disabled'
        if row['status']=='failing':return 'proxy_unhealthy'
        if row['expires_at'] is not None and row['expires_at']<=self.clock():return 'proxy_expired'
        if not row['last_checked_at'] or not row['exit_ip']:return 'proxy_unverified'
        if binding and row['consecutive_failures']>0:return 'proxy_unhealthy'
        if binding and row['last_error']=='proxy_ip_unavailable':return 'proxy_ip_unavailable'
        if row['protocol'] in ('socks5','socks5h') and row['username']:return 'browser_proxy_unsupported'
        if binding and not row['timezone']:return 'proxy_timezone_unknown'
        return None

    def proxy_scheduling_reason(self,conn,account):
        p=_db()[4]
        if account['status']=='proxy_changing':return 'proxy_change_in_progress'
        if account.get('proxy_migration_error'):return account['proxy_migration_error']
        if not account.get('proxy_id'):return 'proxy_required' if self.account_proxy_required else None
        row=conn.execute(select(p).where(p.c.id==account['proxy_id'])).mappings().first();reason=self._proxy_unavailable(row)
        if reason:return reason
        if not account.get('proxy_exit_ip_at_bind') or not account.get('browser_timezone'):return 'proxy_binding_unverified'
        if row['ip_change_policy']=='pause' and account.get('proxy_exit_ip_at_bind') and account['proxy_exit_ip_at_bind']!=row['exit_ip']:return 'proxy_exit_changed'
        return None

    def account_egress_public_fields(self,conn,account):
        p=_db()[4];pid=account.get('proxy_id');row=conn.execute(select(p).where(p.c.id==pid)).mappings().first() if pid else None
        reason=self.proxy_scheduling_reason(conn,account)
        changes=_db()[6]
        change=conn.execute(select(changes).where(changes.c.account_id==account['id'],changes.c.state.in_(('pending','checking','restarting'))).order_by(changes.c.created_at).limit(1)).mappings().first()
        retry=json.loads(change['previous_json']).get('verification_retry',{}) if change else {}
        return {'proxy_id':pid,'proxy_bound_at':account.get('proxy_bound_at'),'proxy_exit_ip_at_bind':account.get('proxy_exit_ip_at_bind'),
            'browser_timezone':account.get('browser_timezone'),'browser_locale':account.get('browser_locale') or 'en-US',
            'proxy_required':self.account_proxy_required,'egress_group':'proxy-'+pid if pid else 'direct',
            'proxy_name':row['name'] if row else None,'proxy_exit_ip':row['exit_ip'] if row else None,'proxy_country':row['country'] if row else None,
            'proxy_status':row['status'] if row else None,'proxy_scheduling_error':reason,
            'proxy_ip_change_policy':row['ip_change_policy'] if row else None,
            'proxy_ip_changed':bool(row and account.get('proxy_exit_ip_at_bind') and account['proxy_exit_ip_at_bind']!=row['exit_ip']),
            'proxy_change_state':change['state'] if change else None,'proxy_retry_at':retry.get('at'),
            'proxy_retry_count':retry.get('count',0),'proxy_change_error_code':retry.get('error_code')}

    def resolve_account_egress(self,account_id,*,for_existing_task=False):
        a,_,_,_,p,_,_=_db()
        if type(for_existing_task) is not bool:_error('Existing task egress mode must be boolean.')
        with self.engine.connect() as conn:
            row=conn.execute(select(a).where(a.c.id==account_id)).mappings().first()
            if row is None:_error('Account not found.','not_found',404)
            if not row['proxy_id'] and self._decrypt(row['secret_encrypted']).get('proxy'):
                _error('Legacy proxy configuration must be migrated and verified before use. Direct fallback is not permitted.','proxy_migration_pending',503)
            reason=self.proxy_scheduling_reason(conn,row)
            read_continuation=for_existing_task and reason in ('proxy_unhealthy','proxy_expired','proxy_exit_changed')
            # A proxy change is intentionally disabled for scheduling, but the
            # pending check must still be able to use its newly bound egress.
            if reason and reason!='proxy_change_in_progress' and not read_continuation:_error('Account proxy is unavailable: '+reason+'. No direct fallback is permitted.',reason,503)
            if not row['proxy_id']:
                if self.account_proxy_required:_error('A managed proxy is required.','proxy_required',503)
                return direct_egress(timezone=row['browser_timezone'],locale=row['browser_locale'] or 'en-US')
            proxy=conn.execute(select(p).where(p.c.id==row['proxy_id'])).mappings().first()
            # Existing tasks may continue read-only requests through their
            # SAME endpoint while new work is paused. Never choose a backup or
            # direct connection, and never override a manual disable/deletion.
            check=dict(proxy) if proxy is not None else None
            if check is not None and for_existing_task:
                if check['status']=='failing':check['status']='active'
                check['expires_at']=None
            unavailable=self._proxy_unavailable(check)
            if unavailable:_error('The bound proxy is unavailable; direct fallback is not permitted.',unavailable,503)
            if not row['proxy_exit_ip_at_bind'] or not row['browser_timezone']:_error('The account proxy binding must be verified before use.','proxy_binding_unverified',503)
            if not for_existing_task and proxy['ip_change_policy']=='pause' and row['proxy_exit_ip_at_bind'] and row['proxy_exit_ip_at_bind']!=proxy['exit_ip']:_error('The proxy exit IP changed; confirm a safe rebinding before use.','proxy_exit_changed',503)
            spec=self._proxy_spec(proxy)
            return Egress(proxy['id'],spec.canonical_url,spec.browser_proxy(),'proxy-'+proxy['id'],row['browser_timezone'],row['browser_locale'] or 'en-US',proxy['fingerprint'])

    def _proxy_bind_check(self,conn,account,proxy_id,*,confirm_country_change=False,confirm_capacity_override=False):
        a,_,_,_,p,_,_=_db()
        if proxy_id is None:
            if self.account_proxy_required:_error('This server requires a proxy for every account.','proxy_required',409)
            return None
        proxy=conn.execute(select(p).where(p.c.id==proxy_id)).mappings().first();reason=self._proxy_unavailable(proxy,binding=True)
        if reason:_error('The selected proxy must pass a probe before binding: '+reason+'.',reason,409)
        old=conn.execute(select(p).where(p.c.id==account.get('proxy_id'))).mappings().first() if account.get('proxy_id') else None
        if old and old['ip_change_policy']=='pause' and account.get('proxy_exit_ip_at_bind') and account['proxy_exit_ip_at_bind']!=old['exit_ip'] and not confirm_country_change:
            _error('The bound proxy exit IP changed; explicit confirmation is required before rebinding.','proxy_country_confirmation_required',409)
        if old and old['country'] and proxy['country'] and old['country']!=proxy['country'] and not confirm_country_change:_error('Proxy country changed; explicit confirmation is required.','proxy_country_confirmation_required',409)
        count=conn.execute(select(func.count()).select_from(a).where(a.c.proxy_id==proxy_id,a.c.deleted_at.is_(None),a.c.id!=account['id'])).scalar_one()
        if count>=proxy['max_accounts'] and not confirm_capacity_override:_error('Proxy account capacity exceeded; explicit confirmation is required.','proxy_capacity_confirmation_required',409)
        return proxy

    def _proxy_idle(self,conn,row):
        _,t,active,_,_,_,_=_db()
        if row['status'] in ('login_pending','proxy_changing','deleting','deleted'):_error('Account is busy with login or another change.','account_busy',409)
        if conn.execute(select(t.c.id).where(t.c.account_id==row['id'],t.c.status.in_(active))).first():_error('Finish or reconcile all account tasks before changing egress.','account_busy',409)

    def create_account_with_proxy(self,data,proxy_id,*,confirm_capacity_override=False):
        a,_,_,_,_,_,_=_db()
        if type(confirm_capacity_override) is not bool:_error('Proxy confirmation must be boolean.')
        if not isinstance(data,dict) or data.get('proxy'):_error('Use a managed proxy ID, not an account proxy URL.')
        values=self._account_values(data)
        with self.transaction() as conn:
            if conn.execute(select(a.c.id).where(a.c.id==values['id'])).first():_error('Account ID already exists.','conflict',409)
            proxy=self._proxy_bind_check(conn,values,proxy_id,confirm_capacity_override=confirm_capacity_override)
            values.update(proxy_id=proxy_id,proxy_bound_at=self.clock() if proxy else None,proxy_exit_ip_at_bind=proxy['exit_ip'] if proxy else None,
                browser_timezone=proxy['timezone'] if proxy else None,browser_locale='en-US')
            conn.execute(insert(a).values(**values));self._proxy_event(conn,'account_created_with_proxy',proxy_id=proxy_id,account_id=values['id'])
        return self.get_account(values['id'])

    def begin_proxy_change(self,account_id,proxy_id,*,confirm_country_change=False,confirm_capacity_override=False,previous_running=False):
        a,_,_,_,_,_,changes=_db()
        if any(type(v) is not bool for v in (confirm_country_change,confirm_capacity_override,previous_running)):_error('Proxy confirmations must be booleans.')
        with self.transaction() as conn:
            row=conn.execute(select(a).where(a.c.id==account_id,a.c.deleted_at.is_(None))).mappings().first()
            if row is None:_error('Account not found.','not_found',404)
            self._proxy_idle(conn,row);self._proxy_bind_check(conn,row,proxy_id,confirm_country_change=confirm_country_change,confirm_capacity_override=confirm_capacity_override)
            cid='pc_'+uuid.uuid4().hex;previous={k:row[k] for k in ('enabled','status','proxy_id','proxy_bound_at','proxy_exit_ip_at_bind','browser_timezone','browser_locale','proxy_migration_error')}
            previous['previous_running']=previous_running
            conn.execute(insert(changes).values(id=cid,account_id=account_id,proxy_id=proxy_id,state='pending',previous_json=json.dumps(previous),allow_capacity=confirm_capacity_override,confirm_country=confirm_country_change,created_at=self.clock(),updated_at=self.clock()))
            conn.execute(update(a).where(a.c.id==account_id).values(enabled=False,status='proxy_changing',updated_at=self.clock()))
            self._proxy_event(conn,'proxy_change_started',proxy_id=proxy_id,account_id=account_id,detail={'change_id':cid,'previous_proxy_id':row['proxy_id']})
            return {'id':cid,'account_id':account_id,'proxy_id':proxy_id,'previous_enabled':previous['enabled'],'previous_running':previous_running,'state':'pending'}

    @staticmethod
    def _public_proxy_change(row):
        previous=json.loads(row['previous_json'])
        retry=previous.get('verification_retry',{})
        return {k:row[k] for k in ('id','account_id','proxy_id','state','created_at','updated_at')}|{
            'previous_enabled':bool(previous.get('enabled')),'previous_running':bool(previous.get('previous_running')),
            'confirm_country':bool(row['confirm_country']),'allow_capacity':bool(row['allow_capacity']),
            'retry_at':retry.get('at'),'retry_count':retry.get('count',0),'error_code':retry.get('error_code')}

    def defer_proxy_verification(self,change_id):
        """Persist retries without treating transport uncertainty as logout."""
        a,_,_,_,_,_,changes=_db()
        with self.transaction() as conn:
            change=conn.execute(select(changes).where(changes.c.id==change_id)).mappings().first()
            if change is None or change['state']!='checking':_error('Proxy change is not awaiting verification.','proxy_change_conflict',409)
            row=conn.execute(select(a).where(a.c.id==change['account_id'])).mappings().one()
            if row['proxy_id']!=change['proxy_id'] or row['enabled']:_error('Account changed during verification.','proxy_change_conflict',409)
            previous=json.loads(change['previous_json']);count=min(int(previous.get('verification_retry',{}).get('count',0))+1,1000)
            previous['verification_retry']={'count':count,'at':self.clock()+min(300,5*(2**min(count-1,6))),'error_code':'account_proxy_check_retry'}
            conn.execute(update(changes).where(changes.c.id==change_id).values(previous_json=json.dumps(previous),updated_at=self.clock()))
            conn.execute(update(a).where(a.c.id==row['id']).values(status='unverified',updated_at=self.clock()))
            self._proxy_event(conn,'proxy_verification_deferred',proxy_id=change['proxy_id'],account_id=row['id'],detail={'change_id':change_id,'retry_count':count})
            return self._public_proxy_change(dict(change,previous_json=json.dumps(previous)))

    def get_proxy_change(self,change_id):
        changes=_db()[6]
        with self.engine.connect() as conn:
            row=conn.execute(select(changes).where(changes.c.id==change_id)).mappings().first()
            if row is None:_error('Proxy change not found.','proxy_change_conflict',404)
            return self._public_proxy_change(row)

    def list_proxy_changes(self):
        changes=_db()[6]
        with self.engine.connect() as conn:
            return [self._public_proxy_change(r) for r in conn.execute(select(changes).where(changes.c.state.in_(('pending','checking','restarting'))).order_by(changes.c.created_at).limit(1000)).mappings()]

    def pending_proxy_change(self,account_id):
        changes=_db()[6]
        with self.engine.connect() as conn:
            row=conn.execute(select(changes).where(changes.c.account_id==account_id,changes.c.state.in_(('pending','checking','restarting'))).order_by(changes.c.created_at).limit(1)).mappings().first()
            return self._public_proxy_change(row) if row else None

    def commit_proxy_change(self,change_id):
        a,t,active,_,_,_,changes=_db()
        with self.transaction() as conn:
            change=conn.execute(select(changes).where(changes.c.id==change_id)).mappings().first()
            if change is None or change['state'] not in ('pending','checking'):_error('Proxy change is not pending.','proxy_change_conflict',409)
            row=conn.execute(select(a).where(a.c.id==change['account_id'])).mappings().one()
            if change['state']=='checking':return {'id':change_id,'account_id':row['id'],'proxy_id':row['proxy_id']}
            if row['enabled'] or row['status']!='proxy_changing' or conn.execute(select(t.c.id).where(t.c.account_id==row['id'],t.c.status.in_(active))).first():_error('Account changed while stopping its worker.','proxy_change_conflict',409)
            proxy=self._proxy_bind_check(conn,row,change['proxy_id'],confirm_country_change=change['confirm_country'],confirm_capacity_override=change['allow_capacity'])
            v={'proxy_id':change['proxy_id'],'proxy_bound_at':self.clock(),'proxy_exit_ip_at_bind':proxy['exit_ip'] if proxy else None,
                'browser_timezone':proxy['timezone'] if proxy else None,'browser_locale':'en-US','proxy_migration_error':None,'status':'unverified','balance':None,'last_checked_at':None,'updated_at':self.clock()}
            conn.execute(update(a).where(a.c.id==row['id']).values(**v));conn.execute(update(changes).where(changes.c.id==change_id).values(state='checking',updated_at=self.clock()))
            self._proxy_event(conn,'proxy_bound',proxy_id=change['proxy_id'],account_id=row['id'],detail={'change_id':change_id})
            return {'id':change_id,'account_id':row['id'],'proxy_id':change['proxy_id']}

    def finish_proxy_change(self,change_id,*,success,error_code=None):
        a,_,_,_,_,_,changes=_db()
        if type(success) is not bool:_error('Proxy change success must be boolean.')
        with self.transaction() as conn:
            change=conn.execute(select(changes).where(changes.c.id==change_id)).mappings().first()
            if change is None:_error('Proxy change not found.','proxy_change_conflict',409)
            if change['state'] in ('completed','failed','restarting'):return {'id':change_id,'account_id':change['account_id'],'success':change['state'] in ('completed','restarting'),'restart_pending':change['state']=='restarting'}
            if change['state']!='checking':_error('Proxy change has not been committed.','proxy_change_conflict',409)
            row=conn.execute(select(a).where(a.c.id==change['account_id'])).mappings().one()
            if row['proxy_id']!=change['proxy_id'] or row['enabled']:_error('Account changed before egress verification.','proxy_change_conflict',409)
            previous=json.loads(change['previous_json'])
            if success and row['status']!='ready':_error('Account session must be checked through its new proxy before enabling it.','proxy_check_required',409)
            if success:
                self._proxy_bind_check(conn,row,change['proxy_id'],confirm_country_change=change['confirm_country'],confirm_capacity_override=change['allow_capacity'])
                reason=self.proxy_scheduling_reason(conn,row)
                if reason:_error('Proxy state changed during account verification.',reason,409)
            conn.execute(update(a).where(a.c.id==row['id']).values(enabled=previous['enabled'] if success else False,status='ready' if success else 'error',updated_at=self.clock()))
            restart=success and previous.get('previous_running') is True and previous['enabled'] is True
            state='restarting' if restart else 'completed' if success else 'failed'
            conn.execute(update(changes).where(changes.c.id==change_id).values(state=state,updated_at=self.clock()))
            self._proxy_event(conn,'proxy_change_restart_pending' if restart else 'proxy_change_completed' if success else 'proxy_change_failed',proxy_id=change['proxy_id'],account_id=row['id'],detail={'change_id':change_id,'error_code':'account_proxy_check_failed' if not success else None})
            return {'id':change_id,'account_id':row['id'],'success':success,'restart_pending':bool(restart)}

    def finish_proxy_restart(self,change_id):
        """Acknowledge a durable worker-start intent only after the supervisor."""
        a,_,_,_,_,_,changes=_db()
        with self.transaction() as conn:
            change=conn.execute(select(changes).where(changes.c.id==change_id)).mappings().first()
            if change is None:_error('Proxy change not found.','proxy_change_conflict',409)
            if change['state']=='completed':return self._public_proxy_change(change)
            if change['state']!='restarting':_error('Proxy change is not awaiting a worker restart.','proxy_change_conflict',409)
            row=conn.execute(select(a).where(a.c.id==change['account_id'])).mappings().one()
            if row['proxy_id']!=change['proxy_id']:_error('Account proxy changed during restart.','proxy_change_conflict',409)
            conn.execute(update(changes).where(changes.c.id==change_id).values(state='completed',updated_at=self.clock()))
            self._proxy_event(conn,'proxy_change_completed',proxy_id=change['proxy_id'],account_id=row['id'],detail={'change_id':change_id,'worker_restart_requested':True})
            return dict(self._public_proxy_change(change),state='completed')

    def finish_proxy_change_for_login(self,change_id):
        """A never-logged-in account can choose egress before entering login."""
        a,_,_,_,_,_,changes=_db()
        with self.transaction() as conn:
            change=conn.execute(select(changes).where(changes.c.id==change_id)).mappings().first()
            if change is None or change['state']!='checking':_error('Proxy change is not awaiting verification.','proxy_change_conflict',409)
            row=conn.execute(select(a).where(a.c.id==change['account_id'])).mappings().one();secrets=self._decrypt(row['secret_encrypted'])
            if row['proxy_id']!=change['proxy_id'] or row['enabled'] or row['browser_profile'] or secrets.get('session_id') or secrets.get('cookie'):_error('Existing logged-in accounts require session validation through the new proxy.','proxy_check_required',409)
            self._proxy_bind_check(conn,row,change['proxy_id'],confirm_country_change=change['confirm_country'],confirm_capacity_override=change['allow_capacity'])
            reason=self.proxy_scheduling_reason(conn,row)
            if reason:_error('Proxy state changed before opening login.',reason,409)
            previous=json.loads(change['previous_json'])
            conn.execute(update(a).where(a.c.id==row['id']).values(enabled=bool(previous['enabled']),status='login_required',updated_at=self.clock()))
            conn.execute(update(changes).where(changes.c.id==change_id).values(state='completed',updated_at=self.clock()))
            self._proxy_event(conn,'proxy_change_login_required',proxy_id=change['proxy_id'],account_id=row['id'],detail={'change_id':change_id})
            return {'id':change_id,'account_id':row['id'],'success':True,'login_required':True}

    def cancel_proxy_change(self,change_id):
        a,_,_,_,_,_,changes=_db()
        with self.transaction() as conn:
            change=conn.execute(select(changes).where(changes.c.id==change_id)).mappings().first()
            if change is None or change['state']!='pending':_error('Only an uncommitted proxy change can be cancelled.','proxy_change_conflict',409)
            row=conn.execute(select(a).where(a.c.id==change['account_id'])).mappings().one()
            if row['status']!='proxy_changing' or row['enabled']:_error('Account changed during proxy operation.','proxy_change_conflict',409)
            previous=json.loads(change['previous_json']);previous.pop('previous_running',None)
            conn.execute(update(a).where(a.c.id==row['id']).values(**previous,updated_at=self.clock()))
            conn.execute(update(changes).where(changes.c.id==change_id).values(state='cancelled',updated_at=self.clock()));self._proxy_event(conn,'proxy_change_cancelled',account_id=row['id'],detail={'change_id':change_id})
            return {'id':change_id,'cancelled':True}

    def proxy_fallback_candidate(self,proxy_id):
        p=_db()[4];seen=set();current=proxy_id
        with self.engine.connect() as conn:
            for _ in range(100):
                if not current or current in seen:return None
                seen.add(current);row=conn.execute(select(p).where(p.c.id==current,p.c.deleted_at.is_(None))).mappings().first()
                if row is None:return None
                if current!=proxy_id and not self._proxy_unavailable(row,binding=True):return current
                if row['fallback_mode']!='proxy':return None
                current=row['backup_proxy_id']
        return None

    def proxy_events(self,limit=100):
        events=_db()[5];_int(limit,'limit',1,500)
        with self.engine.connect() as conn:
            return [{k:r[k] for k in ('id','proxy_id','account_id','action','created_at')}|{'detail':json.loads(r['detail_json'])} for r in conn.execute(select(events).order_by(events.c.created_at.desc()).limit(limit)).mappings()]

    def _migrate_legacy_proxies(self):
        """Idempotent encrypted migration; malformed legacy endpoints stay blocked."""
        a,_,_,_,p,_,_=_db()
        with self.transaction() as conn:
            for account in conn.execute(select(a)).mappings().all():
                try:secret=self._decrypt(account['secret_encrypted'])
                except _db()[3]:continue  # Preserve unreadable secrets; normal access still reports the key mismatch.
                legacy=secret.get('proxy')
                if not legacy:continue
                if account['proxy_id']:
                    # A pending operator binding must not be silently overwritten.
                    continue
                try:spec=parse_proxy_url(legacy)
                except EgressError:
                    conn.execute(update(a).where(a.c.id==account['id']).values(proxy_migration_error='proxy_migration_invalid'))
                    continue
                proxy=conn.execute(select(p).where(p.c.fingerprint==self._proxy_fingerprint(spec),p.c.deleted_at.is_(None))).mappings().first()
                if proxy:pid=proxy['id']
                else:
                    value=self._proxy_values({'url':spec.canonical_url,'name':'Migrated proxy'});pid=value['id'];conn.execute(insert(p).values(**value))
                del secret['proxy']
                conn.execute(update(a).where(a.c.id==account['id']).values(proxy_id=pid,proxy_bound_at=self.clock(),browser_locale='en-US',proxy_migration_error=None,
                    secret_encrypted=self._encrypt(secret) if secret else None,updated_at=self.clock()))
                self._proxy_event(conn,'legacy_proxy_migrated',proxy_id=pid,account_id=account['id'])
