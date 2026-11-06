"""Read-only operator metrics/alerts. No credentials or external notifications."""
import time
import asyncio
import json
import math
import os
import shutil
from collections import defaultdict
from pathlib import Path
from sqlalchemy import select,func
from .platform_store import tasks,task_events

class ProcessSampler:
    def __init__(self):self.latest={'event_loop_lag_ms':None,'process_memory_bytes':None}
    @staticmethod
    def memory():
        try:return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        except (OSError,ValueError,AttributeError):return None
    async def run(self):
        while True:
            start=time.monotonic();await asyncio.sleep(1)
            self.latest={'event_loop_lag_ms':max(0,round((time.monotonic()-start-1)*1000,3)),
                         'process_memory_bytes':self.memory(),'sampled_at':time.time()}

def _percentile(values,p):
    if not values:return None
    values=sorted(values);index=(len(values)-1)*p
    lo=int(index);hi=min(lo+1,len(values)-1)
    return round(values[lo]+(values[hi]-values[lo])*(index-lo),3)

def account_metrics(store):
    """All-time outcome counts; bounded recent event timings without prompts."""
    with store.engine.connect() as conn:
        counts=conn.execute(select(tasks.c.account_id,tasks.c.status,func.count()).group_by(tasks.c.account_id,tasks.c.status)).all()
        recent=conn.execute(select(tasks.c.id,tasks.c.account_id,tasks.c.created_at,tasks.c.completed_at,tasks.c.error_json).order_by(tasks.c.created_at.desc()).limit(5000)).mappings().all()
        ids=[r['id'] for r in recent];starts={}
        if ids:
            rows=conn.execute(select(task_events.c.task_id,task_events.c.stage,func.min(task_events.c.created_at)).where(
                task_events.c.task_id.in_(ids),task_events.c.stage.in_(('generation_response','generating'))).group_by(task_events.c.task_id,task_events.c.stage)).all()
            for tid,stage,stamp in rows:starts.setdefault(tid,{})[stage]=stamp
    result={};prep=defaultdict(list);gen=defaultdict(list)
    for aid,status,count in counts:
        item=result.setdefault(aid,{'account_id':aid,'accepted':0,'completed':0,'terminal':0,'risk_rejections':0,'audio_rejections':0})
        item['accepted']+=count;item['completed']+=count if status=='completed' else 0;item['terminal']+=count if status in ('completed','failed','cancelled') else 0
    for row in recent:
        item=result[row['account_id']];error=json.loads(row['error_json']) if row['error_json'] else {};text=str(error)
        item['risk_rejections']+=int('ret=-6' in text or (error.get('upstream_error') or {}).get('ret')=='-6')
        item['audio_rejections']+=int(str(error.get('code'))=='2052' or 'OutputAudioRisk' in text)
        timing=starts.get(row['id'],{});submitted=timing.get('generation_response');generating=timing.get('generating')
        if submitted is not None and submitted>=row['created_at']:prep[row['account_id']].append(submitted-row['created_at'])
        if generating is not None and row['completed_at'] is not None and row['completed_at']>=generating:gen[row['account_id']].append(row['completed_at']-generating)
    for aid,item in result.items():
        item.update(success_rate_percent=round(100*item['completed']/item['terminal'],2) if item['terminal'] else None,
            preparation_p50_seconds=_percentile(prep[aid],.5),preparation_p95_seconds=_percentile(prep[aid],.95),
            generation_p50_seconds=_percentile(gen[aid],.5),generation_p95_seconds=_percentile(gen[aid],.95),
            timing_sample_count=len(prep[aid]),recent_task_window=5000,preparation_includes_queue=True)
    return list(result.values())

def monitoring_snapshot(store,bridge,workers,maintenance=None,proxy_health=None,performance=None):
    now=time.time();accounts=store.list_accounts();nodes=[workers.status(a['id']) for a in accounts]
    with store.engine.connect() as conn:
        counts=dict(conn.execute(select(tasks.c.status,func.count()).group_by(tasks.c.status)).all())
        uncertain=conn.execute(select(tasks.c.id,tasks.c.account_id).where(tasks.c.status=='uncertain')).all()
    slots=bridge.accounts();holds=store.active_credit_holds();pacing=store.submission_gate_status();alerts=[]
    for task_id,aid in uncertain:alerts.append({'code':'uncertain_task','severity':'warning','message':'原任务结果待核对，未自动重发或释放积分。','task_id':task_id,'account_id':aid})
    crashed=0
    for node in nodes:
        if node.get('desired_running') and node.get('state') not in ('running','starting'):crashed+=1
        if node.get('restart_circuit_open'):
            alerts.append({'code':'worker_restart_circuit','severity':'critical','message':'节点连续重启失败，已暂停自动重启；检查后手动启动。','account_id':node['account_id']})
        elif node.get('desired_running') and node.get('state') not in ('running','starting'):
            alerts.append({'code':'worker_restarting','severity':'warning','message':'期望运行的节点不在线，按退避计划恢复。','account_id':node['account_id']})
        if node.get('worker_state') in ('login_required','verification_required'):
            alerts.append({'code':'login_required','severity':'warning','message':'账号需要重新登录或人工验证。','account_id':node['account_id']})
    seen=set()
    for gate in pacing:
        jid=gate.get('job_id')
        gate['can_release']=False
        if jid:
            try:
                job=bridge.get(jid)
                gate['can_release']=bool(job.get('submission_window_closed_at') is not None or job.get('confirmed_at') is not None or job['status'] in ('completed','failed') or job.get('activation_started_at') is None)
            except Exception:pass
        if jid and jid not in seen and gate.get('awaiting_receipt') and now-(gate.get('activated_at') or now)>120:
            seen.add(jid);alerts.append({'code':'submission_receipt_missing','severity':'critical','message':'提交节流保护正在等待原提交凭据，请核对该任务。','task_id':gate.get('task_id')})
    metrics={'active_tasks':sum(counts.get(s,0) for s in ('queued','claimed','submitted','in_progress','uncertain')),
        'queued_tasks':counts.get('queued',0),'inflight_tasks':sum(counts.get(s,0) for s in ('submitted','in_progress')),
        'uncertain_tasks':counts.get('uncertain',0),'held_credits':sum(h['amount'] for h in holds),
        'ready_slots':sum(w.get('online') and w.get('ready') for a in slots for w in a['workers']),
        'desired_workers':sum(bool(n.get('desired_running')) for n in nodes),'crashed_workers':crashed}
    if performance:metrics.update(performance.latest)
    if hasattr(workers,'browser_memory_snapshot'):
        try:
            memory=workers.browser_memory_snapshot()
            metrics.update(browser_memory_bytes=memory.get('rss_bytes'),browser_processes=memory.get('browser_processes'),
                           browser_memory_measure='rss_sum_upper_bound',browser_memory_by_account=memory.get('accounts',[]))
        except (OSError,ValueError,RuntimeError):pass
    try:
        disk=shutil.disk_usage(getattr(workers,'root',Path.cwd()));used=100*(disk.total-disk.free)/disk.total;metrics['disk_used_percent']=round(used,2)
        if used>=80:alerts.append({'code':'disk_usage_high','severity':'warning','message':'磁盘占用超过80%，请检查保留策略和备份。'})
    except OSError:pass
    proxy_rows=store.list_proxies() if hasattr(store,'list_proxies') else []
    if proxy_health:alerts.extend(proxy_health.latest.get('alerts',[]))
    for account in accounts:
        if account.get('proxy_scheduling_error'):alerts.append({'code':account['proxy_scheduling_error'],'severity':'warning','account_id':account['id'],'message':'账号代理不可用或出口发生变化；新任务已暂停，不会回退直连。'})
    return {'observed_at':now,'metrics':metrics,'alerts':alerts,'pacing':pacing,'maintenance':dict(maintenance.latest) if maintenance else {'state':'disabled'},
            'proxies':proxy_rows,'account_metrics':account_metrics(store)}
