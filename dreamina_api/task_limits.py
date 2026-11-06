"""Bounded foreground waits; an upstream timeout never authorizes a new submit."""
from dataclasses import dataclass

@dataclass(frozen=True)
class TaskLimits:
    queue: int = 60
    preparation: int = 180
    submission: int = 60
    generation: int = 600
    total: int = 900

    @classmethod
    def from_env(cls,env):
        fields={'queue':('TASK_QUEUE_TIMEOUT_SECONDS',60,5,600),
                'preparation':('NATIVE_PREPARATION_TIMEOUT_SECONDS',180,30,600),
                'submission':('NATIVE_SUBMISSION_TIMEOUT_SECONDS',60,15,120),
                'generation':('NATIVE_GENERATION_TIMEOUT_SECONDS',600,60,3600),
                'total':('TASK_TOTAL_TIMEOUT_SECONDS',900,60,7200)}
        values={}
        for name,(key,default,low,high) in fields.items():
            raw=env.get(key,default)
            if isinstance(raw,bool) or not str(raw).isdigit() or not low<=int(raw)<=high:raise ValueError(f'{key} must be {low}–{high} seconds.')
            values[name]=int(raw)
        return cls(**values)

    def expired(self,task,job,now):
        if task['status'] in ('completed','failed','cancelled'):return None
        if now-task['created_at']>=self.total:return ('task_timeout','Task exceeded the foreground time limit; inspect the original task before retrying.')
        if job.get('submit_id'):
            started=job.get('submitted_at') or job.get('activation_started_at') or job.get('claimed_at') or job['created_at']
            if now-started>=self.generation:return ('generation_timeout','Upstream generation did not finish within the time limit. The original task remains under background reconciliation; do not resubmit.')
        elif job.get('activation_started_at') is not None:
            if now-job['activation_started_at']>=self.submission:return ('submission_unconfirmed','Generation activation has no confirmed upstream ID. Review the original task; no automatic resubmission.')
        elif job['status']=='claimed' and job.get('claimed_at') is not None:
            modern=(job.get('claim_protocol_version') or 0)>=6
            started=job.get('preparation_started_at') if modern else job['claimed_at']
            if started is None:
                if now-job['claimed_at']>=self.queue:return ('queue_timeout','No browser preparation lock became available before the queue deadline.')
            elif now-started>=self.preparation:
                return ('preparation_timeout','Browser preparation timed out before generation activation.')
        elif job['status']=='queued' and now-job['created_at']>=self.queue:
            return ('queue_timeout','No execution slot accepted this request before its queue deadline.')
        elif job['status']=='failed' and (job.get('credit_preflight') or {}).get('allowed') is False:
            started=(job.get('credit_preflight') or {}).get('checked_at',job['created_at'])
            if now-started>=self.queue:return ('queue_timeout','No other eligible account became available before the queue deadline.')
        return None
