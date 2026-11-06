"""Shrinking live browser capacity drains only the retired slots, without replay."""
import asyncio,json,time
from types import SimpleNamespace

import pytest

from dreamina_api.browser_worker import write_json
from dreamina_api.platform_store import PlatformError
from test_worker_reliability import worker,job
from test_managed_workers import Harness,run


class Page:
    def __init__(self,close_works=True):self.closed=False;self.close_works=close_works;self.close_calls=0
    async def close(self):self.close_calls+=1;self.closed=self.close_works
    def is_closed(self):return self.closed


def test_busy_shrink_writes_only_instance_bound_retirement_request(tmp_path):
    h=Harness(tmp_path);h.store.accounts['first']['max_concurrent']=3;run(h.worker.start('first'))
    h.store.accounts['first'].update(max_concurrent=1,active_tasks=1)
    run(h.worker.reconfigure('first'));p=h.worker.paths('first')
    value=json.loads((p.state/'slot-retirement.json').read_text())
    assert value['concurrency']==1 and value['instance_id']==json.loads(p.registry.read_text())['instance_id']
    assert not p.stop.exists() and len(h.calls)==1
    run(h.worker.scan());assert not p.stop.exists() and len(h.calls)==1


def test_monitor_ignores_retirement_for_old_process_instance(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config.update(concurrency=3,instance_id='current');slots=owner.prepare_slots()
        try:
            write_json(tmp_path/'slot-retirement.json',{'instance_id':'previous','concurrency':1})
            owner._read_slot_retirement();assert owner.config['concurrency']==3 and not any(s._retired for s in slots)
            write_json(tmp_path/'slot-retirement.json',{'instance_id':'current','concurrency':1})
            owner._read_slot_retirement();assert owner.config['concurrency']==1 and [s._retired for s in slots]==[False,True,True]
        finally:
            for s in slots:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_busy_slot_finishes_original_assignment_then_closes_without_claiming_again(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;slots=owner.prepare_slots();slot=slots[1]
        entered=asyncio.Event();release=asyncio.Event();calls=[];page=Page();current=job();slot._retained_page=page
        async def recovered(*_):return True
        async def api(path,body=None,**_):
            calls.append(path)
            return {'job':current} if path=='/claim' else {'retired':True} if path=='/workers/retire' else {}
        async def handle(original):
            assert original is current;write_json(slot.journal,{'phase':'click_intent','job':original})
            entered.set();await release.wait();write_json(slot.journal,{'phase':'finished','job_id':original['id']});slot.active=None;return True
        owner._await_slot_recovery=recovered;slot.api=api;slot.handle=handle
        control={'stop':asyncio.Event(),'claim_lock':asyncio.Lock(),'readiness':{'ready':True},'claimed':0,'pending_claims':0,'counted_job_ids':set()}
        task=asyncio.create_task(owner._run_slot(slot,control))
        try:
            await asyncio.wait_for(entered.wait(),1);owner._apply_slot_limit(1)
            assert slot._retired and not page.closed and not task.done()
            owner._pool_summary(control)
            assert owner._last_status_data['effective_concurrency']==2 and owner._last_status_data['target_concurrency']==1
            assert owner._last_status_data['retiring_slots']==1
            release.set();await asyncio.wait_for(task,1)
            assert calls.count('/claim')==1 and calls.count('/workers/retire')==1 and page.closed
            assert slot not in owner._slots and (slot.state_dir/'retired.json').is_file()
            owner._pool_summary(control)
            assert owner._last_status_data['effective_concurrency']==1 and owner._last_status_data['retiring_slots']==0
            assert json.loads(slot.journal.read_text())['job_id']==current['id']
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            for s in slots:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_retired_slot_keeps_unknown_journal_and_unconfirmed_page(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;slots=owner.prepare_slots();slot=slots[1];page=Page(False);slot._retained_page=page
        write_json(slot.journal,{'phase':'click_intent','job':job()});original=slot.journal.read_bytes()
        try:
            assert not await owner._finish_retired_slot(slot) and page.close_calls==0
            assert slot.journal.read_bytes()==original and slot in owner._slots
            write_json(slot.journal,{'phase':'finished'})
            assert not await owner._finish_retired_slot(slot) and page.close_calls==1 and slot._retained_page is page
            assert not (slot.state_dir/'retired.json').exists()
        finally:
            for s in slots:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_heartbeat_limit_prevents_an_idle_excess_slot_from_claiming(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;slots=owner.prepare_slots();slot=slots[1];calls=[]
        async def api(path,body=None,**_):calls.append(path);return {'concurrency_limit':1} if path=='/heartbeat' else {'retired':True}
        async def recovered(*_):return True
        owner._await_slot_recovery=recovered;slot.api=api
        try:
            await owner._run_slot(slot,{'stop':asyncio.Event(),'readiness':{'ready':True}})
            assert '/claim' not in calls and '/workers/retire' in calls and slot not in owner._slots
        finally:
            for s in slots:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_managed_resize_remains_pending_for_retiring_recovery_slots(tmp_path):
    h=Harness(tmp_path);h.store.accounts['first']['max_concurrent']=1;run(h.worker.start('first'))
    h.report(effective_concurrency=2,target_concurrency=1,retiring_slots=1,active_slots=1)
    status=h.worker.status('first')
    assert status['effective_concurrency']==2 and status['target_concurrency']==1 and status['retiring_slots']==1
    assert status['configuration_pending'] is True
    h.report(effective_concurrency=1,target_concurrency=1,retiring_slots=0,active_slots=0)
    assert h.worker.status('first')['configuration_pending'] is False


def test_expansion_rotates_only_safely_retired_slot_identity_and_keeps_history(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;slots=owner.prepare_slots();slot=slots[1];previous=slot.worker_id
        write_json(slot.journal,{'phase':'finished'});write_json(slot.state_dir/'retired.json',{'worker_id':previous})
        history=slot.state_dir/'history'/'receipt.json';write_json(history,{'submit_id':'retained'})
        for s in slots:await s.http.aclose()
        recreated=owner.prepare_slots()
        try:
            assert recreated[0].worker_id==owner.worker_id and recreated[1].worker_id!=previous
            assert json.loads(history.read_text())['submit_id']=='retained'
        finally:
            for s in recreated:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_transient_retired_page_close_is_retried_without_any_claim(tmp_path):
    async def check():
        owner=worker(tmp_path);owner.config['concurrency']=2;slots=owner.prepare_slots();slot=slots[1];slot._retired=True;calls=[];page=Page(False);slot._retained_page=page
        async def recovered(*_):return True
        async def api(path,body=None,**_):calls.append(path);return {'retired':True}
        async def backoff(control,attempt):assert attempt==1;page.close_works=True
        owner._await_slot_recovery=recovered;owner._recovery_sleep=backoff;slot.api=api
        try:
            await asyncio.wait_for(owner._run_slot(slot,{'stop':asyncio.Event()}),1)
            assert page.close_calls==2 and page.closed and '/claim' not in calls and '/workers/retire' in calls
        finally:
            for s in slots:await s.http.aclose()
            await owner.http.aclose()
    asyncio.run(check())


def test_remove_node_waits_for_normal_exit_prunes_and_retains_recovery_files(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'));p=h.worker.paths('first');h.report();old=h.processes[1001];pruned=[]
    h.bridge.prune_workers=lambda aid,**kw:pruned.append((aid,kw)) or {'removed':1,'preserved':0}
    journal=p.state/'current.json';write_json(journal,{'phase':'finished'});h.on_sleep=lambda:setattr(old,'returncode',0)
    result=run(h.worker.remove_node('first'))
    assert result['state']=='removed' and not p.registry.exists() and not p.status.exists()
    assert journal.exists() and p.auth.exists() and (p.directory/'retired-node.json').exists()
    assert pruned==[('first',{'keep_slots':0,'process_stopped':True})]
    assert json.loads((p.directory/'supervision.json').read_text())['desired_running'] is False


@pytest.mark.parametrize('guard',['busy','profile_lock','stop_timeout','browser_close_timeout'])
def test_remove_node_never_discards_registration_before_safe_exit(tmp_path,guard):
    h=Harness(tmp_path);run(h.worker.start('first'));p=h.worker.paths('first');old=h.processes[1001]
    if guard=='busy':h.store.accounts['first']['active_tasks']=1
    elif guard=='profile_lock':
        p.profile.mkdir(parents=True,exist_ok=True);(p.profile/'SingletonLock').write_text('unknown browser owner')
        h.on_sleep=lambda:setattr(old,'returncode',0)
    elif guard=='browser_close_timeout':
        write_json(p.state/'shutdown.json',{'state':'shutdown_incomplete','errors':['browser_close_timeout'],'observed_at':h.time})
        h.on_sleep=lambda:setattr(old,'returncode',0)
    with pytest.raises(PlatformError):run(h.worker.remove_node('first'))
    assert p.registry.exists() and p.auth.exists() and not (p.directory/'retired-node.json').exists()
