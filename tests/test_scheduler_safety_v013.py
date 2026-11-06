"""Crash, reassignment and persistence boundaries using real local stores."""
import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest
from dreamina_api.platform_store import PlatformError,PlatformStore
from dreamina_api.chrome_bridge import BridgeStore
from dreamina_api.platform_runtime import PlatformRuntime
from test_pipeline_integration_v6 import Pipeline,MODEL
from test_inflight_submission_pacing import fixture,turn,commit,finish


def test_activated_worker_crash_cannot_block_other_accounts_after_restart(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);first=p.enqueue();job=p.claim(first)
        await p.prepare(job);await p.activate(job)
        # No receipt at all: simulate the crash by creating a new bridge process.
        restarted=BridgeStore(p.bridge.directory,clock=lambda:p.now)
        runtime=PlatformRuntime(p.store,restarted,clock=lambda:p.now)
        second=p.enqueue('two');second_job=p.claim(second)
        p.now+=3
        await runtime.prepare_task(second_job,second_job['worker_id'],second_job['claim_token'])
        assert (await runtime.submission_turn(second_job,second_job['worker_id'],second_job['claim_token']))['ready']
        assert p.store.get_account('one')['reserved_credits']==20
        assert p.bridge.get(job['id'])['activation_started_at'] is not None
        with pytest.raises(PlatformError) as rejected:
            await runtime.release_gate('account:one',note='No closed-page evidence yet')
        assert rejected.value.code=='gate_release_unsafe'
        assert len(p.store.list_tasks())==2 and len(p.store.active_credit_holds())==1
    asyncio.run(run())


def test_admin_closed_page_gate_release_retains_unknown_task_and_credit(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        await p.prepare(job);await p.activate(job)
        closed=p.bridge.report(job['id'],job['worker_id'],job['claim_token'],status='uncertain',
            error='Owned browser closed before a receipt was recovered',submission_window_closed=True)
        assert closed['slot_released']
        value=await p.runtime.release_gate('account:one',note='Verified original page is closed')
        assert value['credit_released'] is False and value['task_resubmitted'] is False
        assert len(p.store.active_credit_holds())==1 and len(p.bridge.list_jobs())==1
        assert p.store.get_task(task['id'])['status'] not in ('completed','failed')
        assert any(event['stage']=='operator_gate_release' for event in p.store.list_events(task['id']))
    asyncio.run(run())


def test_unactivated_cooling_job_reassigns_same_logical_request_once(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        p.store.reserve_task_credits(task['id'],job['id'],required_credits=20,balance=100,request_fingerprint='a'*64)
        before=p.store.list_keys()[0]['used_requests']
        p.store.record_submission_risk('one',MODEL,'prior-risk')
        result=await p.runtime.sync_task(task)
        assert result['id']==task['id'] and result['account_id']=='two' and result['status']=='queued'
        assert result['bridge_job_id']!=job['id'] and p.bridge.get(job['id'])['status']=='failed'
        assert not p.store.active_credit_holds('one') and p.store.list_keys()[0]['used_requests']==before
        repeated=await p.runtime.sync_task(result)
        assert repeated['bridge_job_id']==result['bridge_job_id'] and len(p.bridge.list_jobs())==2
        assert len(repeated['request_detail']['availability_attempts'])==1
    asyncio.run(run())


@pytest.mark.parametrize('key_change',[{'account_ids':['one']},{'allowed_models':['seedance-2.5']},{'enabled':False}])
def test_unactivated_failover_never_expands_key_permissions(tmp_path,monkeypatch,key_change):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        p.store.update_key(p.principal['id'],key_change)
        p.store.record_submission_risk('one',MODEL,'risk')
        result=await p.runtime.sync_task(task)
        assert result['status']=='failed' and result['account_id']=='one' and len(p.bridge.list_jobs())==1
        assert result['id'] not in p.runtime._locks and result['id'] not in p.runtime._polls
    asyncio.run(run())


def test_cooldown_does_not_reassign_activated_unknown_task(tmp_path,monkeypatch):
    async def run():
        p=Pipeline(tmp_path,monkeypatch);task=p.enqueue();job=p.claim(task)
        await p.prepare(job);await p.activate(job)
        p.store.record_submission_risk('one',MODEL,'another-risk')
        result=await p.runtime.sync_task(task)
        assert result['account_id']=='one' and result['bridge_job_id']==job['id']
        assert len(p.bridge.list_jobs())==1 and len(p.store.active_credit_holds())==1
    asyncio.run(run())


def test_risk_clear_preserves_independent_permission_block_and_holds(tmp_path):
    e=fixture(tmp_path);task=e.new()
    e.store.reserve_task_credits(task['id'],task['bridge_job_id'],required_credits=20,balance=100,request_fingerprint='a'*64)
    e.store.block_account_model('a','seedance-2.5',reason='account_plan_required',cooldown_seconds=1800)
    e.store.record_submission_risk('a',MODEL,'one')
    e.store.clear_risk_cooldown('a',note='Reviewed temporary rejection')
    assert e.store.get_account('a')['risk_cooldown_until'] is None
    assert [x['reason'] for x in e.store.account_model_blocks()]==['account_plan_required']
    assert len(e.store.active_credit_holds())==1


def test_finished_gate_read_does_not_take_scheduler_write_lock(tmp_path,monkeypatch):
    e=fixture(tmp_path);task=e.new();turn(e,task);commit(e,task);first=finish(e,task)
    @contextmanager
    def forbidden():
        raise AssertionError('Finished pacing polling must not take a write transaction')
        yield
    monkeypatch.setattr(e.store,'transaction',forbidden)
    assert finish(e,task)==first


def test_identical_task_update_does_not_change_timestamp_or_take_write_lock(tmp_path,monkeypatch):
    from sqlalchemy import event
    e=fixture(tmp_path);task=e.new();writes=[]
    event.listen(e.store.engine,'before_cursor_execute',lambda conn,cursor,statement,*args:writes.append(statement) if statement.lstrip().upper().startswith('UPDATE PLATFORM_TASKS') else None)
    e.clock.now+=5
    @contextmanager
    def forbidden():
        raise AssertionError('Unchanged observation must not take a scheduler transaction')
        yield
    monkeypatch.setattr(e.store,'transaction',forbidden)
    unchanged=e.store.update_task(task['id'],status='claimed')
    assert unchanged['updated_at']==task['updated_at'] and writes==[]


def test_password_hash_work_does_not_hold_scheduler_transaction(tmp_path,monkeypatch):
    e=fixture(tmp_path);e.store.bootstrap_admin('admin','synthetic-password-123')
    entered=threading.Event();release=threading.Event();original=e.store._password_matches
    def slow(password,encoded):
        entered.set();assert release.wait(5);return original(password,encoded)
    monkeypatch.setattr(e.store,'_password_matches',slow)
    with ThreadPoolExecutor(2) as executor:
        login=executor.submit(e.store.login_admin,'admin','synthetic-password-123')
        assert entered.wait(5)
        try:
            reserve=executor.submit(e.new)
            assert reserve.result(timeout=2)['status']=='claimed'
        finally:release.set()
        assert login.result(timeout=5)['user']['username']=='admin'
