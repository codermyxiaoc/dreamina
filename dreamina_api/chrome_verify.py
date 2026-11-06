"""Bind a DOM-observed task to its actual API request, not just matching card text."""
import json
import math

from .models import model_config
from .image_models import IMAGE_UPSTREAM_MODEL
from .prompt_mentions import has_prompt_mentions, verify_prompt_mentions
from .prompt_text import prompt_text_equal


def verify_record(job: dict, record: dict) -> None:
    if record.get('submit_id') != job.get('submit_id'):
        raise ValueError('Upstream submit_id does not match the observed page card.')
    created = record.get('created_time')
    claimed = job.get('claimed_at') or job['created_at']
    if not isinstance(created, (int, float)) or isinstance(created, bool) or not math.isfinite(created):
        raise ValueError('Upstream task creation time is unavailable; identity cannot be confirmed.')
    if created < claimed - 5:
        raise ValueError('The observed card belongs to an older task; do not accept it as this submission.')
    try:
        draft = record['draft_content']
        if isinstance(draft, str):
            draft = json.loads(draft)
        component = draft['component_list'][0]
        if job['request'].get('kind') == 'image':
            core = component['abilities']['generate']['core_param']
            size = core['large_image_info']
            if (not prompt_text_equal(core['prompt'],job['request']['prompt']) or core['model'] != IMAGE_UPSTREAM_MODEL
                    or size['width'] != 2048 or size['height'] != 2048
                    or component['abilities']['gen_option']['gen_count'] != 1):
                raise ValueError('Image task parameters do not match this bridge job.')
            return
        params = component['abilities']['gen_video']['text_to_video_params']
        item = params['video_gen_inputs'][0]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError('Upstream task request is unavailable; identity cannot be confirmed.') from exc
    spec = job['request']
    mentioned=has_prompt_mentions(spec['prompt'])
    if mentioned:
        verify_prompt_mentions(spec['prompt'],spec.get('references',[]),item.get('unified_edit_input'),job.get('reference_bindings'))
    unified_text=''.join(m.get('text','') for m in (item.get('unified_edit_input') or {}).get('meta_list',[]) if m.get('meta_type')=='text').strip()
    if not mentioned and not (prompt_text_equal(unified_text,spec['prompt']) or prompt_text_equal(item.get('prompt'),spec['prompt'])):
        raise ValueError('Upstream request parameters do not match this bridge job.')
    comparisons = [
        (params.get('model_req_key'), model_config(spec['model'])['model_req_key']),
        (params.get('video_aspect_ratio'), spec['ratio']),
        (item.get('duration_ms'), spec['duration'] * 1000),
        (str(item.get('resolution', '')).lower(), spec['resolution'].lower()),
    ]
    if any(actual != expected for actual, expected in comparisons):
        raise ValueError('Upstream request parameters do not match this bridge job.')
    expected=spec.get('references',[])
    actual=(item.get('unified_edit_input') or {}).get('material_list') or []
    if len(actual)!=len(expected):raise ValueError('Upstream reference count does not match this job.')
    for kind in ('image','video'):
        if sum(m.get('material_type')==kind for m in actual)!=sum(m['kind']==kind for m in expected):
            raise ValueError('Upstream reference types do not match this job.')
    conversation=(job.get('telemetry') or {}).get('conversation_id')
    if conversation and record.get('workspace_id') is not None and str(record['workspace_id'])!=conversation:
        raise ValueError('Upstream task belongs to a different conversation.')
