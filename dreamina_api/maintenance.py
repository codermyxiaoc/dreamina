"""Bounded maintenance of disposable files in stopped managed profiles only.

This module never stops a worker, removes login storage, or truncates an active
journal. Central media/input eviction is opt-in. Symlinks and directory junctions are not
followed. The supervisor lock excludes managed starts during a cleanup pass.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from .managed_workers import _positive_int
from .platform_store import PlatformError
from .config import ROOT

CACHE_ROOTS=('Default/Cache','Default/Code Cache','Default/GPUCache','GPUCache')
JOB_ID=re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')


def _safe(path:Path,root:Path):
    """Every existing ancestor must be a real path beneath the intended root."""
    try:
        absolute=path.absolute();base=root.absolute()
        if not absolute.is_relative_to(base) or not absolute.resolve().is_relative_to(base.resolve()):return False
        for item in (absolute,*absolute.parents):
            if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):return False
            if item==base:break
        return True
    except (OSError,ValueError):return False


def cleanup_profile_cache(profile,*,root=None,owned_lock=False,threshold_bytes=256*1024*1024,max_files=20000,max_bytes=2*1024**3):
    """Trim oversized disposable caches before launching an idle owned browser.

    Caller holds profile_lock when owned_lock=True. No cookies, IndexedDB,
    local storage, session snapshots, or job files are in this allowlist.
    """
    profile=Path(profile).absolute();root=Path(root or profile).absolute()
    report={'files_deleted':0,'bytes_reclaimed':0,'cache_bytes_examined':0,'skipped':False}
    locks=('SingletonLock','SingletonSocket','SingletonCookie')+(() if owned_lock else ('.dreamina-worker.lock',))
    if not _safe(profile,root) or any((profile/name).exists() or (profile/name).is_symlink() for name in locks):
        return dict(report,skipped=True)
    files=[];truncated=False;directories=0
    for relative in CACHE_ROOTS:
        directory=profile/relative
        if not _safe(directory,root) or not directory.is_dir():continue
        for current,dirs,names in os.walk(directory,followlinks=False):
            directories+=1;dirs[:]=[name for name in dirs if _safe(Path(current)/name,root)]
            if directories>max_files:truncated=True;break
            for name in names:
                path=Path(current)/name
                if not _safe(path,root):continue
                try:info=path.stat(follow_symlinks=False)
                except OSError:continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                    files.append((path,info));report['cache_bytes_examined']+=info.st_size
                if len(files)>=max_files:truncated=True;break
            if truncated:break
        if truncated:break
    if report['cache_bytes_examined']<=threshold_bytes and not truncated:return report
    for path,previous in files:
        if report['bytes_reclaimed']+previous.st_size>max_bytes:continue
        if not _safe(path,root):continue
        try:
            current=path.stat(follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink!=1 or (current.st_ino,current.st_mtime_ns,current.st_size)!=(previous.st_ino,previous.st_mtime_ns,previous.st_size):continue
            path.unlink();report['files_deleted']+=1;report['bytes_reclaimed']+=current.st_size
        except OSError:continue
    return report


class ManagedMaintenance:
    def __init__(self,workers,*,config=None,clock=time.time,outputs_root=None):
        self.workers=workers;self.root=workers.root;self.clock=clock;config=dict(config or {})
        self.retention_days=_positive_int(config.get('MAINTENANCE_RETENTION_DAYS',7),'MAINTENANCE_RETENTION_DAYS',365)
        self.max_files=_positive_int(config.get('MAINTENANCE_MAX_FILES',5000),'MAINTENANCE_MAX_FILES',100000)
        self.max_bytes=_positive_int(config.get('MAINTENANCE_MAX_MIB',256),'MAINTENANCE_MAX_MIB',4096)*1024*1024
        self.interval=_positive_int(config.get('MAINTENANCE_INTERVAL_SECONDS',3600),'MAINTENANCE_INTERVAL_SECONDS',86400)
        self.outputs_root=Path(outputs_root or ROOT/'outputs').absolute()
        self.central_retention={}
        for kind,key in (('media','MAINTENANCE_MEDIA_RETENTION_DAYS'),('platform-inputs','MAINTENANCE_INPUT_RETENTION_DAYS')):
            value=config.get(key,0)
            if str(value).strip()=='0':days=0
            else:
                days=_positive_int(value,key,3650)
                if days<30:raise ValueError(key+' must be 0 (disabled) or 30–3650 days.')
            self.central_retention[kind]=days
        self.latest={'state':'not_run','files_deleted':0,'bytes_reclaimed':0}
        self._cursor=0
        self._central_cursor=''

    def _central_pass(self,report):
        """Evict only exact known terminal files; no directory-wide guesses.

        Both the platform and bridge write guards remain held for the final
        reference check and unlink. Candidate collection happens outside the
        supervisor lifecycle lock. Unknown schemas/rows conservatively skip.
        """
        if not any(self.central_retention.values()):return
        store=self.workers.store;bridge=self.workers.bridge
        if not hasattr(store,'transaction') or not hasattr(bridge,'_connection'):return
        from sqlalchemy import select
        from .platform_store import tasks,TERMINAL_STATES,ACTIVE_STATES
        from .video_inputs import asset_path_for_task
        oldest=self.clock()-min(x for x in self.central_retention.values() if x)*86400
        limit=min(100,max(0,self.max_files-report['files_examined']))
        if not limit:return
        with store.engine.connect() as conn:
            candidates=conn.execute(select(tasks.c.id).where(tasks.c.status.in_(TERMINAL_STATES),
                tasks.c.completed_at<=oldest,tasks.c.id>self._central_cursor).order_by(tasks.c.id).limit(limit)).scalars().all()
        if not candidates:self._central_cursor='';return
        for task_id in candidates:
            self._central_cursor=task_id
            if report['files_examined']>=self.max_files or report['bytes_reclaimed']>=self.max_bytes:
                report['budget_reached']=True;break
            if not re.fullmatch(r'(?:video|img)_[a-f0-9]{32}',task_id):continue
            try:
                # Requests are immutable after admission. Inspect their bodies
                # outside scheduler locks; inside, compare indexed active IDs
                # and only re-read their immutable hash/output metadata by PK.
                with store.engine.connect() as conn:
                    candidate=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
                    active=conn.execute(select(tasks.c.id,tasks.c.request_json,tasks.c.request_hash,tasks.c.output_path).where(tasks.c.status.in_(ACTIVE_STATES)).limit(1001)).mappings().all()
                if not candidate or len(active)>1000:continue
                if any(task_id in r['request_json'] or candidate['output_path'] and candidate['output_path']==r['output_path'] for r in active):continue
                with bridge._connection() as db:
                    active_jobs=db.execute("SELECT id,request_json FROM jobs WHERE status IN ('queued','claimed','submitted','uncertain') LIMIT 1001").fetchall()
                if len(active_jobs)>1000 or any(task_id in r['request_json'] or r['id']==candidate['bridge_job_id'] for r in active_jobs):continue
                active_ids={r['id'] for r in active};job_ids={r['id'] for r in active_jobs}
                expected={r['id']:(r['request_hash'],r['output_path']) for r in active}
                with store.transaction() as conn,bridge._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=conn.execute(select(tasks).where(tasks.c.id==task_id)).mappings().first()
                    if not row or row['status'] not in TERMINAL_STATES or not row['completed_at']:continue
                    if any(row[k]!=candidate[k] for k in ('request_json','output_path','bridge_job_id','completed_at')):continue
                    current_ids=set(conn.execute(select(tasks.c.id).where(tasks.c.status.in_(ACTIVE_STATES)).limit(1001)).scalars())
                    if current_ids!=active_ids:continue
                    if active_ids:
                        current={r.id:(r.request_hash,r.output_path) for r in conn.execute(select(tasks.c.id,tasks.c.request_hash,tasks.c.output_path).where(tasks.c.id.in_(active_ids)))}
                        if current!=expected:continue
                    if {r[0] for r in db.execute("SELECT id FROM jobs WHERE status IN ('queued','claimed','submitted','uncertain') LIMIT 1001")}!=job_ids:continue
                    refs=json.loads(row['request_json']).get('references',[])
                    known=[]
                    if self.central_retention['media']:
                        path=self.outputs_root/'media'/(task_id+'.mp4')
                        if row['output_path'] and Path(row['output_path']).absolute()==path:known.append(('media',path))
                    if self.central_retention['platform-inputs'] and isinstance(refs,list):
                        for ref in refs[:50]:
                            if isinstance(ref,dict):known.append(('platform-inputs',asset_path_for_task(self.outputs_root/'platform-inputs',task_id,ref)))
                    for kind,path in known:
                        if report['files_examined']>=self.max_files or report['bytes_reclaimed']>=self.max_bytes:
                            report['budget_reached']=True;return
                        cutoff=self.clock()-self.central_retention[kind]*86400
                        if row['completed_at']>cutoff or not _safe(path,self.outputs_root/kind):continue
                        report['files_examined']+=1
                        try:info=path.stat(follow_symlinks=False)
                        except FileNotFoundError:continue
                        if not stat.S_ISREG(info.st_mode) or info.st_mtime>cutoff or info.st_nlink!=1:continue
                        if report['bytes_reclaimed']+info.st_size>self.max_bytes:continue
                        path.unlink();report['files_deleted']+=1;report['bytes_reclaimed']+=info.st_size
                        report['central_files_deleted']=report.get('central_files_deleted',0)+1
            except (OSError,ValueError,KeyError):report['errors']+=1

    def _pass(self):
        report={'state':'completed','checked_at':self.clock(),'accounts_checked':0,'accounts_skipped':0,
                'files_examined':0,'directories_examined':0,'files_deleted':0,'bytes_reclaimed':0,'logs_trimmed':0,'errors':0,'budget_reached':False}
        cutoff=self.clock()-self.retention_days*86400
        def exhausted():
            value=report['files_examined']>=self.max_files or report['directories_examined']>=self.max_files or report['bytes_reclaimed']>=self.max_bytes
            report['budget_reached']|=value;return value
        def remove_old_files(directory):
            if not _safe(directory,self.root) or not directory.is_dir():return
            for current,dirs,files in os.walk(directory,followlinks=False):
                if exhausted():return
                report['directories_examined']+=1
                dirs[:]=[name for name in dirs if _safe(Path(current)/name,self.root)]
                for name in files:
                    if exhausted():return
                    candidate=Path(current)/name;report['files_examined']+=1
                    if not _safe(candidate,self.root):continue
                    try:
                        info=candidate.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_mtime>cutoff or report['bytes_reclaimed']+info.st_size>self.max_bytes:continue
                        # Exact allowlisted regular file, no recursive deletion.
                        candidate.unlink();report['files_deleted']+=1;report['bytes_reclaimed']+=info.st_size
                    except OSError:report['errors']+=1
        accounts=self.workers.store.list_accounts()
        if accounts:
            offset=self._cursor%len(accounts);accounts=accounts[offset:]+accounts[:offset]
            self._cursor=(offset+1)%len(accounts)
        for account in accounts:
            if exhausted():break
            aid=account['id'];report['accounts_checked']+=1
            try:
                paths=self.workers.paths(aid);record=self.workers._record(aid)
                if account.get('status')=='login_pending' or self.workers._is_running(aid,record):
                    report['accounts_skipped']+=1;continue
                self.workers._assert_idle(aid,account)
                # Browser login can run without a worker process. A live lock
                # or any uncertain process identity conservatively skips all.
                if any((paths.profile/name).exists() or (paths.profile/name).is_symlink() for name in
                       ('.dreamina-worker.lock','SingletonLock','SingletonSocket','SingletonCookie')):
                    report['accounts_skipped']+=1;continue
                for relative in CACHE_ROOTS:
                    remove_old_files(paths.profile/relative)
                    if exhausted():break
                # Uploaded local copies are disposable only after the exact
                # bridge job is terminal. Central source assets/media remain.
                diagnostics=[paths.state/'diagnostics',paths.state/'assets']
                diagnostics += [paths.state/'slots'/str(i)/name for i in range(1,9) for name in ('diagnostics','assets')]
                for directory in diagnostics:
                    if exhausted():break
                    if not _safe(directory,self.root) or not directory.is_dir():continue
                    for job_dir in directory.iterdir():
                        if exhausted():break
                        report['directories_examined']+=1
                        if not JOB_ID.fullmatch(job_dir.name) or not _safe(job_dir,self.root) or not self.workers.bridge:continue
                        try:job=self.workers.bridge.get(job_dir.name)
                        except Exception:continue
                        if job.get('account_id')==aid and job.get('status') in ('completed','failed'):
                            remove_old_files(job_dir)
                for name in ('worker.stdout.log','worker.stderr.log'):
                    if exhausted():break
                    log=paths.directory/name
                    if not _safe(log,self.root) or not log.is_file():continue
                    report['files_examined']+=1;info=log.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_mtime>cutoff or info.st_size<=10*1024*1024:continue
                    reclaimed=info.st_size-1024*1024
                    if report['bytes_reclaimed']+reclaimed>self.max_bytes:continue
                    # Keep the most recent MiB of an old stopped worker log.
                    with log.open('rb') as inp:inp.seek(-1024*1024,os.SEEK_END);tail=inp.read()
                    temp=log.with_name(log.name+'.maintenance-'+uuid.uuid4().hex)
                    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    try:
                        with os.fdopen(fd,'wb') as out:out.write(tail);out.flush();os.fsync(out.fileno())
                        temp.replace(log)
                    finally:
                        if temp.exists():temp.unlink()
                    report['logs_trimmed']+=1;report['bytes_reclaimed']+=reclaimed
            except PlatformError as error:
                report['accounts_skipped']+=1
                if error.code not in ('account_busy','login_busy'):report['errors']+=1
            except (OSError,ValueError):report['errors']+=1;report['accounts_skipped']+=1
        try:
            disk=shutil.disk_usage(self.root);report.update(disk_free_bytes=disk.free,disk_total_bytes=disk.total)
        except OSError:pass
        return report

    async def run_once(self):
        async with self.workers._lock:
            report=await asyncio.to_thread(self._pass)
        # Central assets are independent of browser lifecycle; don't hold the
        # supervisor lock during database-backed retention inspection.
        await asyncio.to_thread(self._central_pass,report)
        self.latest=report
        return dict(self.latest)

    async def run(self):
        while True:
            try:await self.run_once()
            except asyncio.CancelledError:raise
            except Exception:self.latest={'state':'error','checked_at':self.clock(),'message':'Managed maintenance could not complete.'}
            await asyncio.sleep(self.interval)
