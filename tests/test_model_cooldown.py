import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dreamina_api.admin_api import create_admin_router
from dreamina_api.platform_store import PlatformStore,PlatformError

MODEL='seedance-2.0-mini'
OTHER='seedance-2.0-fast'


def setup(tmp_path,clock=None):
    key=Fernet.generate_key()
    kwargs={'clock':clock} if clock else {}
    store=PlatformStore('sqlite:///'+str(tmp_path/'db.sqlite'),encryption_key=key,**kwargs)
    for name,priority in [('one',100),('two',0)]:
        store.create_account({'id':name,'name':name,'session_id':'synthetic-'+name,'priority':priority,'capabilities':[MODEL,OTHER]})
    raw=store.create_key({'name':'fixture','max_concurrent':10,'rate_limit_per_minute':1000})
    return store,store.authenticate_key(raw['key']),key


def reserve(store,key,model=MODEL,**kw):return store.reserve_task(key,'video',model,{'prompt':'fixture'},**kw)


def test_cooldown_is_scoped_to_account_and_model(tmp_path):
    store,key,_=setup(tmp_path)
    store.block_account_model('one',MODEL)
    task=reserve(store,key)
    assert task['account_id']=='two'
    other=reserve(store,key,OTHER)
    assert other['account_id']=='one'
    assert store.get_account('one')['capabilities']==[MODEL,OTHER]


def test_no_available_account_does_not_allocate_or_spend_quota(tmp_path):
    store,key,_=setup(tmp_path)
    for name in ('one','two'):store.block_account_model(name,MODEL)
    before=store.list_keys()[0]['used_requests']
    with pytest.raises(PlatformError) as error:reserve(store,key)
    assert error.value.code=='no_available_account' and 'cooldown' in str(error.value)
    assert store.list_tasks()==[] and store.list_keys()[0]['used_requests']==before


def test_completed_idempotent_task_is_returned_even_while_model_is_blocked(tmp_path):
    store,key,_=setup(tmp_path)
    task=reserve(store,key,idempotency_key='same-request')
    store.update_task(task['id'],status='completed')
    for name in ('one','two'):store.block_account_model(name,MODEL)
    assert reserve(store,key,idempotency_key='same-request')['id']==task['id']
    assert store.list_keys()[0]['used_requests']==1


def test_cooldown_persists_restart_and_expires_without_changing_capabilities(tmp_path):
    now=[1000.0];store,key,secret=setup(tmp_path,lambda:now[0])
    store.block_account_model('one',MODEL,cooldown_seconds=60)
    reopened=PlatformStore(str(store.engine.url),encryption_key=secret,clock=lambda:now[0])
    assert reopened.account_model_blocks('one')[0]['blocked_until']==1060
    now[0]=1060
    assert reopened.account_model_blocks('one')==[]
    assert reserve(reopened,key)['account_id']=='one'


def test_manual_clear_only_removes_selected_account_model(tmp_path):
    store,key,_=setup(tmp_path)
    store.block_account_model('one',MODEL);store.block_account_model('one',OTHER);store.block_account_model('two',MODEL)
    store.clear_account_model_block('one',MODEL)
    assert {(b['account_id'],b['model']) for b in store.account_model_blocks()}=={('one',OTHER),('two',MODEL)}
    assert reserve(store,key)['account_id']=='one'


def test_admin_cooldown_and_metrics_routes_require_authentication_and_csrf(tmp_path):
    store,_,_=setup(tmp_path);store.bootstrap_admin('admin','synthetic-password-long')
    store.block_account_model('one',MODEL)
    app=FastAPI();app.include_router(create_admin_router(store))
    with TestClient(app) as c:
        assert c.get('/api/admin/reliability').status_code==401
        assert c.get('/api/admin/model-blocks').status_code==401
        login=c.post('/api/admin/login',json={'username':'admin','password':'synthetic-password-long'});csrf=login.json()['csrf_token']
        assert c.get('/api/admin/reliability').status_code==200
        rows=c.get('/api/admin/accounts').json()['items']
        assert next(a for a in rows if a['id']=='one')['model_blocks'][0]['model']==MODEL
        url='/api/admin/accounts/one/model-blocks/'+MODEL
        assert c.delete(url).status_code==403
        assert c.delete(url,headers={'X-CSRF-Token':csrf}).status_code==200
        assert c.get('/api/admin/model-blocks').json()['items']==[]
