"""Connect the management control plane to verified real-Chrome workers."""
from __future__ import annotations
import asyncio
import json
import logging
import math
import secrets
import uuid
from pathlib import Path
import time
import weakref
from urllib.parse import urlsplit, urljoin

import httpx

from .client import DreaminaClient, HISTORY_PATH, normalize_record, DreaminaError
from .chrome_verify import verify_record
from .config import ROOT
from .image_models import normalize_image_record
from .models import get_models, model_config
from .video_inputs import size_options
from .credit_quote import quote_request_fingerprint
from .chrome_bridge import BridgeError
from .task_limits import TaskLimits
from .upstream_errors import retry_proof, sanitize_upstream_error
from .platform_store import PlatformError
from .db_async import db_call

log = logging.getLogger(__name__)
NATIVE_VERIFICATION_PATH = Path(__file__).parent / 'data' / 'native-verification.json'
_RECORDED_STATES = {'live-verified', 'live-verified-reconciled', 'account-plan-required', 'native-ui-unresolved'}


def exact_credit_balance(credit):
    valid=lambda value:isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0
    value=credit.get('credit') if isinstance(credit,dict) else None
    if valid(value):return float(value)
    if not isinstance(value,dict):return None
    for key in ('total_credit','total','total_amount'):
        if key in value:return float(value[key]) if valid(value[key]) else None
    parts=[value[key] for key in ('gift_credit','purchase_credit','vip_credit') if key in value]
    return float(sum(parts)) if parts and all(valid(part) for part in parts) else None


def _native_verifications():
    """Optional packaged evidence must never prevent the model API from loading."""
    try:
        with NATIVE_VERIFICATION_PATH.open('rb') as source:
            raw=source.read(512*1024+1)
        if len(raw)>512*1024:return {}
        value=json.loads(raw.decode('utf-8-sig'))
        return value['models'] if isinstance(value,dict) and isinstance(value.get('models'),dict) else {}
    except (OSError,UnicodeError,ValueError,TypeError,RecursionError):return {}


def _verified_record(value,item):
    if not isinstance(value,dict) or not isinstance(value.get('status'),str) or value['status'] not in _RECORDED_STATES:
        return None
    raw_cases=value.get('cases')
    if not isinstance(raw_cases,list) or not 1<=len(raw_cases)<=100:return None
    cases=[]
    model=model_config(item['id']) if item['kind']=='video' else None
    for source in raw_cases:
        if not isinstance(source,dict):continue
        resolution=source.get('resolution');duration=source.get('duration')
        images=source.get('reference_images');videos=source.get('reference_videos')
        if any(type(count) is not int or not 0<=count<=50 for count in (images,videos)):continue
        if not isinstance(resolution,str):continue
        if model:
            allowed={r.lower() for r in model['resolutions']} & {'480p','720p','1080p'}
            if resolution.lower() not in allowed or type(duration) is not int or not model['duration']['min']<=duration<=model['duration']['max']:continue
        elif resolution not in ('2048x2048','2K') or duration is not None or images or videos:
            continue
        case={'resolution':resolution,'duration':duration,'reference_images':images,'reference_videos':videos}
        if 'ratio' in source:
            if source['ratio'] not in (model['ratios'] if model else ['1:1']):continue
            case['ratio']=source['ratio']
        cases.append(case)
    if not cases:return None
    def text(name,limit):
        current=value.get(name)
        return current[:limit] if isinstance(current,str) else ''
    return {'status':value['status'],'verified_on':text('verified_on',32) or None,
            'summary':text('summary',1000),'cases':cases,'note':text('note',2000)}


def _apply_native_verification(items):
    evidence=_native_verifications()
    for item in items:
        record=_verified_record(evidence.get(item['id']),item)
        item['verification']=record or {'status':item['verification_status'],'verified_on':None,'summary':'尚无有效的无扩展实测记录。','cases':[],
                                        'note':'模型能力配置不等于所有参数组合均已实测。'}
        if not record:continue
        item['verification_status']=record['status']
        if record['status']=='native-ui-unresolved':
            item['description']='自动化用例尚未完成验证，不能据此判断账号无模型权限。'+record['summary']+' '+record['note']
            continue
        if record['status']=='account-plan-required':
            item['description']='当前测试账号的用例进入订阅升级界面，未完成生成。'+record['summary']+' 此记录仅针对当前测试账号及 verification.cases 所列用例，不代表所有账号均不可用。'
            continue
        label='实测结果已通过上游记录核对确认' if record['status']=='live-verified-reconciled' else '已有成功的无扩展实测用例'
        item['description']=label+'。'+record['summary']+' 仅证明 verification.cases 中列出的组合，其他尺寸、时长和参考素材组合未因此获得实测确认。'
        if item['kind']=='image':item['description']+=' 图片编辑尚未接入。'
    return items


def model_catalog():
    video=[{'id':m['id'],'name':m['name'],'kind':'video','enabled':True,'available':True,
            'verification_status':'native-driver-awaiting-live-verification',
            'description':'无扩展 Worker 已实现；独立新会话、原生参数与多参考素材，待专用浏览器登录后实测',
            'capabilities':{'sizes':list(size_options(m)),'seconds':[str(x) for x in range(m['duration']['min'],m['duration']['max']+1)],
                            'references':True,'input_reference':True,'new_session':True,'max_prompt_length':m['max_prompt_length'],
                            'prompt_mentions':{'image':'@图片N','video':'@视频N','numbering':'one-based-per-type','repeated':True}}}
           for m in get_models()]
    return _apply_native_verification(video+[
        {'id':'seedream-5.0-lite','name':'Seedream 5.0 Lite','kind':'image','enabled':True,
         'available':True,'verification_status':'native-driver-awaiting-live-verification','description':'2K 正方形文生图；旧桥接已验证，无扩展 Worker 待登录后实测；图片编辑尚未接入',
         'capabilities':{'sizes':['2048x2048'],'default_size':'2048x2048','n':[1],
                         'edits':False,'quality':['auto'],'output_format':['jpeg'],'max_prompt_length':2000}},
    ])


class PlatformRuntime:
    def __init__(self, store, bridge, *, clock=time.monotonic, poll_interval=3, max_backoff=60, query_timeout=15, limits=None, submission_attempts=2, retry_delay=None):
        if type(submission_attempts) is not int or submission_attempts not in (1,2):
            raise ValueError('NATIVE_SUBMISSION_ATTEMPTS must be 1 or 2.')
        self.store, self.bridge = store, bridge
        self._downloads=weakref.WeakValueDictionary()
        self._credit_preflight_locks=weakref.WeakValueDictionary()
        self._locks = {}
        # Only the rate limit lives in memory. Task/submit IDs and recovery events
        # remain in the database, so a restarted process queries the same task.
        self._polls = {}
        self._clock = clock
        self.poll_interval = max(1, float(poll_interval))
        self.max_backoff = max(self.poll_interval, float(max_backoff))
        self.query_timeout = max(1, float(query_timeout))
        self._query_slots = asyncio.Semaphore(4)
        self.limits=limits or TaskLimits()
        self._balance_refresh_due={}
        self.proxy_health=None;self._proxy_probe_due={};self._proxy_probe_tasks=set()
        self.submission_attempts=submission_attempts
        self._retry_delay=retry_delay or (lambda:5+secrets.randbelow(11))

    @staticmethod
    def _enforce_retry_budget(task,quote):
        retry=(task.get('request_detail') or {}).get('submission_retry') or {}
        if retry.get('attempt',1)<=1:return
        cap=retry.get('quote_ceiling');amount=quote.get('required_credits')
        valid=lambda n:type(n) in (int,float) and math.isfinite(n) and n>=0
        if not valid(cap) or not valid(amount) or amount>cap:
            raise BridgeError('Retry quote exceeds the original approved credit budget; no generation submitted.')

    async def _query_event(self, task_id, stage, detail, *, job_id=None):
        if not hasattr(self.store, 'append_event'):
            return
        try:
            if job_id is None:(await db_call(self.store.append_event,task_id, stage, detail))
            else:(await db_call(self.store.append_event,task_id, stage, detail,expected_bridge_job_id=job_id))
        except Exception:
            # A diagnostic event cannot discard a confirmed upstream outcome.
            log.warning('Task %s query diagnostic could not be persisted', task_id)

    def _poll_state(self, task_id, submit_id, job_id=None):
        current = self._polls.get(task_id)
        if not current or current['submit_id'] != submit_id:
            current = {'submit_id': submit_id,'bridge_job_id':job_id, 'next_at': 0, 'attempt': 0,
                       'reason': None, 'delay': None}
            self._polls[task_id] = current
        return current

    async def _defer_query(self, task_id, state, reason, *, base=None):
        attempts = state['attempt'] + 1 if state['reason'] == reason else 1
        base = self.poll_interval if base is None else max(self.poll_interval, base)
        delay = min(self.max_backoff, base * 2 ** min(attempts - 1, 8))
        changed = state['reason'] != reason or state['delay'] != delay
        state.update(attempt=attempts, reason=reason, delay=delay, next_at=self._clock() + delay)
        if changed:
            await self._query_event(task_id, 'upstream_query_retry', {
                'reason': reason, 'attempt': attempts, 'retry_after_seconds': delay,
                'retry_at': time.time() + delay, 'submit_id': state['submit_id'],
                'action': 'query_original_task_only',
            },job_id=state.get('bridge_job_id'))

    async def _query_recovered(self, task_id, state):
        if state['reason'] is not None:
            await self._query_event(task_id, 'upstream_query_recovered', {
                'previous_reason': state['reason'], 'attempts': state['attempt'],
                'submit_id': state['submit_id'], 'action': 'original_task_confirmed',
            },job_id=state.get('bridge_job_id'))
        state.update(attempt=0, reason=None, delay=None, next_at=self._clock() + self.poll_interval)

    @staticmethod
    def _query_problem(error):
        """Return a fixed classification, never upstream text or signed URLs."""
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if status == 429:return 'rate_limited', 15
            if status in (401, 403):return 'auth_required', 60
            if status >= 500:return 'upstream_unavailable', 3
            return 'query_rejected', 60
        if isinstance(error, (TimeoutError, httpx.TransportError)):
            return 'network_unavailable', 3
        code = getattr(error, 'code', '')
        status = getattr(error, 'status', None)
        if code in ('missing_session', 'invalid_session', 'unauthorized', 'upstream_401', 'upstream_403') or status in (401, 403):
            return 'auth_required', 60
        if code in ('rate_limited', 'upstream_429') or status == 429:
            return 'rate_limited', 15
        if str(code).startswith('proxy_'):return 'proxy_unavailable', 15
        if code == 'network_error':return 'network_unavailable', 3
        if code in ('invalid_response', 'non_json_response') or str(code).startswith(('upstream_50', 'upstream_51', 'upstream_52')):
            return 'upstream_unavailable', 3
        # Business/permission errors have no authority to fail the generation.
        return 'query_rejected', 60

    async def _query_unavailable(self, task, state, reason, *, base=None):
        current = (await db_call(self.store.get_task,task['id']))
        if current['bridge_job_id']!=task['bridge_job_id']:return current
        if current['status'] in ('completed', 'failed', 'cancelled'):
            self._polls.pop(task['id'], None)
            self._locks.pop(task['id'], None)
            return current
        await self._defer_query(task['id'], state, reason, base=base)
        status = 'submitted' if current['status'] in ('queued', 'claimed') else current['status']
        error = current.get('error') if current['status'] == 'uncertain' else None
        if not error:
            error = {
                'code': 'upstream_query_auth_required' if reason == 'auth_required' else 'upstream_query_retrying',
                'message': ('The original task is preserved; restore account access to continue checking it.'
                            if reason == 'auth_required' else
                            'The original task is preserved while its status query is retried. Do not resubmit generation.'),
            }
        return (await db_call(self.store.update_task,task['id'], expected_bridge_job_id=task['bridge_job_id'],status=status, error=error))

    async def check_account(self, account_id, settings):
        async with DreaminaClient(settings) as client:
            credit = await client.credits()
        balance=exact_credit_balance(credit)
        return {'status':'ready','balance':balance}

    async def credit_preflight(self,job,worker_id,claim_token,quote):
        lock=self._credit_preflight_locks.setdefault(job['id'],asyncio.Lock())
        async with lock:
            job=(await db_call(self.bridge.verify_claim,job['id'],worker_id,claim_token))
            return await self._credit_preflight_locked(job,worker_id,claim_token,quote)

    async def _credit_preflight_locked(self,job,worker_id,claim_token,quote):
        task=(await db_call(self.store.task_for_bridge,job['id']))
        if not task or task.get('submit_id') or job.get('activation_started_at') is not None or task['status'] in ('completed','failed','cancelled','uncertain') or task['account_id']!=job['account_id']:
            raise BridgeError('Credit check does not belong to an unsubmitted platform task.')
        if quote.get('request_fingerprint')!=quote_request_fingerprint(job['request']) or quote.get('model')!=job['request']['model']:
            raise ValueError('Price quote does not match this task.')
        self._enforce_retry_budget(task,quote)
        cached=job.get('credit_preflight') or {}
        if cached.get('allowed') is True and cached.get('observed_at')==quote.get('observed_at') and cached.get('required_credits')==quote.get('required_credits'):
            return (await db_call(self.bridge.credit_preflight,job['id'],worker_id,claim_token,quote,cached['balance']))['credit_preflight']
        try:checked=await asyncio.wait_for(self.check_account(task['account_id'],(await db_call(self.store.settings_for_account,task['account_id']))),25)
        except Exception as error:
            if str(getattr(error,'code','')).startswith('proxy_'):
                await self.proxy_failure(task['account_id'])
                await db_call(self.store.append_event,task['id'],'proxy_preflight_failed',{'account_id':task['account_id'],'generation_not_activated':True})
                rejection=BridgeError('The configured proxy is unavailable; no generation was submitted.',status=409);rejection.code='proxy_error';raise rejection from None
            (await db_call(self.store.append_event,task['id'],'credit_check_unavailable',{'account_id':task['account_id'],'generation_not_activated':True,'reason':'balance_query_failed'}))
            raise BridgeError('Current credit balance query is unavailable; no generation submitted.',status=503) from None
        balance=checked.get('balance')
        if not isinstance(balance,(int,float)) or isinstance(balance,bool) or not math.isfinite(balance) or balance<0:
            (await db_call(self.store.append_event,task['id'],'credit_check_unavailable',{'account_id':task['account_id'],'generation_not_activated':True,'reason':'unknown_balance'}))
            raise BridgeError('Current spendable credit balance is unknown; no generation submitted.',status=503)
        current=(await db_call(self.store.get_task,task['id']))
        if current['bridge_job_id']!=job['id'] or current['account_id']!=job['account_id'] or current.get('submit_id') or current['status'] in ('completed','failed','cancelled','uncertain'):
            raise BridgeError('Task state changed during credit verification; no generation may be activated.')
        decision=(await db_call(self.store.reserve_task_credits,task['id'],job['id'],required_credits=quote['required_credits'],balance=balance,request_fingerprint=quote['request_fingerprint']))
        try:updated=(await db_call(self.bridge.credit_preflight,job['id'],worker_id,claim_token,quote,balance,decision=decision))
        except Exception:
            latest=(await db_call(self.bridge.get,job['id']))
            if latest.get('activation_started_at') is None and not latest.get('submit_id'):
                (await db_call(self.store.release_task_credits,task['id'],job['id'],reason='preflight_not_approved'))
            raise
        proof=updated['credit_preflight']
        (await db_call(self.store.append_event,task['id'],'credit_preflight' if proof['allowed'] else 'credit_insufficient',proof))
        return proof

    async def activate_task(self,job,worker_id,claim_token):
        lock=self._credit_preflight_locks.setdefault(job['id'],asyncio.Lock())
        async with lock:
            job=(await db_call(self.bridge.verify_claim,job['id'],worker_id,claim_token))
            task=(await db_call(self.store.task_for_bridge,job['id']))
            try:
                if not task or task.get('submit_id') or task['status'] not in ('queued','claimed') or task['account_id']!=job['account_id']:
                    raise BridgeError('Task is not eligible for a new generation activation.')
                (await db_call(self.store.validate_task_activation,task['id'],job['id']))
                expiry=self.limits.expired(task,job,self.store.clock())
                if expiry:raise PlatformError(expiry[1],code=expiry[0],status=409)
                proof=job.get('credit_preflight') or {}
                self._enforce_retry_budget(task,proof)
                holds=(await db_call(self.store.active_credit_holds,task['account_id']))
                if not any(h['job_id']==job['id'] and h['task_id']==task['id'] and h['amount']==proof.get('required_credits') and h['request_fingerprint']==proof.get('request_fingerprint') for h in holds):
                    raise BridgeError('An approved credit hold is required before activation.')
                if (job.get('claim_protocol_version') or 0)>=6:
                    turn=(await db_call(self.store.commit_submission_turn,task['id'],job['id']))
                    if turn.get('ready') is not True:raise PlatformError('Submission turn expired or unavailable; no generation submitted.',code='submission_turn_unavailable',status=409)
                return (await db_call(self.bridge.activate,job['id'],worker_id,claim_token))
            except Exception as exc:
                if isinstance(exc,PlatformError) and exc.code=='worker_status_unavailable':
                    raise BridgeError('Worker status is temporarily unavailable; retry this activation request.',status=503) from None
                # Only an atomic failed-job fence can prove a negative reply.
                # If a competing activation already won, this raises instead
                # and the worker must preserve its uncertain activation intent.
                denial=(await db_call(self.bridge.deny_activation,job['id'],worker_id,claim_token,
                    error_code=getattr(exc,'code','activation_denied') if isinstance(exc,(PlatformError,BridgeError)) else 'activation_check_failed',
                    message=str(exc) if isinstance(exc,(PlatformError,BridgeError)) else 'The central activation check failed before a confirmed activation.'))
                (await db_call(self.store.finish_submission_turn,job['id']))
                return denial

    async def prepare_task(self,job,worker_id,claim_token):
        job=(await db_call(self.bridge.verify_claim,job['id'],worker_id,claim_token))
        task=(await db_call(self.store.task_for_bridge,job['id']))
        if not task:raise BridgeError('This claim has no platform task.')
        expiry=self.limits.expired(task,job,self.store.clock())
        if expiry:
            (await db_call(self.bridge.deadline,job['id'],*expiry));raise BridgeError(expiry[1])
        return (await db_call(self.bridge.start_preparation,job['id'],worker_id,claim_token))

    async def submission_turn(self,job,worker_id,claim_token):
        job=(await db_call(self.bridge.verify_claim,job['id'],worker_id,claim_token))
        if job['status']!='claimed' or (job.get('claim_protocol_version') or 0)<6 or job.get('preparation_started_at') is None or job.get('activation_started_at') is not None or job.get('submit_id'):raise BridgeError('Only an unactivated prepared v6 task may request a submission turn.')
        task=(await db_call(self.store.task_for_bridge,job['id']))
        if not task:raise BridgeError('This claim has no platform task.')
        expiry=self.limits.expired(task,job,self.store.clock())
        if expiry:
            (await db_call(self.bridge.deadline,job['id'],*expiry));raise BridgeError(expiry[1])
        try:
            egress=await db_call(self.store.resolve_account_egress,task['account_id'])
            return (await db_call(self.store.reserve_submission_turn,task['id'],job['id'],egress_group=egress.group))
        except PlatformError as error:raise BridgeError(str(error),status=error.status) from None

    async def reported_submission(self,job):
        """Release pacing after a verified receipt or fenced closed task page.

        Closing an unknown submission window releases only the cross-account
        pacing turn; its original logical task and possible charge stay held.
        """
        if job['status'] in ('submitted','completed','failed') or job.get('confirmed_at') is not None or job.get('submission_window_closed_at') is not None:
            (await db_call(self.store.finish_submission_turn,job['id']))
        if str(job.get('error_code','')).startswith('proxy_'):
            await self.proxy_failure(job['account_id'])

    async def proxy_failure(self,account_id):
        if self.proxy_health is None or self.store.clock()<self._proxy_probe_due.get(account_id,0):return
        account=await db_call(self.store.get_account,account_id);pid=account.get('proxy_id')
        if not pid:return
        self._proxy_probe_due[account_id]=self.store.clock()+60
        async def probe():
            try:await self.proxy_health.test(pid)
            except Exception:pass
        task=asyncio.create_task(probe());self._proxy_probe_tasks.add(task);task.add_done_callback(self._proxy_probe_tasks.discard)

    async def release_gate(self,scope,*,note):
        gates=await db_call(self.store.submission_gate_status)
        gate=next((g for g in gates if g['scope']==scope),None)
        if not gate:raise PlatformError('Submission gate not found.',code='not_found',status=404)
        jid=gate['job_id']
        if jid:
            job=await db_call(self.bridge.get,jid)
            safe=(job.get('submission_window_closed_at') is not None or job.get('confirmed_at') is not None or job['status'] in ('completed','failed')
                  or job.get('activation_started_at') is None and await db_call(self.bridge.fail_unactivated,jid))
            if not safe:raise PlatformError('The original browser submission is still unconfirmed. Verify that its page is closed before releasing the pacing gate.',code='gate_release_unsafe',status=409)
        return await db_call(self.store.release_submission_gate,scope,expected_job_id=jid,note=note)

    async def manual_reconcile(self,task_id,*,submit_id=None,note=''):
        task=(await db_call(self.store.get_task,task_id))
        if task['status']!='uncertain':raise PlatformError('Only uncertain tasks require this review.',code='not_uncertain',status=409)
        if not task.get('bridge_job_id'):raise PlatformError('Dispatch identity is missing; database/queue evidence must be repaired before releasing credit.',code='missing_dispatch_identity',status=409)
        job=(await db_call(self.bridge.get,task['bridge_job_id']))
        original=job.get('submit_id') or task.get('submit_id')
        if submit_id and original and submit_id!=original:raise PlatformError('An existing upstream ID cannot be replaced.',code='identity_conflict',status=409)
        selected=original or submit_id
        if not selected:raise PlatformError('Enter the original upstream submit_id after checking the account history; absence of billing alone cannot release this task.',code='original_id_required',status=409)
        settings=(await db_call(self.store.settings_for_account,task['account_id'],existing_task=True))
        if job.get('api_origin'):settings.api_origin=job['api_origin']
        async with DreaminaClient(settings) as client:
            data=await asyncio.wait_for(client.request(HISTORY_PATH,{'submit_ids':[selected]}),self.query_timeout)
        record=data.get(selected) if isinstance(data,dict) else None
        if not record:raise PlatformError('Upstream has not returned this task. Its outcome is still unknown; credit remains reserved.',code='original_not_confirmed',status=409)
        candidate=dict(job,submit_id=selected)
        try:verify_record(candidate,record)
        except (ValueError,TypeError,KeyError,IndexError,AttributeError):raise PlatformError('Upstream account/task parameters do not match the original request.',code='identity_unconfirmed',status=409) from None
        normalized=(normalize_image_record if task['kind']=='image' else normalize_record)(record,selected)
        if normalized.get('status') not in ('processing','completed','failed','deleted'):raise PlatformError('The original task has an unknown state; keep its credit reservation.',code='unrecognized_task_state',status=409)
        if not original:
            (await db_call(self.bridge.bind_verified_original,job['id'],selected,settings.api_origin or client.api_base))
            (await db_call(self.store.append_event,task_id,'manual_reconciliation',{'submit_id':selected,'note':note,'source':'verified_upstream_history'},expected_bridge_job_id=job['id']))
        (await db_call(self.store.append_event,task_id,'manual_query',{'submit_id':selected,'note':note,'upstream_status':normalized['status']},expected_bridge_job_id=job['id']))
        self._polls.pop(task_id,None)
        return await self.sync_task((await db_call(self.store.get_task,task_id)))

    async def refresh_credit_thresholds(self):
        now=self.store.clock()
        due=[a for a in (await db_call(self.store.list_accounts)) if a['enabled'] and a.get('min_credit_balance',0)>0 and a.get('secret_configured')
             and a['status'] not in ('login_pending','deleting','deleted') and now-(a.get('last_checked_at') or 0)>=60
             and now>=self._balance_refresh_due.get(a['id'],0)]
        async def refresh(account):
            self._balance_refresh_due[account['id']]=now+120
            try:
                checked=await asyncio.wait_for(self.check_account(account['id'],(await db_call(self.store.settings_for_account,account['id']))),15)
                if checked.get('balance') is not None:(await db_call(self.store.record_account_check,account['id'],status='ready',balance=checked['balance']))
            except Exception:pass
        await asyncio.gather(*(refresh(a) for a in sorted(due,key=lambda a:a.get('last_checked_at') or 0)[:3]))

    async def run_credit_refresh(self):
        while True:
            try:await self.refresh_credit_thresholds()
            except asyncio.CancelledError:raise
            except Exception:log.warning('Account credit refresh temporarily unavailable')
            await asyncio.sleep(15)

    @staticmethod
    def _dispatch_request(task):
        spec=task['request']
        if task['kind']=='image':
            request={'kind':'image','model':spec['model'],'prompt':spec['prompt'],'n':spec['n'],'size':spec['size'],'ratio':'1:1','resolution':'2K'}
        else:
            request={k:spec[k] for k in ('model','prompt','duration','ratio','resolution','mode','references')}
            request.update(asset_owner_task_id=task['id'],new_session=True)
        return request

    def _ensure_submission_dispatch(self,task):
        if not (task.get('request_detail') or {}).get('submission_retry_pending'):return task
        task=self.store.validate_submission_retry(task['id'],total_timeout=self.limits.total)
        if task['status'] in ('completed','failed','cancelled','uncertain'):return task
        detail=task.get('request_detail') or {}
        if not detail.get('submission_retry_pending'):return task
        retry=detail['submission_retry']
        if self.store.clock()<retry['next_retry_at']:return task
        # A deterministic job ID makes a crash before/after enqueue recoverable.
        # Activation rechecks authorization and the original quote ceiling.
        self.bridge.enqueue(self._dispatch_request(task),account_id=task['account_id'],job_id=task['bridge_job_id'])
        self.store.finish_submission_retry_dispatch(task['id'],task['bridge_job_id'])
        return self.store.get_task(task['id'])

    def _ensure_credit_dispatch(self,task):
        if not (task.get('request_detail') or {}).get('credit_failover_pending'):return task
        request=self._dispatch_request(task)
        self.bridge.enqueue(request,account_id=task['account_id'],job_id=task['bridge_job_id'])
        self.store.finish_credit_failover_dispatch(task['id'],task['bridge_job_id'])
        return self.store.get_task(task['id'])

    async def sync_task(self, task):
        result=await self._sync_task(task)
        if result['status'] in ('completed','failed','cancelled'):
            self._locks.pop(result['id'],None);self._polls.pop(result['id'],None)
        return result

    async def _sync_task(self, task):
        task = (await db_call(self.store.get_task,task['id']))
        if not task.get('bridge_job_id') and task['status']=='queued' and task['kind'] in ('video','image'):
            if self.store.clock()-task['created_at']>=min(self.limits.queue,self.limits.total):
                dispatch_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-'+task['kind']+'-dispatch:'+task['id']))
                if await db_call(self.bridge.fail_unactivated,dispatch_id):
                    return await db_call(self.store.fail_missing_initial_dispatch,task['id'],dispatch_id)
        if task['status'] in ('completed','failed','cancelled') or not task.get('bridge_job_id'):
            self._polls.pop(task['id'], None)
            self._locks.pop(task['id'], None)
            return task
        lock = self._locks.setdefault(task['id'], asyncio.Lock())
        # A simultaneous admin/public GET should return the persisted snapshot,
        # rather than queue another HTTP call behind an already-running query.
        if lock.locked():
            return (await db_call(self.store.get_task,task['id']))
        async with lock:
            task=(await db_call(self.store.get_task,task['id']))
            if task['status'] in ('completed','failed','cancelled'): return task
            task=(await db_call(self._ensure_submission_dispatch,task))
            if task['status'] in ('completed','failed','cancelled'):return task
            if (task.get('request_detail') or {}).get('submission_retry_pending'):return task
            task=(await db_call(self._ensure_credit_dispatch,task))
            try:job=(await db_call(self.bridge.get,task['bridge_job_id']))
            except BridgeError as exc:
                if exc.status!=404:raise
                expected=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-'+task['kind']+'-dispatch:'+task['id']))
                safe_dispatch=(task['kind'] in ('video','image') and task['bridge_job_id']==expected
                    and task['status'] in ('queued','claimed') and not task.get('submit_id'))
                # The deterministic initial dispatch was persisted before the
                # queue write. Only its missing identity can be fenced, and
                # only after the original admission deadline has elapsed.
                if not safe_dispatch:return task
                if self.store.clock()-task['created_at']<min(self.limits.queue,self.limits.total):return task
                if await db_call(self.bridge.fail_unactivated,task['bridge_job_id']):
                    return await db_call(self.store.update_task,task['id'],expected_bridge_job_id=task['bridge_job_id'],
                        status='failed',error={'code':'dispatch_failed','message':'The initial dispatch did not reach the browser queue before its deadline; no generation was activated.'})
                return await db_call(self.store.get_task,task['id'])
            if job['status']=='uncertain' and (job.get('claim_protocol_version') or 0)>=5 and job.get('activation_started_at') is None and not job.get('submit_id') and job.get('confirmed_at') is None:
                job=await db_call(self.bridge.resolve_unactivated_uncertain,job['id'])
            if job['status'] in ('submitted','completed','failed') or job.get('confirmed_at') is not None or job.get('submission_window_closed_at') is not None:
                (await db_call(self.store.finish_submission_turn,job['id']))
            if task['status'] in ('queued','claimed') and job.get('activation_started_at') is None and not job.get('submit_id'):
                reason=await db_call(self.store.current_availability_reason,task['account_id'])
                if not reason and job['status']=='failed' and str(job.get('error_code','')).startswith('proxy_'):reason=job['error_code']
                if reason and (reason=='upstream_risk_cooldown' or reason.startswith('proxy_')):
                    if reason.startswith('proxy_'):await self.proxy_failure(task['account_id'])
                    if await db_call(self.bridge.fail_unactivated,job['id'],'The account became unavailable before generation.'):
                        fenced=await db_call(self.bridge.get,job['id'])
                        await db_call(self.store.finish_submission_turn,job['id'])
                        reassigned=await db_call(self.store.reassign_unavailable_task,task['id'],fenced,reason)
                        return await db_call(self._ensure_credit_dispatch,reassigned,_store=self.store)
            expiry=self.limits.expired(task,job,self.store.clock())
            if expiry:
                previous=job['status'];job=(await db_call(self.bridge.deadline,job['id'],*expiry))
                code,message=expiry
                if job['status']=='failed':
                    return (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],status='failed',error={'code':code,'message':message}))
                if job['status']=='uncertain' and (task.get('error') or {}).get('code')!=code:
                    task=(await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],status='uncertain',error={'code':code,'message':message}))
                    (await db_call(self.store.append_event,task['id'],code,{'submit_id':job.get('submit_id'),'original_task_pending':bool(job.get('submit_id')),'no_resubmission':True}))
            if job['status']=='failed' and job.get('submit_id') and job.get('error_code')=='upstream_rejected':
                diagnostic=sanitize_upstream_error(job.get('upstream_error'))
                if (diagnostic or {}).get('ret')=='-6' or 'ret=-6.' in (job.get('error') or ''):
                    cooldown=(await db_call(self.store.record_submission_risk,task['account_id'],task['model'],job['id']))
                    if cooldown.get('recorded'):
                        (await db_call(self.store.append_event,task['id'],'risk_cooldown',cooldown,expected_bridge_job_id=job['id']))
                if retry_proof(diagnostic) and task['status']!='uncertain' and type(job.get('claim_protocol_version')) is int and job['claim_protocol_version']>=5:
                    delay=max(self._retry_delay(),(diagnostic or {}).get('retry_after_seconds',0))
                    try:
                        return (await db_call(self.store.schedule_submission_retry,task['id'],job,delay_seconds=delay,
                            max_attempts=self.submission_attempts,total_timeout=self.limits.total))
                    except PlatformError as exc:
                        if exc.code!='unsafe_submission_retry':raise
                        (await db_call(self.store.append_event,task['id'],'submission_retry_not_permitted',
                            {'reason':'incomplete_retry_evidence','bridge_job_id':job['id']},expected_bridge_job_id=job['id']))
                error={'code':'upstream_rejected','message':job.get('error') or 'Upstream rejected this request.'}
                if diagnostic:error['upstream_error']=diagnostic
                return (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],status='failed',submit_id=job['submit_id'],error=error))
            proof=job.get('credit_preflight') or {}
            if not task.get('submit_id') and not job.get('submit_id') and job['status']=='failed' and proof.get('allowed') is False and proof.get('generation_not_activated') is True:
                if task['status']=='uncertain':return task
                reassigned=(await db_call(self.store.reassign_insufficient_credits,task['id'],job))
                self._polls.pop(task['id'],None)
                return (await db_call(self._ensure_credit_dispatch,reassigned))
            telemetry=job.get('telemetry') or {}
            old=task.get('progress_details') or {}
            if telemetry.get('observed_at',0)>old.get('observed_at',0):
                patch={'progress_details':{'source':'page','observed_at':telemetry['observed_at'],'stage':telemetry.get('stage'),'value':telemetry.get('progress')}}
                if telemetry.get('conversation_id'):patch['conversation_id']=telemetry['conversation_id']
                if telemetry.get('progress') is not None:
                    patch['progress']=telemetry['progress']
                (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],**patch))
                if telemetry.get('stage')!=old.get('stage') or telemetry.get('progress')!=old.get('value'):
                    (await db_call(self.store.append_event,task['id'],telemetry.get('stage','browser'),telemetry,expected_bridge_job_id=job['id']))
            if job['account_id'] != task['account_id']:
                return (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],status='uncertain',error={'code':'account_mismatch','message':'Worker account mismatch; manual reconciliation required.'}))
            if not job.get('submit_id'):
                mapped={'queued':'queued','claimed':'claimed','uncertain':'uncertain','failed':'failed'}.get(job['status'],task['status'])
                if task['status']!='queued' and mapped=='queued': mapped=task['status']
                if task['status']=='uncertain' and mapped not in ('failed','cancelled'):mapped='uncertain'
                error={'code':job.get('error_code') or 'browser_'+job['status'],'message':job.get('error')} if job['status'] in ('uncertain','failed') else None
                if mapped=='uncertain' and not error:
                    error=task.get('error') or {'code':'submission_unconfirmed','message':'No upstream task ID has been confirmed; do not resubmit automatically.'}
                plan_detail=telemetry.get('detail') or {}
                if mapped=='failed' and telemetry.get('stage')=='account_plan_required' and isinstance(plan_detail,dict) and plan_detail.get('model_permission_confirmed') is True and hasattr(self.store,'block_account_model'):
                    (await db_call(self.store.block_account_model,task['account_id'],task['model'],reason='account_plan_required',cooldown_seconds=900))
                return (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],status=mapped,error=error))
            updated=(await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],submit_id=job['submit_id']))
            if updated['bridge_job_id']!=job['id']:return updated
            state = self._poll_state(task['id'], job['submit_id'],job['id'])
            if self._clock() < state['next_at']:
                return (await db_call(self.store.get_task,task['id']))
            interval=max(self.poll_interval,60 if task['status']=='uncertain' and self.store.clock()-task['created_at']>7200 else 5)
            state['next_at'] = self._clock() + interval
            try:settings=(await db_call(self.store.settings_for_account,task['account_id'],existing_task=True))
            except PlatformError as error:
                if not str(error.code).startswith('proxy_'):raise
                return await self._query_unavailable(task,state,'proxy_unavailable',base=60)
            if job.get('api_origin'): settings.api_origin=job['api_origin']
            if self._query_slots.locked():return (await db_call(self.store.get_task,task['id']))
            await self._query_slots.acquire()
            try:
                async with asyncio.timeout(self.query_timeout):
                    async with DreaminaClient(settings) as client:
                        data=await client.request(HISTORY_PATH,{'submit_ids':[job['submit_id']]})
                        api_origin=settings.api_origin or getattr(client,'api_base','')
            except (DreaminaError, httpx.HTTPError, TimeoutError) as error:
                if getattr(error,'code','')=='proxy_error':await self.proxy_failure(task['account_id'])
                reason,base=self._query_problem(error)
                return await self._query_unavailable(task,state,reason,base=base)
            finally:self._query_slots.release()
            task=(await db_call(self.store.get_task,task['id']))
            if task['bridge_job_id']!=job['id']:return task
            if task['status'] in ('completed','failed','cancelled'):
                self._polls.pop(task['id'],None);self._locks.pop(task['id'],None)
                return task
            if not isinstance(data,dict):
                return await self._query_unavailable(task,state,'invalid_response')
            record=data.get(job['submit_id'])
            if not record:
                await self._defer_query(task['id'],state,'history_not_found')
                stale = time.time() - (job.get('claimed_at') or job['created_at']) > 120
                return (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],status='uncertain' if stale or task['status']=='uncertain' else 'submitted',
                    error={'code':'upstream_confirmation_missing','message':'The page card has no confirmed upstream task. Review the original page; do not resubmit automatically.'} if stale else task.get('error') if task['status']=='uncertain' else None))
            if not isinstance(record,dict):
                return await self._query_unavailable(task,state,'invalid_response')
            job=(await db_call(self.bridge.get,job['id']))
            try: verify_record(job,record)
            except (ValueError,TypeError,KeyError,IndexError,AttributeError):
                await self._defer_query(task['id'],state,'identity_unconfirmed',base=60)
                return (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],status='uncertain',error={'code':'identity_unconfirmed','message':'The upstream record does not match this task; manual reconciliation is required.'}))
            try:normalized=(normalize_image_record if task['kind']=='image' else normalize_record)(record,job['submit_id'])
            except (ValueError,TypeError,KeyError,IndexError,AttributeError):
                return await self._query_unavailable(task,state,'invalid_response')
            if normalized.get('status') not in ('completed','failed','deleted','processing'):
                return await self._query_unavailable(task,state,'unrecognized_task_state',base=15)
            normalized['api_origin']=api_origin
            (await db_call(self.bridge.reconcile_submitted,job['id'],normalized))
            (await db_call(self.store.finish_submission_turn,job['id']))
            await self._query_recovered(task['id'],state)
            state['next_at']=max(state['next_at'],self._clock()+interval)
            outcome={'completed':'completed','failed':'failed','deleted':'failed'}.get(normalized['status'],'in_progress')
            background_pending=normalized['status']=='processing' and job['status']=='uncertain' and job.get('error_code') in ('generation_timeout','task_timeout')
            if background_pending:outcome='uncertain'
            result = {'images':normalized.get('images',[]),'output_format':'jpeg'} if task['kind']=='image' else normalized
            fresh = (await db_call(self.store.get_task,task['id']))
            if fresh['bridge_job_id']!=job['id']:return fresh
            if fresh['status'] in ('completed','failed','cancelled'): return fresh
            if task.get('status')!=outcome:
                (await db_call(self.store.append_event,task['id'],'upstream_response',{'submit_id':job['submit_id'],'api_origin':settings.api_origin,
                    'status':normalized.get('upstream_status'),'workspace_id':record.get('workspace_id'),'failure':normalized.get('failure'),
                    'videos':normalized.get('videos'),'image_count':len(normalized.get('images',[]))},expected_bridge_job_id=job['id']))
            patch={'status':outcome,'result':result,'error':{'code':job['error_code'],'message':job.get('error') or 'Original generation remains under reconciliation.'} if background_pending else normalized.get('failure')}
            if (job.get('claim_protocol_version') or 0)>=6 and outcome in ('in_progress','submitted'):
                patch['progress_details']={'source':'upstream','observed_at':self.store.clock(),'stage':'generating','value':None}
            if outcome=='completed':
                patch.update(progress=100,progress_details={'source':'terminal','observed_at':time.time(),'stage':'completed'})
                if hasattr(self.store,'clear_account_model_block'):
                    (await db_call(self.store.clear_account_model_block,task['account_id'],task['model']))
            if outcome in ('completed','failed'):
                self._polls.pop(task['id'],None)
                self._locks.pop(task['id'],None)
            return (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=job['id'],**patch))

    async def generate_image(self, spec, settings, *, task):
        if spec.get('operation','generate') != 'generate':
            raise ValueError('Image editing is not connected to a verified worker.')
        dispatch_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-image-dispatch:'+task['id']))
        (await db_call(self.store.update_task,task['id'],bridge_job_id=dispatch_id))
        try:job=(await db_call(self.bridge.enqueue,{'kind':'image','model':spec['model'],'prompt':spec['prompt'],
                                 'n':spec['n'],'size':spec['size'],'ratio':'1:1','resolution':'2K'},
                                account_id=task['account_id'],job_id=dispatch_id))
        except Exception:
            if (await db_call(self.bridge.fail_unactivated,dispatch_id)):
                (await db_call(self.store.update_task,task['id'],expected_bridge_job_id=dispatch_id,status='failed',error={'code':'dispatch_failed','message':'Image dispatch failed before generation activation.'}))
            raise
        (await db_call(self.store.update_task,task['id'],bridge_job_id=job['id']))
        (await db_call(self.store.update_task,task['id'],request_detail={'method':'POST','path':'/v1/images/generations','parameters':spec,'new_session':True}))
        (await db_call(self.store.append_event,task['id'],'request_validated',{'parameters':spec,'new_session':True}))
        while True:
            current=await self.sync_task(task)
            if current['status']=='completed': return current['result']
            if current['status']=='failed':
                raise DreaminaError((current.get('error') or {}).get('message') or 'Image generation failed.',code='generation_failed')
            if current['status']=='uncertain':
                raise DreaminaError('Image submission needs reconciliation; do not submit again.',code='submission_uncertain')
            await asyncio.sleep(2)

    async def download_video(self, task):
        lock=self._downloads.setdefault(task['id'],asyncio.Lock())
        async with lock:
            task=(await db_call(self.store.get_task,task['id']))
            destination=ROOT/'outputs'/'media'/f"{task['id']}.mp4"
            if task.get('output_path')==str(destination) and destination.is_file():return destination
            settings=(await db_call(self.store.settings_for_account,task['account_id'],existing_task=True))
            if (task.get('result') or {}).get('api_origin'):settings.api_origin=task['result']['api_origin']
            for attempt in range(3):
                try:
                    # Each attempt re-queries the original ID for a fresh signed media URL.
                    async with DreaminaClient(settings) as client:
                        path=await client.download(task['submit_id'],destination)
                    (await db_call(self.store.update_task,task['id'],output_path=str(path)));return path
                except (DreaminaError,httpx.HTTPError,TimeoutError) as error:
                    retryable=isinstance(error,(httpx.HTTPError,TimeoutError)) or getattr(error,'code',None) in ('download_failed','network_error')
                    if not retryable or attempt==2:raise
                    await self._query_event(task['id'],'video_download_retry',{'attempt':attempt+1,'action':'download_original_video_only'})
                    await asyncio.sleep(2**attempt)

    async def download_image(self, entry, settings):
        url=entry.get('url','')
        allowed=('capcutcdn-us.com','capcutcdn.com','byteimg.com','ibytedtos.com','byteoversea.com')
        async with DreaminaClient(settings) as account_client:
            client=account_client.http
            for _ in range(5):
                parsed=urlsplit(url)
                if parsed.scheme!='https' or not any(parsed.hostname==h or (parsed.hostname or '').endswith('.'+h) for h in allowed):
                    raise ValueError('Image result must use an observed HTTPS Dreamina media host.')
                async with client.stream('GET',url) as response:
                    if response.is_redirect:
                        url=urljoin(url,response.headers['location']);continue
                    response.raise_for_status()
                    data=bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data)>20*1024*1024: raise ValueError('Image output exceeds 20 MiB.')
                    return bytes(data)
        raise ValueError('Too many image result redirects.')

    async def run_reconciler(self):
        while True:
            try:
                await db_call(self.bridge.expire_claims)
                cursor = None
                while True:
                    batch = (await db_call(self.store.reconciliation_tasks,after_id=cursor, limit=100))
                    if not batch: break
                    slots=asyncio.Semaphore(8)
                    async def reconcile(task):
                        async with slots:
                            try:await self.sync_task(task)
                            except Exception:log.warning('Task %s reconciliation temporarily unavailable',task['id'])
                    await asyncio.gather(*(reconcile(task) for task in batch))
                    cursor = batch[-1]['id']
            except asyncio.CancelledError: raise
            except Exception: log.warning('Task reconciliation scan unavailable')
            await asyncio.sleep(5)
