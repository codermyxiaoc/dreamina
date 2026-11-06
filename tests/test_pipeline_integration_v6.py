"""Real stores, synthetic upstream histories: no website calls or paid tasks."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time
import uuid

from cryptography.fernet import Fernet
import pytest

from dreamina_api.chrome_bridge import BridgeStore,BridgeError
from dreamina_api.client import HISTORY_PATH
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.openai_api import video_object
from dreamina_api.payloads import build_generation
from dreamina_api.platform_runtime import PlatformRuntime
from dreamina_api.platform_store import PlatformStore,PlatformError
import dreamina_api.platform_runtime as runtime_module


PAGE='https://dreamina.capcut.com/ai-tool/generate'
ORIGIN='https://dreamina-api.us.capcut.com'
MODEL='seedance-2.0-mini'


class Pipeline:
    def __init__(self,tmp_path,monkeypatch):
        self.now=time.time();self.records={};self.http_calls=[]
        self.bridge=BridgeStore(tmp_path/'bridge',clock=lambda:self.now)
        self.store=PlatformStore('sqlite:///:memory:',encryption_key=Fernet.generate_key(),clock=lambda:self.now,worker_status=self.bridge.accounts)
        for account in ('one','two'):
            self.store.create_account({'id':account,'name':account,'session_id':'synthetic-'+account,
                'capabilities':[MODEL],'max_concurrent':1,'max_inflight':3})
            self.store.record_account_check(account,status='ready',balance=100)
            self.ready(account)
        self.key=self.store.create_key({'name':'Synthetic pipeline','max_concurrent':10,'request_limit':20,'account_ids':['one','two'],'allowed_models':[MODEL]})
        self.principal=self.store.authenticate_key(self.key['key'])
        self.runtime=PlatformRuntime(self.store,self.bridge,clock=lambda:self.now)
        async def credit(*_):return {'status':'ready','balance':100}
        self.runtime.check_account=credit
        harness=self
        class Client:
            api_base=ORIGIN
            def __init__(self,settings):self.settings=settings
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def request(self,path,body):
                assert path==HISTORY_PATH,'No generation/billing network endpoint may be used by this fixture'
                harness.http_calls.append((path,deepcopy(body),self.settings.session_id))
                return {sid:deepcopy(harness.records[sid]) for sid in body['submit_ids'] if sid in harness.records}
        monkeypatch.setattr(runtime_module,'DreaminaClient',Client)

    def ready(self,account='one'):
        self.bridge.heartbeat(account,account+'-slot',ready=True,page_url=PAGE,protocol_version=6,driver='native',slot_index=1,slot_count=1,owner_id=account+'-browser')

    def enqueue(self,account='one'):
        spec={'model':MODEL,'prompt':'Synthetic cloud '+uuid.uuid4().hex[:8],'duration':4,'seconds':'4',
              'resolution':'720p','ratio':'16:9','mode':'omni','references':[],'size':'1280x720'}
        task=self.store.reserve_task(self.principal,'video',MODEL,spec,account_id=account)
        queued=self.bridge.enqueue(self.runtime._dispatch_request(task),account_id=account)
        self.store.update_task(task['id'],bridge_job_id=queued['id'])
        return self.store.get_task(task['id'])

    def claim(self,task):
        account=task['account_id'];self.ready(account)
        job=self.bridge.claim(account,account+'-slot',capacity=1,request_id=str(uuid.uuid4()))
        if job:self.store.update_task(task['id'],status='claimed')
        return job

    async def prepare(self,job):
        value=await self.runtime.prepare_task(job,job['worker_id'],job['claim_token'])
        assert value['can_prepare'] is True
        return await self.runtime.submission_turn(job,job['worker_id'],job['claim_token'])

    async def activate(self,job):
        quote={'source':'native_page','model':MODEL,'observed_at':self.now,'required_credits':20,'request_fingerprint':quote_request_fingerprint(job['request'])}
        assert (await self.runtime.credit_preflight(job,job['worker_id'],job['claim_token'],quote))['allowed']
        value=await self.runtime.activate_task(job,job['worker_id'],job['claim_token'])
        assert value['can_activate'] is True
        return value

    async def report(self,job,*,known=True,status='submitted'):
        sid=str(uuid.uuid4()) if known else None
        value=self.bridge.report(job['id'],job['worker_id'],job['claim_token'],status=status,
                                submit_id=sid,api_origin=ORIGIN,error='Synthetic unknown' if status=='uncertain' else None)
        await self.runtime.reported_submission(value)
        return sid,value

    def history(self,job,sid,*,status=20,prompt=None):
        spec=dict(job['request']);spec.pop('asset_owner_task_id',None);spec.pop('new_session',None)
        if prompt is not None:spec['prompt']=prompt
        record={'submit_id':sid,'created_time':self.now,'workspace_id':'123456','history_record_id':'123456',
                'draft_content':build_generation(spec,workspace_id='123456')['body']['draft_content'],'status':status,'item_list':[]}
        if status==50:record['item_list']=[{'video':{'duration_ms':4000,'transcoded_video':{'origin':{'video_url':'https://cdn.example.test/fixture.mp4','width':1280,'height':720,'format':'mp4'}}}}]
        if status==30:record.update(fail_code='2052',fail_starling_key='ErrMessage_APP_OutputAudioRisk')
        self.records[sid]=record
        return record


def test_one_browser_slot_can_feed_three_confirmed_inflight_tasks_without_releasing_credit(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);first=p.enqueue();job=p.claim(first)
        assert (await p.prepare(job))['ready'];await p.activate(job)
        sid,receipt=await p.report(job)
        assert receipt['slot_released'] is False
        second=p.enqueue();assert p.claim(second) is None
        p.history(job,sid)
        current=await p.runtime.sync_task(first)
        assert current['status']=='in_progress' and p.bridge.get(job['id'])['slot_released'] is True
        assert len(p.store.active_credit_holds('one'))==1
        public=video_object(current);assert public.get('progress') is None
        assert public.get('progress_details',{}).get('source')!='page'
        second_job=p.claim(second);assert second_job and second_job['id']!=job['id']
        turn=await p.prepare(second_job);assert turn['ready'] is False and turn['wait_seconds']>=5
        p.now+=5
        assert (await p.runtime.submission_turn(second_job,second_job['worker_id'],second_job['claim_token']))['ready']
        await p.activate(second_job);second_sid,_=await p.report(second_job);p.history(second_job,second_sid)
        await p.runtime.sync_task(second)
        third=p.enqueue();third_job=p.claim(third);assert third_job
        assert not (await p.prepare(third_job))['ready'];p.now+=5
        assert (await p.runtime.submission_turn(third_job,third_job['worker_id'],third_job['claim_token']))['ready']
        await p.activate(third_job);third_sid,_=await p.report(third_job);p.history(third_job,third_sid)
        await p.runtime.sync_task(third)
        assert len(p.store.active_credit_holds('one'))==3
        account=p.store.get_account('one');assert account['active_tasks']==3 and account['current_slot_capacity']==1 and account['current_inflight_capacity']==3
        with pytest.raises(PlatformError) as full:p.enqueue()
        assert full.value.code=='no_available_account'
        assert p.store.list_keys()[0]['used_requests']==3
        p.now+=3;p.history(job,sid,status=50)
        done=await p.runtime.sync_task(first);assert done['status']=='completed'
        assert len(p.store.active_credit_holds('one'))==2
        assert p.enqueue()['id'] not in (first['id'],second['id'],third['id'])
        assert all(call[0]==HISTORY_PATH for call in p.http_calls)
    asyncio.run(run())


def test_shared_egress_turn_waits_three_seconds_and_same_account_five(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);one=p.enqueue();j1=p.claim(one);assert (await p.prepare(j1))['ready'];await p.activate(j1)
        two=p.enqueue('two');j2=p.claim(two)
        waiting=await p.prepare(j2);assert waiting['ready'] is False
        sid,_=await p.report(j1);p.history(j1,sid);await p.runtime.sync_task(one)
        waiting=await p.runtime.submission_turn(j2,j2['worker_id'],j2['claim_token'])
        assert waiting['ready'] is False and waiting['wait_seconds']>=3
        p.now+=2.99;assert not (await p.runtime.submission_turn(j2,j2['worker_id'],j2['claim_token']))['ready']
        p.now+=.01;assert (await p.runtime.submission_turn(j2,j2['worker_id'],j2['claim_token']))['ready']
        await p.activate(j2)
    asyncio.run(run())


@pytest.mark.parametrize('problem',['missing','mismatched'])
def test_missing_or_wrong_history_never_detaches_or_frees_credit(tmp_path,monkeypatch,problem):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        await p.prepare(job);await p.activate(job);sid,_=await p.report(job)
        if problem=='mismatched':p.history(job,sid,prompt='A completely different task')
        value=await p.runtime.sync_task(task)
        assert value['status'] in ('submitted','uncertain')
        assert not p.bridge.get(job['id'])['slot_released'] and len(p.store.active_credit_holds())==1
        second=p.enqueue();assert p.claim(second) is None
    asyncio.run(run())


def test_unknown_without_submit_id_never_releases_slot_or_budget(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        await p.prepare(job);await p.activate(job);await p.report(job,known=False,status='uncertain')
        value=await p.runtime.sync_task(task)
        assert value['status']=='uncertain' and value['submit_id'] is None
        assert not p.bridge.get(job['id'])['slot_released'] and len(p.store.active_credit_holds())==1
        with pytest.raises(PlatformError) as required:await p.runtime.manual_reconcile(task['id'])
        assert required.value.code=='original_id_required' and not p.http_calls
    asyncio.run(run())


def test_closed_unknown_page_releases_physical_slot_and_pacing_not_credit_or_inflight(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        await p.prepare(job);await p.activate(job)
        receipt=p.bridge.report(job['id'],job['worker_id'],job['claim_token'],status='uncertain',
            error='Original outcome unknown; owned UI is now closed.',submission_window_closed=True)
        await p.runtime.reported_submission(receipt);await p.runtime.sync_task(task)
        assert receipt['submission_window_closed_at'] is not None and receipt['slot_released'] is True
        assert len(p.store.active_credit_holds('one'))==1 and p.store.get_task(task['id'])['status']=='uncertain'
        second=p.enqueue('two');next_job=p.claim(second)
        blocked=await p.prepare(next_job);assert blocked['ready'] is False and blocked['wait_seconds']>=3
        p.now+=3
        assert (await p.runtime.submission_turn(next_job,next_job['worker_id'],next_job['claim_token']))['ready']
        assert p.bridge.get(job['id'])['status']=='uncertain' and p.bridge.get(job['id'])['submit_id'] is None
        assert p.bridge.get(job['id'])['slot_released'] and len(p.store.active_credit_holds('one'))==1
    asyncio.run(run())


def test_expired_turn_never_activates_even_with_a_fresh_approved_quote(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        assert (await p.prepare(job))['ready'];p.now+=45;p.ready();p.now+=45;p.ready()
        quote={'source':'native_page','model':MODEL,'observed_at':p.now,'required_credits':20,'request_fingerprint':quote_request_fingerprint(job['request'])}
        assert (await p.runtime.credit_preflight(job,job['worker_id'],job['claim_token'],quote))['allowed']
        response=await p.runtime.activate_task(job,job['worker_id'],job['claim_token'])
        assert response['can_activate'] is False and response['generation_not_activated'] is True
        assert response['error_code']=='submission_turn_unavailable'
        assert p.bridge.get(job['id'])['activation_started_at'] is None
        value=await p.runtime.sync_task(task);assert value['status']=='failed' and not p.store.active_credit_holds()
    asyncio.run(run())


def test_late_old_report_cannot_modify_the_new_slot_claim(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();old=p.claim(task)
        await p.prepare(old);await p.activate(old);sid,_=await p.report(old);p.history(old,sid)
        await p.runtime.sync_task(task)
        new_task=p.enqueue();new=p.claim(new_task);assert new
        with pytest.raises(BridgeError):p.bridge.report(old['id'],old['worker_id'],old['claim_token'],status='uncertain',submit_id=str(uuid.uuid4()))
        assert p.bridge.get(new['id'])['status']=='claimed' and p.bridge.get(new['id'])['submit_id'] is None
        assert p.store.get_task(new_task['id'])['bridge_job_id']==new['id']
        assert p.bridge.get(old['id'])['submit_id']==sid and p.bridge.get(old['id'])['slot_released']
    asyncio.run(run())


@pytest.mark.parametrize('status',[20,30,50])
def test_manual_original_binding_verifies_then_releases_only_terminal_credit(tmp_path,monkeypatch,status):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        await p.prepare(job);await p.activate(job);await p.report(job,known=False,status='uncertain')
        await p.runtime.sync_task(task);selected=str(uuid.uuid4());p.history(job,selected,status=status)
        current=await p.runtime.manual_reconcile(task['id'],submit_id=selected,note='Checked original history')
        assert current['submit_id']==selected and len(p.bridge.list_jobs())==1
        if status==20:assert len(p.store.active_credit_holds())==1
        else:assert not p.store.active_credit_holds() and current['status'] in ('failed','completed')
        assert any(event['stage']=='manual_reconciliation' for event in p.store.list_events(task['id']))
        assert p.bridge.get(job['id'])['slot_released']
        assert p.store.list_keys()[0]['used_requests']==1
    asyncio.run(run())


@pytest.mark.parametrize('problem',['not_found','mismatched','replacement'])
def test_manual_review_cannot_release_using_missing_wrong_or_replacement_history(tmp_path,monkeypatch,problem):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        await p.prepare(job);await p.activate(job);sid,_=await p.report(job,known=problem=='replacement',status='uncertain')
        p.store.update_task(task['id'],status='uncertain',submit_id=sid)
        selected=str(uuid.uuid4())
        if problem!='not_found':p.history(job,selected,prompt='Mismatched' if problem=='mismatched' else None)
        with pytest.raises(PlatformError):await p.runtime.manual_reconcile(task['id'],submit_id=selected)
        assert len(p.store.active_credit_holds())==1 and not p.bridge.get(job['id'])['slot_released']
        assert len(p.bridge.list_jobs())==1 and p.store.list_keys()[0]['used_requests']==1
    asyncio.run(run())
