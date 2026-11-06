import json
from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select,update
from dreamina_api.platform_store import PlatformStore,PlatformError,accounts,proxies
from dreamina_api.egress import parse_proxy_url

@pytest.fixture
def env(tmp_path):
    clock=SimpleNamespace(now=1000.0);key=Fernet.generate_key();url='sqlite:///'+str(tmp_path/'proxy.db')
    store=PlatformStore(url,encryption_key=key,clock=lambda:clock.now)
    return SimpleNamespace(store=store,clock=clock,key=key,url=url)

def account(e,aid='a',**kw):return e.store.create_account({'id':aid,'name':aid,'session_id':'private-session-'+aid,'capabilities':['seedance-2.0-mini'],**kw})
def proxy(e,name='p',**kw):
    p=e.store.create_proxy({'name':name,'url':'http://private-user:private-password@'+name+'.example:8080',**kw})
    e.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.10','latency_ms':3,'country':'US','timezone':'America/New_York'})
    return e.store.get_proxy(p['id'])
def bind(e,aid,pid,**kw):
    change=e.store.begin_proxy_change(aid,pid,**kw);e.store.commit_proxy_change(change['id'])
    e.store.record_account_check(aid,status='ready',balance=100)
    e.store.finish_proxy_change(change['id'],success=True)
    return change

def test_secret_encryption_and_public_redaction(env):
    p=proxy(env);public=json.dumps(p)+json.dumps(env.store.list_proxies())+json.dumps(env.store.proxy_events())
    assert 'private-password' not in public and 'private-user' not in public and p['has_password'] and p['has_username']
    with env.store.engine.connect() as c:r=c.execute(select(proxies)).mappings().one()
    assert 'private-password' not in r['password_encrypted']
    assert env.store._fernet.decrypt(r['password_encrypted'].encode())==b'private-password'
    assert r['fingerprint']!=parse_proxy_url('http://private-user:private-password@p.example:8080').fingerprint
    assert env.store.create_proxy({'url':'http://private-user:private-password@p.example:8080'})['deduplicated']

def test_atomic_create_capacity_and_binding_scope(env):
    p=proxy(env,max_accounts=1);account(env);bind(env,'a',p['id'])
    with pytest.raises(PlatformError) as err:env.store.create_account_with_proxy({'id':'b','name':'b'},p['id'])
    assert err.value.code=='proxy_capacity_confirmation_required'
    with pytest.raises(PlatformError):env.store.get_account('b')
    env.store.create_account_with_proxy({'id':'b','name':'b'},p['id'],confirm_capacity_override=True)
    assert env.store.get_proxy(p['id'])['bound_accounts_count']==2
    assert env.store.create_proxy({'url':'http://private-user:private-password@p.example:8080'})['bound_accounts_count']==2
    e=env.store.resolve_account_egress('a');assert e.proxy_id==p['id'] and e.group=='proxy-'+p['id']
    settings=env.store.settings_for_account('a');assert settings.egress==e and settings.account_id=='a'

@pytest.mark.parametrize('status,reason',[('disabled','proxy_disabled'),('failing','proxy_unhealthy')])
def test_bound_unhealthy_proxy_never_falls_back(env,status,reason):
    account(env);p=proxy(env);bind(env,'a',p['id'])
    with env.store.transaction() as c:c.execute(update(proxies).where(proxies.c.id==p['id']).values(status=status))
    with pytest.raises(PlatformError) as error:env.store.resolve_account_egress('a')
    assert error.value.code==reason

def test_required_unbound_and_expiry_and_exit_change(env):
    account(env);env.store.account_proxy_required=True
    with pytest.raises(PlatformError) as error:env.store.resolve_account_egress('a')
    assert error.value.code=='proxy_required'
    p=proxy(env,expires_at=1100,ip_change_policy='pause');bind(env,'a',p['id']);env.clock.now=1200
    with pytest.raises(PlatformError) as error:env.store.resolve_account_egress('a')
    assert error.value.code=='proxy_expired'
    env.store.update_proxy(p['id'],{'expires_at':2000});env.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.20','latency_ms':2,'timezone':'America/New_York'})
    with pytest.raises(PlatformError) as error:env.store.resolve_account_egress('a')
    assert error.value.code=='proxy_exit_changed'
    with pytest.raises(PlatformError) as error:bind(env,'a',p['id'])
    assert error.value.code=='proxy_country_confirmation_required'
    bind(env,'a',p['id'],confirm_country_change=True);assert env.store.resolve_account_egress('a').timezone=='America/New_York'

@pytest.mark.parametrize('issue',['expiry','health','ip','combined'])
def test_existing_task_reads_continue_same_proxy_without_authorizing_new_work(env,issue):
    account(env);p=proxy(env,ip_change_policy='pause');bind(env,'a',p['id']);before=env.store.resolve_account_egress('a')
    changes={}
    if issue in ('expiry','combined'):changes['expires_at']=999
    if issue in ('health','combined'):changes['status']='failing'
    if issue in ('ip','combined'):changes['exit_ip']='203.0.113.25'
    with env.store.transaction() as c:c.execute(update(proxies).where(proxies.c.id==p['id']).values(**changes))
    with pytest.raises(PlatformError):env.store.resolve_account_egress('a')
    continuation=env.store.resolve_account_egress('a',for_existing_task=True)
    assert continuation.proxy_id==before.proxy_id and continuation.httpx_proxy==before.httpx_proxy and continuation.playwright_proxy==before.playwright_proxy
    assert continuation.timezone==before.timezone and continuation.group==before.group

@pytest.mark.parametrize('issue',['disabled','deleted','required-unbound'])
def test_existing_task_read_exception_never_overrides_explicit_disable_or_direct_policy(env,issue):
    account(env);p=proxy(env);bind(env,'a',p['id'])
    with env.store.transaction() as c:
        if issue=='disabled':c.execute(update(proxies).where(proxies.c.id==p['id']).values(status='disabled'))
        elif issue=='deleted':c.execute(update(proxies).where(proxies.c.id==p['id']).values(deleted_at=1000))
        else:c.execute(update(accounts).where(accounts.c.id=='a').values(proxy_id=None));env.store.account_proxy_required=True
    with pytest.raises(PlatformError):env.store.resolve_account_egress('a',for_existing_task=True)

@pytest.mark.parametrize('missing_field',['proxy_exit_ip_at_bind','browser_timezone'])
def test_existing_task_expiry_exception_does_not_hide_unverified_binding(env,missing_field):
    account(env);p=proxy(env);bind(env,'a',p['id'])
    with env.store.transaction() as c:
        c.execute(update(proxies).where(proxies.c.id==p['id']).values(expires_at=999,status='failing'))
        c.execute(update(accounts).where(accounts.c.id=='a').values(**{missing_field:None}))
    with pytest.raises(PlatformError) as error:env.store.resolve_account_egress('a',for_existing_task=True)
    assert error.value.code=='proxy_binding_unverified'

def test_country_change_confirmation_and_pin_timezone(env):
    account(env);first=proxy(env);second=proxy(env,'q')
    env.store.record_proxy_probe(second['id'],{'ok':True,'exit_ip':'203.0.113.30','latency_ms':4,'country':'JP','timezone':'Asia/Tokyo'})
    bind(env,'a',first['id'])
    with pytest.raises(PlatformError) as error:env.store.begin_proxy_change('a',second['id'])
    assert error.value.code=='proxy_country_confirmation_required'
    bind(env,'a',second['id'],confirm_country_change=True)
    assert env.store.resolve_account_egress('a').timezone=='Asia/Tokyo'
    env.store.record_proxy_probe(second['id'],{'ok':True,'exit_ip':'203.0.113.30','latency_ms':4,'timezone':'Europe/London'})
    assert env.store.resolve_account_egress('a').timezone=='Asia/Tokyo'

def test_binding_requires_probe_and_browser_support(env):
    account(env);p=env.store.create_proxy({'url':'http://p.example:80'})
    with pytest.raises(PlatformError) as error:env.store.begin_proxy_change('a',p['id'])
    assert error.value.code=='proxy_unverified'
    p=env.store.create_proxy({'url':'socks5://u:p@p.example:1080'})
    env.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.1','latency_ms':2})
    with pytest.raises(PlatformError) as error:env.store.begin_proxy_change('a',p['id'])
    assert error.value.code=='browser_proxy_unsupported'

def test_proxy_change_durable_gates_and_failed_check(env):
    account(env);p=proxy(env);change=env.store.begin_proxy_change('a',p['id'])
    assert not env.store.get_account('a')['enabled']
    with pytest.raises(PlatformError):env.store.begin_proxy_change('a',p['id'])
    env.store.cancel_proxy_change(change['id']);assert env.store.get_account('a')['enabled']
    change=env.store.begin_proxy_change('a',p['id']);env.store.commit_proxy_change(change['id'])
    with pytest.raises(PlatformError):env.store.finish_proxy_change(change['id'],success=True)
    env.store.finish_proxy_change(change['id'],success=False,error_code='private-cookie-value')
    assert env.store.get_account('a')['status']=='error' and not env.store.get_account('a')['enabled']
    assert 'private-cookie-value' not in json.dumps(env.store.proxy_events())

def test_proxy_change_resume_metadata_and_prelogin_binding(env):
    env.store.create_account({'id':'a','name':'new'});p=proxy(env)
    change=env.store.begin_proxy_change('a',p['id'],previous_running=False)
    assert env.store.pending_proxy_change('a')['id']==change['id']
    assert env.store.list_proxy_changes()[0]['previous_enabled'] is True
    env.store.commit_proxy_change(change['id'])
    assert env.store.get_proxy_change(change['id'])['state']=='checking'
    result=env.store.finish_proxy_change_for_login(change['id'])
    assert result['login_required'] and env.store.get_account('a')['status']=='login_required'
    assert env.store.get_account('a')['enabled'] and not env.store.list_proxy_changes()
    assert env.store.resolve_account_egress('a').proxy_id==p['id']

def test_existing_account_cannot_skip_new_proxy_session_check(env):
    account(env);p=proxy(env);change=env.store.begin_proxy_change('a',p['id'],previous_running=True)
    assert env.store.get_proxy_change(change['id'])['previous_running']
    env.store.commit_proxy_change(change['id'])
    with pytest.raises(PlatformError):env.store.finish_proxy_change_for_login(change['id'])

def test_proxy_disabled_during_session_check_does_not_enable_account(env):
    account(env);p=proxy(env);change=env.store.begin_proxy_change('a',p['id'])
    env.store.commit_proxy_change(change['id']);env.store.record_account_check('a',status='ready',balance=100)
    env.store.update_proxy(p['id'],{'status':'disabled'})
    with pytest.raises(PlatformError) as error:env.store.finish_proxy_change(change['id'],success=True)
    assert error.value.code=='proxy_disabled' and not env.store.get_account('a')['enabled']
    env.store.finish_proxy_change(change['id'],success=False)
    assert env.store.get_proxy_change(change['id'])['state']=='failed'

def test_busy_account_cannot_change_proxy(env):
    account(env);p=proxy(env);k=env.store.create_key({'name':'key'});pr=env.store.authenticate_key(k['key'])
    task=env.store.reserve_task(pr,'video','seedance-2.0-mini',{'prompt':'test'})
    with pytest.raises(PlatformError) as error:env.store.begin_proxy_change('a',p['id'])
    assert error.value.code=='account_busy'
    env.store.update_task(task['id'],status='uncertain')
    with pytest.raises(PlatformError):env.store.begin_proxy_change('a',p['id'])

def test_bound_endpoint_and_deletion_guard(env):
    account(env);p=proxy(env);bind(env,'a',p['id'])
    with pytest.raises(PlatformError):env.store.update_proxy(p['id'],{'url':'http://different.example:80'})
    with pytest.raises(PlatformError):env.store.delete_proxy(p['id'])
    env.store.update_proxy(p['id'],{'url':'http://private-user:private-password@p.example:8080'})
    assert env.store.get_proxy(p['id'])['last_checked_at']==1000
    assert env.store.resolve_account_egress('a').proxy_id==p['id']

def test_historical_account_binding_preserves_proxy_for_original_downloads(env):
    account(env);p=proxy(env);bind(env,'a',p['id'])
    env.store.begin_account_delete('a');env.store.delete_account('a')
    public=env.store.get_proxy(p['id']);assert public['bound_accounts_count']==0 and public['historical_bindings_count']==1
    with pytest.raises(PlatformError):env.store.delete_proxy(p['id'])
    with pytest.raises(PlatformError):env.store.update_proxy(p['id'],{'url':'http://new.example:80'})
    assert env.store.resolve_account_egress('a').proxy_id==p['id']

def test_backup_cycles_and_disabled_backup(env):
    p=proxy(env);q=proxy(env,'q');env.store.update_proxy(p['id'],{'backup_proxy_id':q['id'],'fallback_mode':'proxy'})
    assert env.store.proxy_fallback_candidate(p['id'])==q['id']
    with pytest.raises(PlatformError):env.store.update_proxy(q['id'],{'backup_proxy_id':p['id'],'fallback_mode':'proxy'})
    with pytest.raises(PlatformError):env.store.delete_proxy(q['id'])
    env.store.update_proxy(q['id'],{'status':'disabled'});assert env.store.proxy_fallback_candidate(p['id']) is None

def test_legacy_migration_dedup_and_invalid_preservation(env):
    account(env,'a',proxy='http://legacy:secret@p.example:80');account(env,'b',proxy='http://legacy:secret@p.example:80')
    account(env,'c',proxy='not a proxy')
    fresh=PlatformStore(env.url,encryption_key=env.key,clock=lambda:env.clock.now)
    assert len(fresh.list_proxies())==1
    with fresh.engine.connect() as c:
        rows={r['id']:r for r in c.execute(select(accounts)).mappings()}
    assert rows['a']['proxy_id']==rows['b']['proxy_id']
    assert 'proxy' not in fresh._decrypt(rows['a']['secret_encrypted'])
    assert fresh._decrypt(rows['c']['secret_encrypted'])['proxy']=='not a proxy'
    assert rows['c']['proxy_migration_error']=='proxy_migration_invalid'
    with pytest.raises(PlatformError):fresh.resolve_account_egress('c')
    assert len(PlatformStore(env.url,encryption_key=env.key).list_proxies())==1

def test_health_state_machine_never_reenables_disabled(env):
    p=proxy(env)
    for i in range(3):env.store.record_proxy_probe(p['id'],{'ok':False,'error_code':'proxy_connect_failed'})
    assert env.store.get_proxy(p['id'])['status']=='failing'
    env.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.10','latency_ms':1});assert env.store.get_proxy(p['id'])['status']=='failing'
    env.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.10','latency_ms':1});assert env.store.get_proxy(p['id'])['status']=='active'
    env.store.update_proxy(p['id'],{'status':'disabled'})
    for i in range(4):env.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.10','latency_ms':1})
    assert env.store.get_proxy(p['id'])['status']=='disabled'

def test_stale_probe_cannot_replace_new_endpoint_even_with_fixed_clock(env):
    p=proxy(env);_,revision=env.store.proxy_probe_context(p['id'])
    env.store.update_proxy(p['id'],{'url':'http://new.example:80'})
    with pytest.raises(PlatformError) as error:env.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.10','latency_ms':1},expected_revision=revision)
    assert error.value.code=='proxy_probe_stale'

def test_batch_validation_and_dedup_never_echo_credentials(env):
    with pytest.raises(PlatformError) as error:env.store.batch_create_proxies('http://p.example:80\nhttp://user:secret@bad.example:no')
    assert 'secret' not in str(error.value) and not env.store.list_proxies()
    value=env.store.batch_create_proxies('http://p.example:80\nhttp://p.example:80')
    assert len(value['items'])==2 and len(env.store.list_proxies())==1


def test_batch_rolls_back_all_rows_when_later_storage_fails(env,monkeypatch):
    original=env.store._proxy_event;writes=[]
    def event(*args,**kwargs):
        writes.append(1)
        if len(writes)==2:raise RuntimeError('Synthetic database write error')
        return original(*args,**kwargs)
    monkeypatch.setattr(env.store,'_proxy_event',event)
    with pytest.raises(RuntimeError):env.store.batch_create_proxies('http://one.example:80\nhttp://two.example:80')
    assert not env.store.list_proxies() and not env.store.proxy_events()


def test_ip_change_warn_default_and_pause_are_independent_of_account_cooldown(env):
    account(env);p=proxy(env);bind(env,'a',p['id'])
    before=env.store.resolve_account_egress('a')
    env.clock.now+=601;env.store.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.99','latency_ms':3,'timezone':'Asia/Tokyo','country':'JP'})
    public=env.store.get_account('a')
    assert public['proxy_ip_changed'] and public['proxy_ip_change_policy']=='warn' and not public['proxy_scheduling_error']
    after=env.store.resolve_account_egress('a');assert after.revision==before.revision and after.timezone==before.timezone
    env.store.update_proxy(p['id'],{'ip_change_policy':'pause'})
    assert env.store.get_account('a')['proxy_scheduling_error']=='proxy_exit_changed'
    with pytest.raises(PlatformError):env.store.resolve_account_egress('a')
    env.store.update_proxy(p['id'],{'ip_change_policy':'warn','name':'Renamed'})
    assert env.store.resolve_account_egress('a').revision==before.revision
    with pytest.raises(PlatformError):env.store.update_proxy(p['id'],{'ip_change_policy':'direct'})


def test_unbind_clears_old_browser_timezone(env):
    account(env);p=proxy(env);bind(env,'a',p['id'])
    change=env.store.begin_proxy_change('a',None);env.store.commit_proxy_change(change['id'])
    assert env.store.get_account('a')['browser_timezone'] is None
    assert env.store.resolve_account_egress('a').timezone is None
    env.store.record_account_check('a',status='ready',balance=100);env.store.finish_proxy_change(change['id'],success=True)
    assert env.store.get_account('a')['proxy_id'] is None
