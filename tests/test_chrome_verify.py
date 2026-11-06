from copy import deepcopy
import json

import pytest

from dreamina_api.chrome_verify import verify_record
from dreamina_api.payloads import build_generation


def pair():
    spec = {'prompt': 'Clouds over a quiet lake', 'model': 'seedance-2.0-mini',
            'duration': 4, 'ratio': '16:9', 'resolution': '720p', 'mode': 'omni'}
    prepared = build_generation(spec)
    job = {'submit_id': prepared['submit_id'], 'created_at': 1000, 'claimed_at': 1001, 'request': spec}
    record = {'submit_id': prepared['submit_id'], 'created_time': 1002, 'draft_content': prepared['body']['draft_content']}
    return job, record


def test_actual_task_request_matches_claim():
    verify_record(*pair())


def test_virtualized_old_matching_card_is_rejected():
    job, record = pair()
    record['created_time'] = 900
    with pytest.raises(ValueError, match='older task'):
        verify_record(job, record)


@pytest.mark.parametrize('field,value', [('prompt', 'Another prompt'), ('duration_ms', 14000), ('resolution', '1080p')])
def test_wrong_task_inputs_rejected(field, value):
    job, record = pair()
    draft = json.loads(record['draft_content'])
    draft['component_list'][0]['abilities']['gen_video']['text_to_video_params']['video_gen_inputs'][0][field] = value
    record['draft_content'] = draft
    with pytest.raises(ValueError, match='parameters'):
        verify_record(job, record)


def test_missing_task_evidence_is_not_accepted():
    job, record = pair()
    record.pop('draft_content')
    with pytest.raises(ValueError, match='unavailable'):
        verify_record(job, record)
