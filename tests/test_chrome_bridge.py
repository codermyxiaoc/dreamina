"""Synthetic bridge tests: no browser, credentials, network, or paid task."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from dreamina_api.chrome_bridge import BridgeError, BridgeStore, create_chrome_bridge_router


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return BridgeStore(tmp_path / 'bridge', clock=clock)


def ready(store, worker='worker-a', account='default'):
    return store.heartbeat(account, worker, ready=True,
                           page_url='https://dreamina.capcut.com/ai-tool/generate?workspace=private#fragment')


def queued(store, account='default'):
    return store.enqueue({'prompt': 'Synthetic camera test'}, account_id=account)


def claimed(store):
    ready(store)
    queued(store)
    return store.claim('default', 'worker-a')


def report(store, job, status, **kwargs):
    return store.report(job['id'], 'worker-a', job['claim_token'], status=status, **kwargs)


def test_queue_normalizes_only_supported_first_version(store):
    job = queued(store)
    assert job['status'] == 'queued'
    assert job['account_id'] == 'default'
    assert job['request'] == {'prompt': 'Synthetic camera test', 'model': 'seedance-2.0-mini',
                              'duration': 4, 'ratio': '16:9', 'resolution': '720p', 'mode': 'omni','new_session':True,'references':[],'asset_owner_task_id':None}
    assert job['model_name'] == 'Dreamina Seedance 2.0 Mini'
    assert job['submission_evidence'] is None
    assert 'claim_token' not in job and 'claim_hash' not in job


@pytest.mark.parametrize('extra', [
    {'model':'unsupported'}, {'duration':99}, {'resolution':'4k'}, {'ratio':'7:3'}, {'references':[{}]},
    {'first_frame': {}}, {'seed': 123}, {'mode': 'frames'}, {'prompt': ''},
])
def test_unsupported_options_are_rejected_before_queueing(store, extra):
    with pytest.raises(ValueError):
        store.enqueue({'prompt': 'test', **extra})
    assert store.list_jobs() == []


def test_claim_requires_ready_recent_heartbeat(store, clock):
    queued(store)
    with pytest.raises(BridgeError):
        store.claim('default', 'worker-a')
    store.heartbeat('default', 'worker-a', ready=False, page_url='https://dreamina.capcut.com/ai-tool/generate')
    with pytest.raises(BridgeError):
        store.claim('default', 'worker-a')
    ready(store)
    clock.now += store.heartbeat_timeout+1
    with pytest.raises(BridgeError):
        store.claim('default', 'worker-a')


def test_concurrent_workers_cannot_claim_two_jobs_for_one_account(store):
    queued(store)
    queued(store)
    ready(store, 'worker-a')
    ready(store, 'worker-b')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda worker: store.claim('default', worker), ['worker-a', 'worker-b']))
    assert sum(job is not None for job in results) == 1
    assert sorted(job['status'] for job in store.list_jobs()) == ['claimed', 'queued']


def test_accounts_have_separate_queues_and_workers_are_bound(store):
    queued(store, 'one')
    queued(store, 'two')
    ready(store, 'worker-a', 'one')
    ready(store, 'worker-b', 'two')
    assert store.claim('one', 'worker-a')['account_id'] == 'one'
    assert store.claim('two', 'worker-b')['account_id'] == 'two'
    with pytest.raises(BridgeError, match='different account'):
        ready(store, 'worker-a', 'two')


def test_claim_is_not_returned_twice_or_stored_as_plaintext(store):
    job = claimed(store)
    assert store.claim('default', 'worker-a') is None
    public = store.get(job['id'])
    assert 'claim_token' not in public and 'claim_hash' not in public
    with sqlite3.connect(store.path) as db:
        row = db.execute('SELECT claim_hash FROM jobs WHERE id=?', (job['id'],)).fetchone()
    assert len(row[0]) == 64
    assert row[0] != job['claim_token']


def test_disconnect_freezes_claim_and_never_requeues_it(store, clock):
    job = claimed(store)
    following = queued(store)
    clock.now += store.heartbeat_timeout+1
    store.expire_claims()
    assert store.get(job['id'])['status'] == 'uncertain'
    ready(store)
    assert store.claim('default', 'worker-a') is None
    assert store.get(following['id'])['status'] == 'queued'
    assert store.accounts()[0]['blocked_by_uncertain'] is True


def test_expired_claim_is_persistent_across_restart(store, clock):
    job = claimed(store)
    clock.now += store.heartbeat_timeout+1
    reopened = BridgeStore(store.directory, clock=clock)
    reopened.expire_claims()
    assert reopened.get(job['id'])['status'] == 'uncertain'
    ready(reopened)
    queued(reopened)
    assert reopened.claim('default', 'worker-a') is None


def test_hard_action_deadline_expires_even_when_worker_stays_online(store, clock):
    job = claimed(store)
    for _ in range(17):
        clock.now += 35
        ready(store)
    assert store.get(job['id'])['status'] == 'claimed'
    clock.now += 6
    ready(store)
    assert store.get(job['id'])['status'] == 'uncertain'


def test_uncertain_claim_can_be_reconciled_by_original_worker_but_not_reassigned(store, clock):
    job = claimed(store)
    following = queued(store)
    clock.now += store.heartbeat_timeout+1
    store.expire_claims()
    assert store.get(job['id'])['status'] == 'uncertain'
    ready(store)
    result = report(store, job, 'submitted', submit_id=str(uuid.uuid4()))
    assert result['status'] == 'submitted'
    assert result['submission_evidence'] == 'native-response-observed'
    assert store.claim('default', 'worker-a')['id'] == following['id']


def test_worker_cannot_report_without_own_claim_or_replace_submit_id(store):
    job = claimed(store)
    with pytest.raises(BridgeError) as error:
        store.report(job['id'], 'worker-b', job['claim_token'], status='failed')
    assert error.value.status == 403
    with pytest.raises(BridgeError):
        store.report(job['id'], 'worker-a', 'wrong-token', status='failed')
    submit_id = str(uuid.uuid4())
    assert report(store, job, 'submitted', submit_id=submit_id)['submit_id'] == submit_id
    assert report(store, job, 'submitted', submit_id=submit_id)['status'] == 'submitted'
    with pytest.raises(BridgeError):
        report(store, job, 'submitted', submit_id=str(uuid.uuid4()))
    with pytest.raises(BridgeError):
        report(store, job, 'uncertain')


def test_extension_cannot_claim_completion_and_submitted_requires_uuid(store):
    job = claimed(store)
    with pytest.raises(ValueError):
        report(store, job, 'completed', submit_id=str(uuid.uuid4()))
    with pytest.raises(ValueError):
        report(store, job, 'submitted')
    with pytest.raises(ValueError):
        report(store, job, 'submitted', submit_id='not-a-task-uuid')
    assert store.get(job['id'])['status'] == 'claimed'


def test_duplicate_upstream_task_cannot_be_bound_to_a_second_job(store):
    first = claimed(store)
    submit_id = str(uuid.uuid4())
    report(store, first, 'submitted', submit_id=submit_id)
    queued(store)
    second = store.claim('default', 'worker-a')
    with pytest.raises(BridgeError, match='already associated'):
        report(store, second, 'submitted', submit_id=submit_id)


def test_http_reconciliation_requires_exact_id_and_real_media_for_completion(store):
    job = claimed(store)
    submit_id = str(uuid.uuid4())
    report(store, job, 'submitted', submit_id=submit_id)
    with pytest.raises(BridgeError, match='does not match'):
        store.reconcile_submitted(job['id'], {'id': str(uuid.uuid4()), 'status': 'completed', 'videos': [{}]})
    assert store.reconcile_submitted(job['id'], {'id': submit_id, 'status': 'processing', 'videos': []})['status'] == 'submitted'
    assert store.reconcile_submitted(job['id'], {'id': submit_id, 'status': 'completed', 'videos': []})['status'] == 'submitted'
    result = store.reconcile_submitted(job['id'], {'id': submit_id, 'status': 'completed', 'upstream_status': 50,
                                                  'videos': [{'url': 'https://media.example/result.mp4'}]})
    assert result['status'] == 'completed'
    assert result['result']['upstream_status'] == 50
    with pytest.raises(BridgeError):
        report(store, job, 'failed')


def test_http_failure_reason_is_preserved(store):
    job = claimed(store)
    submit_id = str(uuid.uuid4())
    report(store, job, 'submitted', submit_id=submit_id)
    failure = {'code': '2052', 'reason_key': 'audio_output_audit_failed'}
    result = store.reconcile_submitted(job['id'], {'id': submit_id, 'status': 'failed', 'videos': [], 'failure': failure})
    assert result['status'] == 'failed'
    assert result['result']['failure'] == failure


def test_heartbeat_keeps_only_page_path_not_private_url_fields(store):
    ready(store)
    encoded = json.dumps(store.accounts())
    assert 'workspace' not in encoded and 'private' not in encoded and 'fragment' not in encoded
    for page_url in ['http://dreamina.capcut.com/ai-tool/generate', 'https://unrelated.example/ai-tool/generate',
                     'https://user:secret@dreamina.capcut.com/ai-tool/generate', 'https://dreamina.capcut.com:444/ai-tool/generate']:
        with pytest.raises(ValueError):
            store.heartbeat('default', 'worker-a', ready=True, page_url=page_url)


BRIDGE_KEY = 'synthetic-bridge-key-long-enough'
EXTENSION_ID = 'a' * 32
ORIGIN = 'chrome-extension://' + EXTENSION_ID
BRIDGE_HEADERS = {'X-Chrome-Bridge-Key': BRIDGE_KEY, 'Origin': ORIGIN}
API_HEADERS = {'Authorization': 'Bearer synthetic-local-key'}


@pytest.fixture
def api(store):
    def authorize(request: Request):
        if request.headers.get('origin'):
            raise HTTPException(403, 'Use a local API client.')
        if request.headers.get('authorization') != API_HEADERS['Authorization']:
            raise HTTPException(401, 'Local API key required.')
    app = FastAPI()
    app.include_router(create_chrome_bridge_router(authorize, store=store,
        pairing_key_loader=lambda: BRIDGE_KEY, extension_id_loader=lambda: EXTENSION_ID))
    with TestClient(app) as client:
        yield client


def test_http_contract_queue_heartbeat_claim_report(api):
    job = api.post('/v1/chrome/jobs', headers=API_HEADERS, json={'request': {'prompt': 'Synthetic test'}})
    assert job.status_code == 202
    job_id = job.json()['id']
    heartbeat = api.post('/v1/chrome/heartbeat', headers=BRIDGE_HEADERS, json={
        'account_id': 'default', 'worker_id': 'worker-a', 'ready': True, 'protocol_version':4,
        'page_url': 'https://dreamina.capcut.com/ai-tool/generate'})
    assert heartbeat.status_code == 200
    claim = api.post('/v1/chrome/claim', headers=BRIDGE_HEADERS, json={'account_id': 'default', 'worker_id': 'worker-a'})
    assert claim.status_code == 200
    payload = claim.json()['job']
    assert payload['id'] == job_id
    submit_id = str(uuid.uuid4())
    response = api.post(f'/v1/chrome/jobs/{job_id}/report', headers=BRIDGE_HEADERS, json={
        'worker_id': 'worker-a', 'claim_token': payload['claim_token'], 'status': 'submitted', 'submit_id': submit_id})
    assert response.status_code == 200
    assert response.json()['submit_id'] == submit_id
    assert response.headers['access-control-allow-origin'] == ORIGIN
    assert 'claim_token' not in api.get(f'/v1/chrome/jobs/{job_id}', headers=API_HEADERS).json()
    assert api.get('/v1/chrome/accounts', headers=API_HEADERS).json()['accounts'][0]['jobs'] == {'submitted': 1}


def test_bridge_key_cannot_enqueue_and_local_key_cannot_claim(api):
    assert api.post('/v1/chrome/jobs', headers=BRIDGE_HEADERS, json={'request': {'prompt': 'test'}}).status_code == 403
    assert api.post('/v1/chrome/jobs', json={'request': {'prompt': 'test'}}).status_code == 401
    assert api.post('/v1/chrome/claim', headers=API_HEADERS, json={'worker_id': 'worker-a'}).status_code == 401


@pytest.mark.parametrize('headers,status', [
    ({}, 401),
    ({'Origin': ORIGIN, 'X-Chrome-Bridge-Key': 'wrong'}, 401),
    ({'Origin': 'https://dreamina.capcut.com', 'X-Chrome-Bridge-Key': BRIDGE_KEY}, 403),
    ({'Origin': 'https://unrelated.example', 'X-Chrome-Bridge-Key': BRIDGE_KEY}, 403),
    ({'Origin': 'chrome-extension://' + 'b' * 32, 'X-Chrome-Bridge-Key': BRIDGE_KEY}, 403),
])
def test_worker_auth_and_origin_are_both_enforced(api, headers, status):
    response = api.post('/v1/chrome/heartbeat', headers=headers, json={
        'worker_id': 'worker-a', 'ready': True, 'protocol_version':4, 'page_url': 'https://dreamina.capcut.com/ai-tool/generate'})
    assert response.status_code == status
    if headers.get('Origin') == ORIGIN:
        assert response.headers['access-control-allow-origin'] == ORIGIN
    else:
        assert 'access-control-allow-origin' not in response.headers


def test_no_origin_worker_still_requires_key(api):
    response = api.post('/v1/chrome/heartbeat', headers={'X-Chrome-Bridge-Key': BRIDGE_KEY}, json={
        'worker_id': 'worker-a', 'ready': True, 'protocol_version':4, 'page_url': 'https://dreamina.capcut.com/ai-tool/generate'})
    assert response.status_code == 200
    assert 'access-control-allow-origin' not in response.headers


def test_cors_preflight_is_exact_not_wildcard(api):
    response = api.options('/v1/chrome/heartbeat', headers={'Origin': ORIGIN,
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-chrome-bridge-key'})
    assert response.status_code == 204
    assert response.headers['access-control-allow-origin'] == ORIGIN
    assert 'access-control-allow-credentials' not in response.headers
    assert '*' not in response.headers['access-control-allow-headers']
    assert api.options('/v1/chrome/heartbeat', headers={'Origin': 'https://unrelated.example'}).status_code == 403
    assert api.options('/v1/chrome/heartbeat', headers={'Origin': ORIGIN,
        'Access-Control-Request-Headers': 'authorization'}).status_code == 403


def test_queue_rejects_unsupported_scope_and_bridge_payload_typing(api):
    assert api.post('/v1/chrome/jobs', headers=API_HEADERS,
                    json={'request': {'prompt': 'test', 'duration': 99}}).status_code == 422
    assert api.post('/v1/chrome/heartbeat', headers=BRIDGE_HEADERS,
                    json={'worker_id': 'worker-a', 'ready': 'true', 'page_url': 'https://dreamina.capcut.com/ai-tool/generate'}).status_code == 422


def test_unconfigured_bridge_key_fails_closed(tmp_path):
    app = FastAPI()
    app.include_router(create_chrome_bridge_router(lambda: None, store=BridgeStore(tmp_path),
        pairing_key_loader=lambda: '', extension_id_loader=lambda: EXTENSION_ID))
    with TestClient(app) as client:
        response = client.post('/v1/chrome/claim', headers=BRIDGE_HEADERS, json={'worker_id': 'worker-a'})
    assert response.status_code == 503
