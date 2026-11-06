"""Read-only video reliability statistics with explicit, non-selective denominators."""
from __future__ import annotations

import json
import math
import time

from sqlalchemy import case, func, select

from .platform_store import tasks, task_events


TARGET = 0.99
WILSON_Z = 1.959963984540054  # Two-sided 95% normal quantile.
ACTIVE = {'queued', 'claimed', 'submitted', 'in_progress'}
TERMINAL = {'completed', 'failed', 'cancelled'}
FAILURE_LABELS = {
    'account_plan': '套餐或账号权限',
    'authentication': '登录或会话失效',
    'reference_input': '参考素材或参数',
    'network_timeout': '网络或超时',
    'upstream_rejected': '上游生成拒绝',
    'browser_interface': '浏览器或界面执行',
    'unknown': '未分类失败',
}


def wilson_lower(successes, trials):
    """Two-sided 95% Wilson lower bound; no sample has no interval."""
    if trials <= 0:
        return None
    p = successes / trials
    z2 = WILSON_Z * WILSON_Z
    center = p + z2 / (2 * trials)
    radius = WILSON_Z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials))
    return max(0.0, (center - radius) / (1 + z2 / trials))


def _failure_category(raw, plan_event=False):
    if plan_event:
        return 'account_plan'
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError, RecursionError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    # Only classify internally. Raw error content is never included in the result.
    fields = [value.get('code'), value.get('message')]
    text = ' '.join(v[:3000] for v in fields if isinstance(v, str)).lower()
    if any(term in text for term in ('plan upgrade', 'subscription upgrade', 'account_plan', '套餐', '升级界面')):
        return 'account_plan'
    if any(term in text for term in ('login_required', 'login required', 'session_expired', 'invalid_session', 'authentication', '登录', '会话失效')):
        return 'authentication'
    if any(term in text for term in ('reference', 'invalid_video_parameters', 'invalid_request', 'upload', '素材', '参数')):
        return 'reference_input'
    if any(term in text for term in ('timeout', 'timed out', 'network', 'connection', 'transport', '超时', '网络')):
        return 'network_timeout'
    if any(term in text for term in ('upstream', 'ret=', 'generation_rejected', '上游')):
        return 'upstream_rejected'
    if any(term in text for term in ('browser', 'worker', 'uiunavailable', 'interface', '浏览器', '控件')):
        return 'browser_interface'
    return 'unknown'


def _bucket():
    return {'accepted': 0, 'completed': 0, 'failed': 0, 'cancelled': 0,
            'active': 0, 'uncertain': 0, 'unknown': 0,
            'manual_reconciled': 0, 'manual_reconciled_completed': 0,
            'failure_categories': {key: 0 for key in FAILURE_LABELS}}


def _add(bucket, row):
    bucket['accepted'] += 1
    status = row['status']
    slot = status if status in TERMINAL or status == 'uncertain' else 'active' if status in ACTIVE else 'unknown'
    bucket[slot] += 1
    if row['manual_reconciled']:
        bucket['manual_reconciled'] += 1
        bucket['manual_reconciled_completed'] += int(status == 'completed')
    if status == 'failed':
        bucket['failure_categories'][_failure_category(row['error_json'], row['plan_event'])] += 1


def _finalize(bucket):
    result = dict(bucket)
    ended = sum(bucket[key] for key in TERMINAL)
    outstanding = bucket['active'] + bucket['uncertain'] + bucket['unknown']
    lower = wilson_lower(bucket['completed'], ended)
    automatic = bucket['completed'] - bucket['manual_reconciled_completed']
    automatic_lower = wilson_lower(automatic, ended)
    result.update(
        terminal=ended,
        outstanding=outstanding,
        automatic_completed=automatic,
        automatic_closed_success_rate_percent=round(100 * automatic / ended, 4) if ended else None,
        automatic_wilson_95_lower_percent=round(100 * automatic_lower, 4) if automatic_lower is not None else None,
        closed_success_rate_percent=round(100 * bucket['completed'] / ended, 4) if ended else None,
        accepted_completion_rate_percent=round(100 * bucket['completed'] / bucket['accepted'], 4) if bucket['accepted'] else None,
        wilson_95_lower_percent=round(100 * lower, 4) if lower is not None else None,
        sample_supports_target=bool(ended and not outstanding and automatic_lower is not None and automatic_lower >= TARGET),
    )
    return result


def reliability_snapshot(store):
    """Count every stored video task, not a paginated/latest task listing.

    A single SELECT gives one database snapshot and never reads task payloads,
    account credentials, signed media URLs, or API keys. Event flags are grouped
    before joining so repeated recovery events do not multiply task counts.
    """
    flags = select(
        task_events.c.task_id,
        func.max(case((task_events.c.stage == 'manual_reconciliation', 1), else_=0)).label('manual_reconciled'),
        func.max(case((task_events.c.stage == 'account_plan_required', 1), else_=0)).label('plan_event'),
    ).where(task_events.c.stage.in_(['manual_reconciliation', 'account_plan_required'])).group_by(task_events.c.task_id).subquery()
    statement = select(
        tasks.c.model, tasks.c.status, tasks.c.error_json, tasks.c.created_at,
        flags.c.manual_reconciled, flags.c.plan_event,
    ).outerjoin(flags, flags.c.task_id == tasks.c.id).where(tasks.c.kind == 'video')
    overall = _bucket()
    models = {}
    earliest = None
    with store.engine.connect() as conn:
        for row in conn.execution_options(stream_results=True).execute(statement).mappings():
            _add(overall, row)
            _add(models.setdefault(row['model'], _bucket()), row)
            created = row['created_at']
            if isinstance(created, (int, float)) and math.isfinite(created):
                earliest = created if earliest is None else min(earliest, created)
    all_success_minimum = math.ceil(WILSON_Z * WILSON_Z * TARGET / (1 - TARGET))
    return {
        'generated_at': time.time(),
        'scope': 'all_recorded_video_tasks',
        'first_task_at': earliest,
        'target_percent': 100 * TARGET,
        'target_is_guarantee': False,
        'overall': _finalize(overall),
        'by_model': [dict(model=model, **_finalize(bucket)) for model, bucket in sorted(models.items())],
        'failure_categories': [dict(code=key, label=label, count=overall['failure_categories'][key]) for key, label in FAILURE_LABELS.items()],
        'definitions': {
            'accepted': '所有已落库的视频任务；API鉴权或校验阶段未创建任务的拒绝不在此口径内。',
            'terminal': 'completed + failed + cancelled；权限失败与取消均保留在分母。',
            'closed_success_rate': 'completed / terminal，交付总成功率包含手工核对后完成，分母为已结束任务。',
            'automatic_completed': 'completed - manual_reconciled_completed；仅manual_reconciliation事件对应的完成任务扣除。自动查询与回执恢复仍计入自动完成。',
            'automatic_closed_success_rate': 'automatic_completed / terminal；手工恢复完成仍在分母，但不进入自动完成分子。',
            'accepted_completion_rate': 'completed / accepted；未完成和待核对不会从全部请求分母中隐藏。',
            'uncertain': '单独列示，仍占未完成任务，不作为成功。',
            'confidence': '已结束任务样本的95%双侧Wilson区间下界；描述性统计，不是长期SLA保证，任务相关性与未完成样本会影响解释。',
            'automatic_confidence': '99%目标按自动完成率的95%双侧Wilson下界判断，并要求没有未完成或待核对任务；手工恢复不会提高该自动化指标。',
            'manual_reconciled': '出现manual_reconciliation事件的去重任务数；其中完成数另列。',
        },
        'evidence': {
            'method': 'two_sided_95_percent_wilson',
            'target_basis': 'automatic_closed_success_rate',
            'minimum_all_success_terminal_samples_for_target': all_success_minimum,
            'requires_no_outstanding_tasks': True,
            'note': f'即使全部自动完成，也至少需要{all_success_minimum}个已结束样本，自动完成率的95%双侧Wilson下界才达到99%；仍不能承诺未来成功率。',
        },
    }
