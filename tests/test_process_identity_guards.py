"""Reused PIDs are not old workers; all checks are read-only synthetic evidence."""
import asyncio
import json
import os
from pathlib import Path
import time

import pytest

import dreamina_api.browser_worker as worker_module
from dreamina_api.browser_worker import profile_lock
from dreamina_api.process_identity import process_snapshot
from test_managed_workers import Harness,run


def registry(h,**updates):
    path=h.worker.paths('first').registry;record=json.loads(path.read_text());record.update(updates);path.write_text(json.dumps(record));return record


def test_stopped_registry_pid_reuse_does_not_block_start_or_capacity(tmp_path):
    h=Harness(tmp_path,{'MANAGED_MAX_WORKERS':1});run(h.worker.start('first'))
    registry(h,stopped_at=h.time)
    # The recovered supervisor has no owned Popen. Its PID probe can be true
    # because an unrelated command in a new container now owns this number.
    recovered=h.supervisor();recovered.alive=lambda _:True
    assert recovered.status('first')['state']=='stopped'
    assert run(recovered.start('second'))['state']=='starting'
    assert len(h.calls)==2


def test_owned_live_popen_overrides_a_stale_stopped_marker(tmp_path):
    h=Harness(tmp_path);first=run(h.worker.start('first'));registry(h,stopped_at=h.time)
    assert run(h.worker.start('first'))['pid']==first['pid'] and len(h.calls)==1


@pytest.mark.parametrize('change',['module','config','account','extra_argument','birth'])
def test_readable_non_worker_pid_is_not_adopted_or_counted(tmp_path,change):
    h=Harness(tmp_path,{'MANAGED_MAX_WORKERS':1});run(h.worker.start('first'));h.report()
    snapshot=h.identity(1001);snapshot={'argv':list(snapshot['argv']),'identity':snapshot['identity']}
    if change=='module':snapshot['argv'][2]='http.server'
    if change=='config':snapshot['argv'][5]='/different/browser-worker.json'
    if change=='account':snapshot['argv'][7]='another-account'
    if change=='extra_argument':snapshot['argv'].append('--unknown')
    if change=='birth':snapshot['identity']='a-new-process-birth'
    recovered=h.supervisor();recovered.process_identity=lambda _:snapshot
    assert recovered.status('first')['state']=='stopped'
    assert run(recovered.start('second'))['state']=='starting'
    assert h.processes[1001].returncode is None  # No kill/terminate was attempted.


def test_exact_live_worker_can_be_adopted_but_unreadable_pid_stays_unverified(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'));h.report()
    recovered=h.supervisor();assert recovered.status('first')['state']=='running'
    recovered.process_identity=lambda _:None
    assert recovered.status('first')['state']=='running_unverified'
    assert run(recovered.start('first'))['state']=='running_unverified' and len(h.calls)==1


def test_matching_birth_without_command_supports_new_registry_only(tmp_path):
    h=Harness(tmp_path);run(h.worker.start('first'));h.report();recovered=h.supervisor()
    recovered.process_identity=lambda _: {'identity':'fixture-birth-1001'}
    assert recovered.status('first')['state']=='running'
    path=h.worker.paths('first').registry;record=json.loads(path.read_text());record.pop('process_birth_id');path.write_text(json.dumps(record))
    assert recovered.status('first')['state']=='running_unverified'


def test_current_process_has_birth_evidence_when_host_allows_it():
    snapshot=process_snapshot(os.getpid())
    if snapshot is None:pytest.skip('Host does not expose process birth information')
    assert snapshot.get('identity') and 0<snapshot['created_at']<=time.time()+2


@pytest.mark.parametrize('old_identity,current_identity,should_reuse',[
    ('linux:old-namespace:old-start','linux:new-namespace:new-start',True),
    ('same-birth','same-birth',False),('same-birth',None,False),
])
def test_json_profile_lock_uses_birth_identity_not_pid_number(tmp_path,monkeypatch,old_identity,current_identity,should_reuse):
    async def check():
        lock=tmp_path/'.dreamina-worker.lock';original={'pid':12345,'process_identity':old_identity,'token':'old-token'};lock.write_text(json.dumps(original))
        monkeypatch.setattr(worker_module,'process_alive',lambda _:True)
        monkeypatch.setattr(worker_module,'process_snapshot',lambda pid:({'identity':current_identity} if pid==12345 else {'identity':'current-worker-birth'}))
        if should_reuse:
            async with profile_lock(tmp_path):
                active=json.loads(lock.read_text());assert active['pid']==os.getpid() and active['process_identity']=='current-worker-birth'
            assert not lock.exists()
        else:
            with pytest.raises(RuntimeError,match='already owned'):
                async with profile_lock(tmp_path):pass
            assert json.loads(lock.read_text())==original
    asyncio.run(check())


@pytest.mark.parametrize('created_offset,should_reuse',[(20,True),(-20,False),(None,False),(float('inf'),False)])
def test_legacy_pid_lock_only_reused_with_reliable_newer_creation(tmp_path,monkeypatch,created_offset,should_reuse):
    async def check():
        lock=tmp_path/'.dreamina-worker.lock';lock.write_text('12345');stamp=lock.stat().st_mtime
        monkeypatch.setattr(worker_module,'process_alive',lambda _:True)
        def snapshot(pid):
            if pid!=12345:return {'identity':'new-worker'}
            return {} if created_offset is None else {'created_at':stamp+created_offset}
        monkeypatch.setattr(worker_module,'process_snapshot',snapshot)
        if should_reuse:
            async with profile_lock(tmp_path):assert json.loads(lock.read_text())['pid']==os.getpid()
        else:
            with pytest.raises(RuntimeError,match='already owned'):
                async with profile_lock(tmp_path):pass
            assert lock.read_text()=='12345'
    asyncio.run(check())


@pytest.mark.parametrize('raw',['bad-json','{}','{"pid":true}','{"pid":0}','{"pid":12,"process_identity":false}'])
def test_malformed_profile_lock_is_never_silently_deleted(tmp_path,monkeypatch,raw):
    async def check():
        lock=tmp_path/'.dreamina-worker.lock';lock.write_text(raw)
        monkeypatch.setattr(worker_module,'process_alive',lambda _:False)
        with pytest.raises(RuntimeError,match='metadata is invalid'):
            async with profile_lock(tmp_path):pass
        assert lock.read_text()==raw
    asyncio.run(check())


def test_profile_release_does_not_unlink_replaced_owner_marker(tmp_path):
    async def check():
        lock=tmp_path/'.dreamina-worker.lock'
        async with profile_lock(tmp_path):lock.write_text('another-owner-marker')
        assert lock.read_text()=='another-owner-marker'
    asyncio.run(check())
