"""Owned pre-activation balance protocol: no real browser/accounts or generation."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dreamina_api.chrome_bridge import BridgeStore,BridgeError,create_chrome_bridge_router
from dreamina_api.credit_quote import quote_request_fingerprint
import dreamina_api.browser_worker as worker_module
from test_worker_reliability import worker,job,approved_quote


def fixture(tmp_path,*,balance=100,required=20,configured=True):
    clock=SimpleNamespace(now=1000.)
    store=BridgeStore(tmp_path/'bridge',clock=lambda:clock.now)
    queued=store.enqueue({'model':'seedance-2.0-mini','prompt':'Synthetic quote only','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni'},account_id='one')
    store.heartbeat('one','native-test',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',protocol_version=4,driver='native')
    claimed=store.claim('one','native-test');assert claimed['id']==queued['id']
    calls=[];current=SimpleNamespace(balance=balance)
    async def check(j,worker_id,claim_token,quote):
        calls.append(deepcopy(quote))
        return store.credit_preflight(j['id'],worker_id,claim_token,quote,current.balance)['credit_preflight']
    key='internal-fixture-'+('x'*32)
    app=FastAPI();app.include_router(create_chrome_bridge_router(lambda:None,store=store,pairing_key_loader=lambda:key,credit_checker=check if configured else None))
    body={'worker_id':claimed['worker_id'],'claim_token':claimed['claim_token'],'generation_not_activated':True,
          'required_credits':required,'source':'native_page','observed_at':clock.now,'model':claimed['request']['model'],
          'request_fingerprint':quote_request_fingerprint(claimed['request'])}
    return SimpleNamespace(store=store,clock=clock,claimed=claimed,calls=calls,current=current,
                           body=body,headers={'X-Chrome-Bridge-Key':key},client=TestClient(app),
                           path='/v1/chrome/jobs/'+claimed['id']+'/preflight')


@pytest.mark.parametrize('required,balance',[(20,100),(20,20),(0,0),(0,100),(1.25,1.25)])
def test_owned_exact_quote_allows_sufficient_balance_without_submission(tmp_path,required,balance):
    env=fixture(tmp_path,required=required,balance=balance)
    r=env.client.post(env.path,json=env.body,headers=env.headers)
    assert r.status_code==200 and r.json()['allowed'] is True
    assert r.json()['required_credits']==required and r.json()['balance']==balance
    assert r.json()['generation_not_activated'] is True and len(env.calls)==1
    job=env.store.get(env.claimed['id'])
    assert job['status']=='claimed' and job['submit_id'] is None
    assert job['credit_preflight']==r.json()


def test_insufficient_proof_is_durable_and_replay_never_rechecks_or_reopens_claim(tmp_path):
    env=fixture(tmp_path,balance=19,required=20)
    first=env.client.post(env.path,json=env.body,headers=env.headers)
    assert first.status_code==200 and first.json()['allowed'] is False
    env.current.balance=10000;env.clock.now+=1
    repeated=env.client.post(env.path,json=env.body,headers=env.headers)
    assert repeated.status_code==200 and repeated.json()==first.json() and len(env.calls)==1
    reopened=BridgeStore(env.store.directory,clock=lambda:env.clock.now)
    final=reopened.get(env.claimed['id'])
    assert final['status']=='failed' and final['submit_id'] is None and final['credit_preflight']==first.json()
    reported=reopened.report(final['id'],env.body['worker_id'],env.body['claim_token'],status='failed',error='Insufficient credits')
    assert reported['credit_preflight']==first.json() and reported['submit_id'] is None


@pytest.mark.parametrize('headers,expected',[
    ({},401),({'Authorization':'Bearer sk-dr-public-not-worker'},401),
    ({'X-Chrome-Bridge-Key':'wrong'},401),
    ({'Origin':'https://unrelated.example','X-Chrome-Bridge-Key':'internal-fixture-'+('x'*32)},403),
])
def test_public_bearer_and_web_origin_cannot_access_worker_credit_check(tmp_path,headers,expected):
    env=fixture(tmp_path);r=env.client.post(env.path,json=env.body,headers=headers)
    assert r.status_code==expected and not env.calls
    assert env.store.get(env.claimed['id'])['credit_preflight'] is None


@pytest.mark.parametrize('field,value',[('worker_id','different-worker'),('claim_token','wrong-claim')])
def test_quote_must_own_the_claim(tmp_path,field,value):
    env=fixture(tmp_path);body=env.body|{field:value}
    r=env.client.post(env.path,json=body,headers=env.headers)
    assert r.status_code==403 and not env.calls


@pytest.mark.parametrize('patch,expected',[
    ({'generation_not_activated':False},409),({'generation_not_activated':1},422),
    ({'observed_at':939.0},422),({'observed_at':1006.0},422),
    ({'request_fingerprint':'0'*64},422),({'model':'seedance-2.0'},422),
    ({'source':'guess'},422),({'required_credits':-1},422),
    ({'required_credits':True},422),({'required_credits':False},422),
    ({'observed_at':True},422),({'required_credits':'20'},422),
    ({'submit_id':str(uuid.uuid4())},422),
])
def test_stale_mismatched_or_untyped_quotes_never_persist_approval(tmp_path,patch,expected):
    env=fixture(tmp_path);r=env.client.post(env.path,json=env.body|patch,headers=env.headers)
    assert r.status_code==expected
    current=env.store.get(env.claimed['id']);assert current['credit_preflight'] is None and current['submit_id'] is None


@pytest.mark.parametrize('field,value',[('required_credits',float('nan')),('required_credits',float('inf')),('observed_at',float('nan'))])
def test_nonfinite_json_quotes_are_rejected(tmp_path,field,value):
    env=fixture(tmp_path)
    # Deliberately malformed/nonstandard JSON input; httpx's normal json= encoder rejects it first.
    r=env.client.post(env.path,content=json.dumps(env.body|{field:value}),headers=env.headers|{'Content-Type':'application/json'})
    assert r.status_code==422 and not env.calls
    assert env.store.get(env.claimed['id'])['credit_preflight'] is None


@pytest.mark.parametrize('state',['submitted','expired'])
def test_activated_or_expired_claim_cannot_switch_accounts(tmp_path,state):
    env=fixture(tmp_path)
    if state=='submitted':env.store.report(env.claimed['id'],env.body['worker_id'],env.body['claim_token'],status='submitted',submit_id=str(uuid.uuid4()))
    else:env.clock.now+=601
    r=env.client.post(env.path,json=env.body|{'observed_at':env.clock.now},headers=env.headers)
    assert r.status_code==409 and env.store.get(env.claimed['id'])['credit_preflight'] is None


def test_missing_balance_checker_is_explicit_and_never_approves(tmp_path):
    env=fixture(tmp_path,configured=False);r=env.client.post(env.path,json=env.body,headers=env.headers)
    assert r.status_code==503 and env.store.get(env.claimed['id'])['credit_preflight'] is None


@pytest.mark.parametrize('balance',[None,True,float('nan'),float('inf'),-1])
def test_unknown_or_invalid_exact_balance_cannot_approve(tmp_path,balance):
    env=fixture(tmp_path,balance=balance);r=env.client.post(env.path,json=env.body,headers=env.headers)
    assert r.status_code==422 and env.store.get(env.claimed['id'])['credit_preflight'] is None


def test_worker_credit_denial_never_writes_click_intent_or_retries_submission(tmp_path,monkeypatch):
    async def run():
        w=worker(tmp_path);j=job();pages=[];calls=[];activations=[]
        class Page:
            closed=False
            async def bring_to_front(self):pass
            async def goto(self,*_,**__):pass
            async def close(self):self.closed=True
        class Context:
            async def new_page(self):p=Page();pages.append(p);return p
        class Driver:
            def __init__(self,*_,**__):self.clicked=False;self.request_observed=None
            async def diagnostic(self,*_):pass
            async def wait_ready(self):return {'ready':True}
            async def submit(self,spec,files,before_click,on_request_observed=None,on_preflight=None):
                assert on_preflight is not None
                await on_preflight(approved_quote(spec))
                await before_click();self.clicked=True;activations.append(True)
                raise AssertionError('Denied credit check cannot activate generation')
        async def api(path,body=None,**_):
            calls.append((path,body))
            if path.endswith('/preflight'):
                assert body['generation_not_activated'] is True
                assert body['request_fingerprint']==quote_request_fingerprint(j['request'])
                return {'allowed':False,'balance':1,'required_credits':20,'generation_not_activated':True}
            if path.endswith('/report'):return {'status':'failed'}
            raise AssertionError('A denied task must not reach status/claim operations')
        async def files(_):return []
        w.context=Context();w.api=api;w.files=files
        monkeypatch.setattr(worker_module,'NativeUI',Driver)
        try:
            assert await w.handle(j) is True
            assert len(pages)==1 and pages[0].closed and not activations
            assert len(calls)==2 and calls[0][0].endswith('/preflight') and calls[1][0].endswith('/report')
            assert calls[1][1]['status']=='failed' and 'submit_id' not in calls[1][1]
            assert json.loads(w.journal.read_text())['phase']=='finished' and w.active is None
        finally:await w.http.aclose()
    asyncio.run(run())
