"""Disposable stopped-profile cleanup; credentials and unknown work are preserved."""
import json,os,time,uuid
from pathlib import Path

import pytest

from dreamina_api.maintenance import ManagedMaintenance
from test_managed_workers import Harness,run


def fixture(tmp_path,config=None):
    h=Harness(tmp_path);h.time=2_000_000_000
    task=ManagedMaintenance(h.worker,clock=lambda:h.time,config=config)
    return h,task


def old(path,body=b'disposable',now=2_000_000_000):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body);os.utime(path,(now-8*86400,now-8*86400));return path


def test_only_old_allowlisted_cache_files_are_deleted(tmp_path):
    h,clean=fixture(tmp_path);p=h.worker.paths('first')
    discarded=old(p.profile/'Default/Cache/Cache_Data/data_0')
    recent=p.profile/'Default/Cache/new';recent.write_bytes(b'new');os.utime(recent,(h.time,h.time))
    kept=[old(p.profile/'Default/Cookies',b'cookie'),old(p.profile/'Default/Local Storage/state'),old(p.state/'current.json'),old(p.auth,b'login snapshot')]
    report=run(clean.run_once())
    assert not discarded.exists() and recent.exists() and all(x.exists() for x in kept)
    assert report['files_deleted']==1 and report['bytes_reclaimed']==len(b'disposable')
    assert report['disk_free_bytes']>0


@pytest.mark.parametrize('guard',['running','active','login','browser_lock','bridge'])
def test_live_or_unresolved_accounts_are_never_cleaned(tmp_path,guard):
    h,clean=fixture(tmp_path);p=h.worker.paths('first');file=old(p.profile/'Default/Cache/data')
    if guard=='running':run(h.worker.start('first'))
    elif guard=='active':h.store.accounts['first']['active_tasks']=1
    elif guard=='login':h.store.accounts['first']['status']='login_pending'
    elif guard=='browser_lock':old(p.profile/'SingletonLock')
    else:h.bridge.records=[{'account_id':'first','jobs':{'uncertain':1}}]
    report=run(clean.run_once());assert file.exists() and report['files_deleted']==0


def test_diagnostics_require_exact_terminal_owned_job_and_keep_journals(tmp_path):
    h,clean=fixture(tmp_path);p=h.worker.paths('first');ids=[str(uuid.uuid4()) for _ in range(4)]
    files=[old(p.state/'slots/1/diagnostics'/sid/'attempt-1/screen.png') for sid in ids]
    states={ids[0]:{'status':'completed','account_id':'first'},ids[1]:{'status':'uncertain','account_id':'first'},ids[2]:{'status':'failed','account_id':'second'}}
    h.bridge.get=lambda sid:states[sid]
    journal=old(p.state/'slots/1/current.json');history=old(p.state/'slots/1/history'/(ids[0]+'.json'))
    report=run(clean.run_once())
    assert not files[0].exists() and all(f.exists() for f in files[1:]) and journal.exists() and history.exists()
    assert report['files_deleted']==1


def test_cleanup_budget_bounds_work_and_leaves_large_file(tmp_path):
    h,clean=fixture(tmp_path,{'MAINTENANCE_MAX_FILES':2,'MAINTENANCE_MAX_MIB':1});p=h.worker.paths('first')
    files=[old(p.profile/'Default/Cache'/str(i),b'a') for i in range(5)]
    report=run(clean.run_once());assert report['files_examined']==2 and report['files_deleted']==2 and report['budget_reached']
    large=old(p.profile/'Default/GPUCache/large',b'a'*(2*1024*1024))
    other=ManagedMaintenance(h.worker,clock=lambda:h.time,config={'MAINTENANCE_MAX_MIB':1})
    run(other.run_once());assert large.exists()


def test_old_stopped_logs_keep_recent_tail(tmp_path):
    h,clean=fixture(tmp_path);p=h.worker.paths('first')
    log=old(p.directory/'worker.stderr.log',b'a'*(11*1024*1024)+b'last')
    unknown=old(p.directory/'custom.log',b'do not touch')
    report=run(clean.run_once());assert log.stat().st_size==1024*1024 and log.read_bytes().endswith(b'last')
    assert unknown.read_bytes()==b'do not touch' and report['logs_trimmed']==1


def test_symlink_escape_and_internal_auth_symlink_are_preserved(tmp_path):
    h,clean=fixture(tmp_path/'managed');p=h.worker.paths('first');outside=old(tmp_path/'outside/data',b'protected')
    cache=p.profile/'Default/Cache';cache.parent.mkdir(parents=True)
    try:cache.symlink_to(outside.parent,target_is_directory=True)
    except OSError:pytest.skip('Symlink privileges unavailable')
    run(clean.run_once());assert outside.read_bytes()==b'protected'
    cache.unlink();cache.mkdir();link=cache/'auth.enc';link.symlink_to(p.auth)
    run(clean.run_once());assert link.is_symlink() and p.auth.exists()


def test_no_cleanup_path_starts_or_stops_workers(tmp_path,monkeypatch):
    h,clean=fixture(tmp_path)
    async def forbidden(*_,**__):raise AssertionError('Maintenance must not change lifecycle')
    monkeypatch.setattr(h.worker,'start',forbidden);monkeypatch.setattr(h.worker,'stop',forbidden)
    run(clean.run_once());assert not h.calls
