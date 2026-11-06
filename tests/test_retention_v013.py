"""Opt-in central retention: exact ownership, terminal age, reference guards."""
import asyncio,json,os,uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import insert,update

from dreamina_api.maintenance import ManagedMaintenance
from dreamina_api.platform_store import tasks
from test_account_login import make_store
from test_managed_workers import Harness,run


def setup(tmp_path,config=None):
    store,bridge=make_store(tmp_path);key=store.create_key({'name':'retention-fixture'})['item']['id'];now=2_000_000_000
    workers=SimpleNamespace(store=store,bridge=bridge,root=tmp_path/'managed',_lock=asyncio.Lock())
    clean=ManagedMaintenance(workers,clock=lambda:now,outputs_root=tmp_path/'outputs',config=config)
    def add(status='completed',age=40,reference_to=None):
        sid='video_'+uuid.uuid4().hex;asset='asset_0_'+'a'*24
        request={'references':[{'asset_id':asset}]}
        if reference_to:request['asset_owner_task_id']=reference_to
        path=clean.outputs_root/'media'/(sid+'.mp4')
        data=dict(id=sid,kind='video',model='seedance-2.0-mini',status=status,account_id='one',key_id=key,
                  request_json=json.dumps(request),request_hash=uuid.uuid4().hex,dispatch_claimed=True,progress=100,
                  output_path=str(path),created_at=now-age*86400,updated_at=now-age*86400,
                  completed_at=now-age*86400 if status=='completed' else None)
        with store.transaction() as conn:conn.execute(insert(tasks).values(**data))
        paths=[path,clean.outputs_root/'platform-inputs'/sid/'references'/(asset+'.bin')]
        for file in paths:
            file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'fixture');os.utime(file,(now-40*86400,now-40*86400))
        return sid,paths
    def collect():
        report=dict(files_examined=0,files_deleted=0,bytes_reclaimed=0,budget_reached=False,errors=0)
        clean._central_cursor='';clean._central_pass(report);return report
    return store,bridge,clean,add,collect,now


def test_central_retention_is_disabled_by_default(tmp_path):
    store,_,_,add,collect,_=setup(tmp_path)
    try:
        _,paths=add();assert collect()['files_deleted']==0 and all(p.exists() for p in paths)
    finally:store.engine.dispose()


@pytest.mark.parametrize('value',[-1,1,29,3651,True,'invalid'])
def test_central_retention_rejects_unsafe_or_invalid_periods(tmp_path,value):
    h=Harness(tmp_path)
    with pytest.raises(ValueError):ManagedMaintenance(h.worker,config={'MAINTENANCE_MEDIA_RETENTION_DAYS':value})


def test_central_retention_only_evicts_known_expired_terminal_files(tmp_path):
    store,_,clean,add,collect,now=setup(tmp_path,{'MAINTENANCE_MEDIA_RETENTION_DAYS':30,'MAINTENANCE_INPUT_RETENTION_DAYS':30})
    try:
        _,expired=add();_,fresh_terminal=add(age=5);_,active=add('in_progress');_,fresh_file=add()
        for p in fresh_file:os.utime(p,(now,now))
        unknown=clean.outputs_root/'media'/'unknown.mp4';unknown.write_bytes(b'keep')
        unknown_ref=expired[1].parent/'asset_1_aaaaaaaaaaaaaaaaaaaaaaaa.bin';unknown_ref.write_bytes(b'unknown')
        report=collect()
        assert report['central_files_deleted']==2 and not any(p.exists() for p in expired)
        assert all(p.exists() for p in [*fresh_terminal,*active,*fresh_file,unknown,unknown_ref])
        assert store.get_task(expired[0].stem)['output_path']==str(expired[0])  # /content can refetch original ID.
    finally:store.engine.dispose()


def test_active_platform_and_bridge_references_retain_terminal_sources(tmp_path):
    store,bridge,_,add,collect,now=setup(tmp_path,{'MAINTENANCE_MEDIA_RETENTION_DAYS':30,'MAINTENANCE_INPUT_RETENTION_DAYS':30})
    try:
        sid,paths=add();active,_=add('queued',reference_to=sid)
        assert collect()['files_deleted']==0 and all(p.exists() for p in paths)
        with store.transaction() as conn:conn.execute(update(tasks).where(tasks.c.id==active).values(request_json='{}'))
        with bridge._connection() as db:
            db.execute('INSERT INTO jobs(id,account_id,request_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (str(uuid.uuid4()),'one',json.dumps({'asset_owner_task_id':sid}),'uncertain',now,now))
        assert collect()['files_deleted']==0 and all(p.exists() for p in paths)
        with bridge._connection() as db:db.execute("UPDATE jobs SET status='failed'")
        assert collect()['files_deleted']==2 and not any(p.exists() for p in paths)
    finally:store.engine.dispose()


def test_escaped_output_record_never_authorizes_deletion(tmp_path):
    store,_,clean,add,collect,now=setup(tmp_path,{'MAINTENANCE_MEDIA_RETENTION_DAYS':30})
    try:
        sid,paths=add();outside=tmp_path/'protected.mp4';outside.write_bytes(b'keep');os.utime(outside,(now-40*86400,now-40*86400))
        with store.transaction() as conn:conn.execute(update(tasks).where(tasks.c.id==sid).values(output_path=str(outside)))
        assert collect()['files_deleted']==0 and outside.exists() and all(p.exists() for p in paths)
    finally:store.engine.dispose()
