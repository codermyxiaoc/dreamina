import asyncio
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest

import dreamina_api.browser_worker as module
from dreamina_api.browser_worker import Worker,write_json,BrowserTimeoutError,UIUnavailable
from dreamina_api.credit_quote import quote_request_fingerprint


def worker(tmp_path):
    return Worker({'server_url':'https://fixture.test','worker_key':'synthetic-key-'+'x'*32,
                   'account':'one','state_dir':tmp_path,'profile_dir':tmp_path/'profile','prepare_attempts':2})


def job():
    return {'id':str(uuid.uuid4()),'account_id':'one','claim_token':'synthetic-claim',
            'request':{'model':'seedance-2.0-mini','prompt':'fixture','references':[]}}


async def no_wait(*_):pass


def approved_quote(spec):
    return {'required_credits':20,'source':'native_page','observed_at':1000,
            'model':spec['model'],'request_fingerprint':quote_request_fingerprint(spec)}


@pytest.mark.parametrize('endpoint,method,expected',[
    ('/claim','POST',1),('/slots/recover','POST',3),('/jobs/x/activate','POST',3),('/jobs/x/status','POST',3),('/jobs/x/report','POST',3),('/jobs/x/assets/a','GET',3)])
def test_worker_only_retries_repeatable_internal_operations(tmp_path,monkeypatch,endpoint,method,expected):
    async def run():
        w=worker(tmp_path);await w.http.aclose();calls=[]
        def transport(req):
            calls.append(req)
            return httpx.Response(503 if len(calls)<3 else 200,json={'ok':True})
        w.http=httpx.AsyncClient(base_url='https://fixture.test',transport=httpx.MockTransport(transport))
        monkeypatch.setattr(module.asyncio,'sleep',no_wait)
        try:
            if expected==1:
                with pytest.raises(RuntimeError):await w.api(endpoint,method=method)
            else:await w.api(endpoint,method=method)
            assert len(calls)==expected
            assert all(str(c.url).startswith('https://fixture.test/v1/chrome/') for c in calls)
        finally:await w.http.aclose()
    asyncio.run(run())


def test_request_id_durable_before_result_and_one_activation_after_safe_prepare_retry(tmp_path,monkeypatch):
    async def run():
        w=worker(tmp_path);j=job();sid=str(uuid.uuid4());pages=[];activations=[];events=[];calls=[]
        class Page:
            closed=False
            async def bring_to_front(self):pass
            async def goto(self,*_,**__):
                if self is pages[0]:raise BrowserTimeoutError('Synthetic page load timeout')
            async def close(self):self.closed=True
        class Context:
            async def new_page(self):
                p=Page();pages.append(p);return p
        class Driver:
            def __init__(self,page,*_,**__):self.page=page;self.clicked=False;self.request_observed=None
            async def diagnostic(self,*_):pass
            async def wait_ready(self):return {'ready':True,'reason':'Ready'}
            async def submit(self,spec,files,before_click,on_request_observed=None,on_preflight=None):
                assert on_preflight is not None
                await on_preflight(approved_quote(spec))
                await before_click();self.clicked=True;activations.append(True)
                self.request_observed={'submit_id':sid,'api_origin':'https://dreamina-api.us.capcut.com'}
                on_request_observed(self.request_observed)
                stored=json.loads(w.journal.read_text());assert stored['phase']=='request_observed';assert stored['confirmation']['submit_id']==sid
                return dict(self.request_observed,status='submitted')
        async def api(path,body=None,**_):
            calls.append((path,body))
            if path.endswith('/preflight'):return {'allowed':True}
            if path.endswith('/activate'):return {'can_activate':True}
            if path.endswith('/status'):return {'status':'completed' if any(p.endswith('/report') for p,b in calls) else 'claimed'}
            return {'ok':True}
        async def telemetry(j,v):events.append(v)
        async def files(_):return []
        w.context=Context();w.api=api;w.telemetry=telemetry;w.files=files
        monkeypatch.setattr(module,'NativeUI',Driver);monkeypatch.setattr(module.asyncio,'sleep',no_wait)
        try:
            assert await w.handle(j)
            assert len(pages)==2 and all(p.closed for p in pages)
            assert len(activations)==1
            assert [e['stage'] for e in events]==['preparation_retry']
            assert json.loads(w.journal.read_text())['phase']=='finished'
            assert len([p for p,b in calls if p.endswith('/report')])==1
        finally:await w.http.aclose()
    asyncio.run(run())


def test_uncertain_after_intent_never_retries_preparation(tmp_path,monkeypatch):
    async def run():
        w=worker(tmp_path);j=job();pages=[];reports=[]
        class Page:
            async def bring_to_front(self):pass
            async def goto(self,*_,**__):pass
        class Context:
            async def new_page(self):p=Page();pages.append(p);return p
        class Driver:
            def __init__(self,*_,**__):self.clicked=False;self.request_observed=None
            async def diagnostic(self,*_):pass
            async def wait_ready(self):return {'ready':True}
            async def submit(self,spec,files,before_click,on_request_observed=None,on_preflight=None):
                assert on_preflight is not None
                await on_preflight(approved_quote(spec))
                await before_click();self.clicked=True;raise BrowserTimeoutError('Synthetic action timeout')
        async def api(path,body=None,**_):
            if path.endswith('/preflight'):return {'allowed':True}
            if path.endswith('/activate'):return {'can_activate':True}
            if path.endswith('/report'):reports.append(body)
            return {'status':'claimed'}
        async def files(_):return []
        w.context=Context();w.api=api;w.files=files;monkeypatch.setattr(module,'NativeUI',Driver)
        try:
            assert await w.handle(j) is False
            assert len(pages)==1 and len(reports)==1 and reports[0]['status']=='uncertain'
            assert 'submit_id' not in reports[0]
        finally:await w.http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('phase',['request_observed','reported','reporting'])
def test_restart_recovers_known_id_without_a_new_submission(tmp_path,phase):
    async def run():
        w=worker(tmp_path);j=job();sid=str(uuid.uuid4());calls=[]
        confirmation={'submit_id':sid,'api_origin':'https://dreamina-api.us.capcut.com','status':'uncertain'}
        result={'worker_id':w.worker_id,'claim_token':j['claim_token'],'status':'uncertain','submit_id':sid}
        write_json(w.journal,{'phase':phase,'job':j,'confirmation':confirmation,'result':result})
        async def api(path,body=None,**_):calls.append((path,body));return {'status':'uncertain'}
        w.api=api
        try:
            assert await w.recover() is True
            assert w.resume_job[1]['submit_id']==sid
            assert all('/claim' not in p and 'generate' not in p for p,b in calls)
            assert calls[-1][1]['submit_id']==sid
        finally:await w.http.aclose()
    asyncio.run(run())


def test_ambiguous_receipt_does_not_recover_first_id(tmp_path):
    async def run():
        w=worker(tmp_path);j=job();ids=[str(uuid.uuid4()),str(uuid.uuid4())];calls=[]
        write_json(w.journal,{'phase':'submission_ambiguous','job':j,'confirmation':{'observed_submit_ids':ids}})
        async def api(path,body=None,**_):calls.append((path,body));return {'status':'uncertain'}
        w.api=api
        try:
            assert await w.recover() is False and w.resume_job is None
            assert calls[-1][1]['status']=='uncertain' and 'submit_id' not in calls[-1][1]
            assert json.loads(w.journal.read_text())['confirmation']['observed_submit_ids']==ids
        finally:await w.http.aclose()
    asyncio.run(run())


def test_receipt_disk_failure_still_reports_centrally_then_stops(tmp_path,monkeypatch):
    async def run():
        w=worker(tmp_path);j=job();calls=[]
        async def api(path,body=None,**_):calls.append((path,body));return {'ok':True}
        def broken(*_):raise OSError('private-path-not-for-output')
        w.api=api;monkeypatch.setattr(module,'write_json',broken)
        try:
            with pytest.raises(RuntimeError,match='central receipt was saved') as error:
                await w.report(j,{'status':'uncertain','submit_id':str(uuid.uuid4())})
            assert 'private-path' not in str(error.value)
            assert len(calls)==1 and calls[0][0].endswith('/report')
        finally:await w.http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('same_id',[True,False])
def test_lost_report_ack_can_reconcile_completed_original_only(tmp_path,same_id):
    async def run():
        w=worker(tmp_path);j=job();sid=str(uuid.uuid4());calls=[]
        async def api(path,body=None,**_):
            calls.append(path)
            if path.endswith('/report'):raise RuntimeError('Worker API HTTP 409; request values omitted.')
            return {'status':'completed','submit_id':sid if same_id else str(uuid.uuid4())}
        w.api=api
        try:
            if same_id:
                await w.report(j,{'status':'submitted','submit_id':sid})
                assert json.loads(w.journal.read_text())['phase']=='reported'
            else:
                with pytest.raises(RuntimeError,match='409'):await w.report(j,{'status':'submitted','submit_id':sid})
                assert json.loads(w.journal.read_text())['phase']=='reporting'
            assert len(calls)==2 and calls[1].endswith('/status')
        finally:await w.http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('message',['Login required','Human verification required','Reference upload rejected','Restore the original page language','Claim is not active'])
def test_actionable_preflight_failures_are_not_retried(message):
    driver=SimpleNamespace(clicked=False,request_observed=None)
    assert Worker.preparation_retryable(UIUnavailable(message),driver,False) is False
