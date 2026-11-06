"""Offline supervisor fault injection; no native processes or website calls."""
import json
import asyncio
from pathlib import Path

import pytest

from dreamina_api.managed_workers import ManagedWorkers
from dreamina_api.platform_store import PlatformError
from test_managed_workers import Harness,run


def state(h,aid='first'):
    return json.loads((h.worker.paths(aid).directory/'supervision.json').read_text())


def ready(h):
    h.report();run(h.worker.scan())


def crash(h,aid='first'):
    record=h.worker._record(aid);h.processes[record['pid']].returncode=1


def test_crash_backoff_is_persisted_and_new_supervisor_resumes_same_journal(tmp_path):
    h=Harness(tmp_path);h.worker.jitter=lambda:0
    first=run(h.worker.start('first'));ready(h)
    journal=h.worker.paths('first').state/'slots/1/current.json';journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'phase':'request_observed','job':{'id':'preserve-original'},'submit_id':'do-not-repeat'}))
    h.store.accounts['first']['active_tasks']=1
    crash(h);run(h.worker.scan());saved=state(h)
    assert saved['desired_running'] is True and saved['failures']==1 and saved['next_restart_at']==h.time+5
    run(h.worker.scan());assert len(h.calls)==1
    h.worker=h.supervisor();h.worker.jitter=lambda:0
    h.time+=5;run(h.worker.scan())
    assert len(h.calls)==2 and h.worker._record('first')['pid']!=first['pid']
    assert 'preserve-original' in journal.read_text() and 'do-not-repeat' in journal.read_text()
    assert state(h)['failures']==1 and state(h)['next_restart_at'] is None


def test_manual_stop_is_durable_and_never_started_accounts_remain_off(tmp_path):
    h=Harness(tmp_path);first=run(h.worker.start('first'))
    h.on_sleep=lambda:setattr(h.processes[first['pid']],'returncode',0)
    run(h.worker.stop('first'));assert state(h)['desired_running'] is False
    h.worker=h.supervisor();h.time+=1000
    for _ in range(3):run(h.worker.scan())
    assert len(h.calls)==1
    assert not (h.worker.paths('second').directory/'supervision.json').exists()


def test_busy_stop_does_not_clear_desired_recovery(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'));h.store.accounts['first']['active_tasks']=1
    with pytest.raises(PlatformError):run(h.worker.stop('first'))
    assert state(h)['desired_running'] is True


def test_legacy_graceful_stop_migrates_off_and_legacy_crash_migrates_on(tmp_path):
    h=Harness(tmp_path);first=run(h.worker.start('first'));path=h.worker.paths('first')
    (path.directory/'supervision.json').unlink();crash(h)
    record=json.loads(path.registry.read_text());record['stopped_at']=h.time;path.registry.write_text(json.dumps(record))
    run(h.worker.scan());assert state(h)['desired_running'] is False
    (path.directory/'supervision.json').unlink();record.pop('stopped_at');path.registry.write_text(json.dumps(record))
    run(h.worker.scan());assert state(h)['desired_running'] is True and state(h)['failures']==1


def test_exponential_restarts_stop_at_circuit_and_manual_start_resets(tmp_path):
    h=Harness(tmp_path,{'MANAGED_RESTART_LIMIT':3});h.worker.jitter=lambda:0
    run(h.worker.start('first'))
    for expected,delay in ((1,5),(2,10)):
        crash(h);run(h.worker.scan());saved=state(h)
        assert saved['failures']==expected and saved['next_restart_at']==h.time+delay
        h.time+=delay;run(h.worker.scan())
    crash(h);run(h.worker.scan());assert state(h)['circuit_open'] is True
    count=len(h.calls);h.time+=1000;run(h.worker.scan());assert len(h.calls)==count
    assert h.worker.status('first')['restart_circuit_open'] is True
    run(h.worker.start('first'));assert len(h.calls)==count+1 and state(h)['failures']==0


def test_stable_ready_period_resets_crash_counter(tmp_path):
    h=Harness(tmp_path);h.worker.jitter=lambda:0;run(h.worker.start('first'));crash(h);run(h.worker.scan())
    h.time+=5;run(h.worker.scan());ready(h);assert state(h)['failures']==1
    h.time+=119;ready(h);assert state(h)['failures']==1
    h.time+=1;ready(h);assert state(h)['failures']==0


@pytest.mark.parametrize('status',['login_required','verification_required','expired','error','login_pending'])
def test_account_attention_prevents_automatic_restart(tmp_path,status):
    h=Harness(tmp_path);run(h.worker.start('first'));crash(h)
    h.store.accounts['first']['status']=status;h.time+=500;run(h.worker.scan());h.time+=500;run(h.worker.scan())
    assert len(h.calls)==1


def test_capacity_wait_is_not_a_crash_and_manual_intent_can_resume(tmp_path):
    h=Harness(tmp_path,{'MANAGED_MAX_WORKERS':1})
    first=run(h.worker.start('first'));run(h.worker.start('second'))
    assert state(h,'second')['desired_running'] and state(h,'second')['failures']==0
    h.on_sleep=lambda:setattr(h.processes[first['pid']],'returncode',0)
    run(h.worker.stop('first'));h.time+=15;run(h.worker.scan())
    assert len(h.calls)==2 and h.calls[-1][0][-1]=='second' and state(h,'second')['failures']==0


def test_spawn_failures_have_bounded_backoff_without_duplicate_count_per_scan(tmp_path):
    h=Harness(tmp_path);h.worker.jitter=lambda:0;run(h.worker.start('first'));crash(h);run(h.worker.scan())
    def bad(*_,**__):raise OSError('secret diagnostic not public')
    h.worker.process_factory=bad;h.time+=5;run(h.worker.scan())
    saved=state(h);assert saved['failures']==2 and saved['next_restart_at']==h.time+10
    run(h.worker.scan());assert state(h)['failures']==2
    assert 'secret diagnostic' not in json.dumps(h.worker.status('first'))


def test_unverified_live_process_is_never_restarted_or_killed(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'));h.worker=h.supervisor()
    h.worker.process_identity=lambda _:None
    h.time+=1000;run(h.worker.scan());assert len(h.calls)==1 and h.worker.status('first')['state']=='running_unverified'


def test_corrupt_supervision_blocks_only_affected_account(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'));run(h.worker.start('second'))
    (h.worker.paths('first').directory/'supervision.json').write_text('bad-json')
    crash(h,'second');result=run(h.worker.scan())
    assert result[0]['state']=='supervision_attention' and state(h,'second')['failures']==1
    with pytest.raises(PlatformError):run(h.worker.start('first'))


@pytest.mark.parametrize('config',[{'MANAGED_RESTART_LIMIT':0},{'MANAGED_RESTART_LIMIT':True},{'MANAGED_RESTART_STABLE_SECONDS':0}])
def test_invalid_restart_configuration_fails_early(tmp_path,config):
    with pytest.raises(ValueError):Harness(tmp_path,config)
