import asyncio
from copy import deepcopy
import json
from pathlib import Path
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet,InvalidToken
from dreamina_api.browser_driver import generation_request,generation_response,NativeUI
from dreamina_api.browser_worker import safe_config,profile_lock,Worker,write_json
from dreamina_api.payloads import build_generation


def request(model='seedance-2.0-mini',references=None):
    spec={'model':model,'prompt':'Clouds over a lake','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni'}
    prepared=build_generation(spec,workspace_id='12345')
    if references:spec['references']=references
    return spec,prepared


@pytest.mark.parametrize('model',['seedance-2.0-mini','seedance-2.0-fast','seedance-2.0','seedance-2.5'])
def test_match_native_requests_all_models(model):
    spec,p=request(model)
    result=generation_request('https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate?secret=not-retained','POST',json.dumps(p['body']),spec)
    assert result['submit_id']==p['submit_id'] and result['conversation_id']=='12345'
    assert 'secret' not in json.dumps(result)
    assert generation_request('https://unrelated.example/mweb/v1/aigc_draft/generate','POST',json.dumps(p['body']),spec) is None
    assert generation_request('https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate','POST',json.dumps(p['body']),spec|{'prompt':'different'}) is None


def test_success_requires_real_upstream_identity_and_business_ret():
    spec,p=request();req={'submit_id':p['submit_id'],'api_origin':'https://dreamina-api.us.capcut.com'}
    assert generation_response(req,200,{'ret':'-6','errmsg':'private'})['status']=='failed'
    assert generation_response(req,200,{'ret':'0','data':{}})['status']=='uncertain'
    assert generation_response(req,200,{'ret':'0','data':{'aigc_data':{'submit_id':str(uuid.uuid4())}}})['status']=='uncertain'
    assert generation_response(req,200,{'ret':'0','data':{'aigc_data':{'submit_id':p['submit_id'],'history_record_id':'123'}}})['status']=='submitted'


def test_file_config_uses_dedicated_profile_and_https(tmp_path):
    path=tmp_path/'worker.json';base={'server_url':'http://127.0.0.1:8787','worker_key':'synthetic-key-'+'a'*32,'accounts':[{'id':'one','profile_dir':'profiles/one'}]}
    path.write_text(json.dumps(base));cfg=safe_config(path,'one')
    assert cfg['profile_dir']==tmp_path/'profiles'/'one'
    for url in ['http://remote.example','https://user:pass@remote.example','https://remote.example/?key=secret']:
        path.write_text(json.dumps(base|{'server_url':url}))
        with pytest.raises(ValueError):safe_config(path,'one')


def test_native_mode_refuses_extension_dispatch(tmp_path,monkeypatch):
    from dreamina_api.chrome_bridge import BridgeStore
    monkeypatch.setenv('WORKER_MODE','native')
    store=BridgeStore(tmp_path/'bridge')
    for name,driver in [('old','extension'),('new','native')]:
        store.heartbeat('default',name,ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',driver=driver)
    workers={w['worker_id']:w for w in store.accounts()[0]['workers']}
    assert workers['old']['ready'] is False
    assert workers['new']['ready'] is True


def test_profile_lock_does_not_allow_two_workers(tmp_path):
    async def run():
        async with profile_lock(tmp_path):
            with pytest.raises(RuntimeError):
                async with profile_lock(tmp_path):pass
        assert not (tmp_path/'.dreamina-worker.lock').exists()
    asyncio.run(run())


def test_recovery_does_not_reclick_after_durable_click_intent(tmp_path):
    async def run():
        cfg={'server_url':'https://example.test','worker_key':'synthetic-'+'x'*32,'account':'one','state_dir':tmp_path,'profile_dir':tmp_path/'profile'}
        worker=Worker(cfg);calls=[]
        async def api(path,body=None,**kwargs):
            calls.append((path,body))
            return {'status':'claimed'} if path.endswith('/status') else {'ok':True}
        worker.api=api
        job={'id':str(uuid.uuid4()),'claim_token':'synthetic','request':{}}
        write_json(worker.journal,{'phase':'click_intent','job':job})
        assert await worker.recover() is False
        assert calls[-1][1]['status']=='uncertain'
        assert all(not path.endswith('/claim') for path,_ in calls)
        await worker.http.aclose()
    asyncio.run(run())


def test_recovery_before_click_reports_safe_local_failure(tmp_path):
    async def run():
        cfg={'server_url':'https://example.test','worker_key':'synthetic-'+'x'*32,'account':'one','state_dir':tmp_path,'profile_dir':tmp_path/'profile'}
        worker=Worker(cfg);calls=[]
        async def api(path,body=None,**kwargs):
            calls.append((path,body));return {'status':'claimed'} if path.endswith('/status') else {'ok':True}
        worker.api=api;job={'id':str(uuid.uuid4()),'claim_token':'synthetic','request':{}}
        write_json(worker.journal,{'phase':'preparing','job':job})
        assert await worker.recover() is True
        assert calls[-1][1]['status']=='failed'
        assert json.loads(worker.journal.read_text())['phase']=='finished'
        await worker.http.aclose()
    asyncio.run(run())


class SnapshotContext:
    def __init__(self,failure=None):
        self.failure=failure;self.closed=False;self.restored=[]
        self.pages=[SimpleNamespace(goto=self.goto)]
        self.current=[{'domain':'.capcut.com','path':'/','name':'sessionid','value':'synthetic-profile-cookie'},
                      {'domain':'.google.com','path':'/','name':'provider-cookie','value':'not-exported'}]

    async def goto(self,*args,**kwargs):
        if self.failure=='goto':raise RuntimeError('Synthetic navigation failure')
        if self.failure=='cancel':raise asyncio.CancelledError()

    async def add_cookies(self,cookies):
        if self.failure=='add_cookies':raise ValueError('Synthetic restore rejection')
        self.restored=cookies

    async def cookies(self):return self.current
    async def close(self):self.closed=True


def snapshot_worker(tmp_path,state_file):
    return Worker({'server_url':'https://example.test','worker_key':'synthetic-'+'x'*32,
                   'account':'one','state_dir':tmp_path/'worker-state','profile_dir':tmp_path/'profile',
                   'headless':True,'channel':'chromium','executable_path':None,'proxy':None,
                   'encrypted_state_file':state_file})


@pytest.mark.parametrize('failure',['decrypt','json','add_cookies','goto','cancel'])
def test_failed_snapshot_restore_does_not_overwrite_existing_ciphertext(tmp_path,monkeypatch,failure):
    async def run():
        key=Fernet.generate_key();monkeypatch.setenv('DREAMINA_PROFILE_KEY',key.decode())
        original={'cookies':[{'domain':'.capcut.com','path':'/','name':'sessionid','value':'synthetic-old-session'}]}
        encrypted=b'corrupted-ciphertext-retained-for-recovery' if failure=='decrypt' else Fernet(key).encrypt(b'invalid JSON' if failure=='json' else json.dumps(original).encode())
        state_file=tmp_path/'one.enc';state_file.write_bytes(encrypted)
        worker=snapshot_worker(tmp_path,state_file);context=SnapshotContext(failure)
        async def launch(*args,**kwargs):return context
        async def heartbeat(*args,**kwargs):pass
        worker.heartbeat=heartbeat
        expected={'decrypt':InvalidToken,'json':json.JSONDecodeError,'add_cookies':ValueError,'goto':RuntimeError,'cancel':asyncio.CancelledError}[failure]
        try:
            with pytest.raises(expected):await worker.open(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch)))
        finally:await worker.close()
        assert context.closed
        assert state_file.read_bytes()==encrypted
        assert not state_file.with_suffix('.tmp').exists()
    asyncio.run(run())


def test_successful_snapshot_restore_refreshes_only_first_party_state_on_close(tmp_path,monkeypatch):
    async def run():
        key=Fernet.generate_key();monkeypatch.setenv('DREAMINA_PROFILE_KEY',key.decode())
        original={'cookies':[{'domain':'.capcut.com','path':'/','name':'sessionid','value':'synthetic-old-session'}]}
        state_file=tmp_path/'one.enc';state_file.write_bytes(Fernet(key).encrypt(json.dumps(original).encode()))
        worker=snapshot_worker(tmp_path,state_file);context=SnapshotContext()
        async def launch(*args,**kwargs):return context
        async def heartbeat(*args,**kwargs):pass
        worker.heartbeat=heartbeat
        try:
            await worker.open(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch)))
            assert context.restored==original['cookies']
        finally:await worker.close()
        assert context.closed
        saved=json.loads(Fernet(key).decrypt(state_file.read_bytes()))
        assert saved=={'cookies':[context.current[0]]}
        assert b'synthetic-profile-cookie' not in state_file.read_bytes()
    asyncio.run(run())
