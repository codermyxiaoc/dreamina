from copy import deepcopy
import json

import pytest

from dreamina_api.image_models import IMAGE_UPSTREAM_MODEL, normalize_image_record, validate_image_spec
from dreamina_api.chrome_verify import verify_record


def sample_spec(**updates):
    return {'kind': 'image', 'model': 'seedream-5.0-lite', 'prompt': 'A small blue ceramic vase.',
            'size': '2048x2048', 'n': 1, 'ratio': '1:1', 'resolution': '2K', **updates}


def synthetic_pair():
    spec = sample_spec()
    job = {'submit_id': 'test-submit', 'created_at': 1000, 'claimed_at': 1001, 'request': spec}
    # Field names match the captured schema; all values and IDs are synthetic.
    draft = {'component_list': [{'type': 'image_base_component', 'abilities': {
        'generate': {'core_param': {'model': IMAGE_UPSTREAM_MODEL, 'prompt': spec['prompt'],
                                    'large_image_info': {'width': 2048, 'height': 2048}}},
        'gen_option': {'gen_count': 1, 'generate_all': False},
    }}]}
    record = {'submit_id': 'test-submit', 'created_time': 1002.125, 'status': 50,
              'draft_content': json.dumps(draft), 'item_list': [{'image': {
                  'format': 'jpeg', 'large_images': [{'image_url': 'https://example.invalid/generated.jpg',
                                                    'width': 2048, 'height': 2048, 'format': 'jpeg'}]}}]}
    return job, record


def test_image_defaults_and_input_immutability():
    value = {'prompt': 'A small blue ceramic vase.'}
    original = deepcopy(value)
    assert validate_image_spec(value) == sample_spec()
    assert value == original


def test_image_prompt_whitespace_normalized_for_dom_identity():
    assert validate_image_spec(sample_spec(prompt='  A small blue ceramic vase.\n '))['prompt'] == 'A small blue ceramic vase.'


@pytest.mark.parametrize('changes', [
    {'model': 'seedream-4.5'}, {'n': 2}, {'n': '1'}, {'size': '1024x1024'},
    {'ratio': '16:9'}, {'resolution': '4K'}, {'image': 'some.png'},
    {'references': []}, {'seed': 5}, {'mode': 'frames'},
])
def test_unsupported_image_options_rejected(changes):
    with pytest.raises(ValueError):
        validate_image_spec(sample_spec(**changes))


@pytest.mark.parametrize('value', [None, [], 'prompt', 1])
def test_image_spec_requires_a_dictionary(value):
    with pytest.raises(ValueError):
        validate_image_spec(value)


@pytest.mark.parametrize('n', [True, False, 1.0])
def test_image_count_is_a_real_integer(n):
    with pytest.raises(ValueError):
        validate_image_spec(sample_spec(n=n))


@pytest.mark.parametrize('kind', ['video', 'other', None, False])
def test_image_kind_must_not_contradict_the_model(kind):
    with pytest.raises(ValueError):
        validate_image_spec(sample_spec(kind=kind))


def test_image_prompt_utf16_limit():
    assert validate_image_spec(sample_spec(prompt='😀' * 1000))['prompt'] == '😀' * 1000
    with pytest.raises(ValueError):
        validate_image_spec(sample_spec(prompt='😀' * 1001))
    for prompt in ['', '   ', None, 123]:
        with pytest.raises(ValueError):
            validate_image_spec(sample_spec(prompt=prompt))


def test_image_normalization_preserves_actual_image_metadata():
    _, record = synthetic_pair()
    result = normalize_image_record(record, 'test-submit')
    assert result['status'] == 'completed'
    assert result['object'] == 'image'
    assert result['images'] == [{'url': 'https://example.invalid/generated.jpg', 'width': 2048,
                                 'height': 2048, 'format': 'jpeg'}]
    assert result['failure'] is None


def test_status_50_without_image_does_not_claim_success():
    _, record = synthetic_pair()
    record['item_list'] = []
    assert normalize_image_record(record, 'test-submit')['status'] == 'processing'


@pytest.mark.parametrize('status', [10, 30, 40, 100])
def test_image_failure_states_and_missing_translation(status):
    result = normalize_image_record({'status': status, 'fail_code': 2052, 'fail_starling_message': '[none]',
                                     'fail_starling_key': 'Synthetic_Audit_Reject'}, 'test-submit')
    assert result['status'] == 'failed'
    assert result['failure'] == {'code': '2052', 'message': 'Synthetic_Audit_Reject'}


def test_image_verification_accepts_synthetic_matching_draft():
    verify_record(*synthetic_pair())


@pytest.mark.parametrize('field,value', [('prompt', 'Wrong prompt'), ('model', 'wrong-model')])
def test_image_verification_rejects_wrong_core_fields(field, value):
    job, record = synthetic_pair()
    draft = json.loads(record['draft_content'])
    draft['component_list'][0]['abilities']['generate']['core_param'][field] = value
    record['draft_content'] = draft
    with pytest.raises(ValueError):
        verify_record(job, record)


@pytest.mark.parametrize('dimension', ['width', 'height'])
def test_image_verification_rejects_wrong_size(dimension):
    job, record = synthetic_pair()
    draft = json.loads(record['draft_content'])
    draft['component_list'][0]['abilities']['generate']['core_param']['large_image_info'][dimension] = 1024
    record['draft_content'] = draft
    with pytest.raises(ValueError):
        verify_record(job, record)


def test_image_verification_rejects_wrong_count():
    job, record = synthetic_pair()
    draft = json.loads(record['draft_content'])
    draft['component_list'][0]['abilities']['gen_option']['gen_count'] = 4
    record['draft_content'] = draft
    with pytest.raises(ValueError):
        verify_record(job, record)


def test_image_verification_rejects_old_or_other_submission():
    job, record = synthetic_pair()
    record['created_time'] = 900
    with pytest.raises(ValueError, match='older task'):
        verify_record(job, record)
    record['created_time'] = 1002
    record['submit_id'] = 'another-submission'
    with pytest.raises(ValueError, match='submit_id'):
        verify_record(job, record)


def test_image_verification_requires_full_draft_evidence():
    job, record = synthetic_pair()
    record['draft_content'] = {'component_list': [{'abilities': {'generate': {}}}]}
    with pytest.raises(ValueError, match='unavailable'):
        verify_record(job, record)
