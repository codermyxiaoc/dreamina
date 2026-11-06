import json
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from dreamina_api.worker_relay import RelayConfig, create_relay_app


LOCAL = 'local-pairing-' + 'a' * 32
REMOTE = 'remote-pairing-' + 'b' * 32
EXTENSION = 'eicelfibiacmlhfepgoiiemfgcbdfafm'
ORIGIN = 'chrome-extension://' + EXTENSION


def config(**overrides):
    values = {'remote_api_url': 'https://gateway.example', 'local_bridge_key': LOCAL,
              'remote_bridge_key': REMOTE, 'extension_id': EXTENSION,
              'allowed_account_ids': ('default',)}
    values.update(overrides)
    return RelayConfig(**values)


def headers(**extra):
    return {'X-Chrome-Bridge-Key': LOCAL, 'Origin': ORIGIN, **extra}


@pytest.mark.parametrize('url', ['http://public.example', 'https://user:pass@example.test',
                               'https://gateway.example/?secret=x', 'https://gateway.example/#fragment',
                               'https://gateway.example/../target', 'http://127.0.0.1:8787'])
def test_config_rejects_unsafe_or_recursive_urls(url):
    with pytest.raises(ValueError):
        config(remote_api_url=url)


def test_loopback_development_remote_can_use_a_different_port():
    assert config(remote_api_url='http://127.0.0.1:8788').remote_api_url.endswith('8788')


def test_local_and_remote_keys_are_independent():
    with pytest.raises(ValueError, match='must be different'):
        config(remote_bridge_key=LOCAL)


def test_worker_config_only_loads_protocol_fields(tmp_path, monkeypatch):
    for name in ('REMOTE_API_URL', 'LOCAL_BRIDGE_KEY', 'REMOTE_BRIDGE_KEY', 'CHROME_BRIDGE_EXTENSION_ID', 'ALLOWED_ACCOUNT_IDS'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / 'worker.env'
    path.write_text(f'REMOTE_API_URL=https://gateway.example\nLOCAL_BRIDGE_KEY={LOCAL}\nREMOTE_BRIDGE_KEY={REMOTE}\nCHROME_BRIDGE_EXTENSION_ID={EXTENSION}\nALLOWED_ACCOUNT_IDS=default,second\nIGNORED_FIELD=not-used\n', encoding='utf-8')
    loaded = RelayConfig.load(path)
    assert loaded.allowed_account_ids == ('default', 'second')
    assert 'not-used' not in repr(loaded)
    assert LOCAL not in repr(loaded)


def test_heartbeat_replaces_key_and_does_not_forward_account_cookie():
    captured = []
    async def handler(request):
        captured.append(request)
        assert str(request.url) == 'https://gateway.example/v1/chrome/heartbeat'
        assert request.headers['x-chrome-bridge-key'] == REMOTE
        assert 'cookie' not in request.headers
        assert 'authorization' not in request.headers
        payload = json.loads(request.content)
        assert payload['page_url'] == 'https://dreamina.capcut.com/ai-tool/generate'
        return httpx.Response(200, json={'ok': True})
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        result = client.post('/v1/chrome/heartbeat', headers=headers(Cookie='sessionid=do-not-forward', Authorization='Bearer do-not-forward'),
                             json={'account_id': 'default', 'worker_id': 'worker-one', 'ready': True,
                                   'page_url': 'https://dreamina.capcut.com/ai-tool/generate?private-query=excluded'})
    assert result.status_code == 200
    assert result.headers['access-control-allow-origin'] == ORIGIN
    assert len(captured) == 1


def test_unknown_account_rejected_before_network():
    def handler(request):
        raise AssertionError('Should not reach remote')
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        response = client.post('/v1/chrome/claim', headers=headers(), json={'account_id': 'other', 'worker_id': 'worker'})
    assert response.status_code == 403


def test_unauthorized_and_wrong_origin_are_rejected():
    def handler(request):
        raise AssertionError('Should not reach remote')
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        assert client.post('/v1/chrome/claim', json={'account_id': 'default', 'worker_id': 'worker'}).status_code == 401
        assert client.post('/v1/chrome/claim', headers=headers(Origin='https://evil.example'), json={'account_id': 'default', 'worker_id': 'worker'}).status_code == 403
        assert client.get('/health').status_code == 200


def test_cors_is_exact_and_does_not_allow_credential_header_passthrough():
    remote = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        response = client.options('/v1/chrome/claim', headers={'Origin': ORIGIN,
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-chrome-bridge-key'})
        assert response.status_code == 204
        assert response.headers['access-control-allow-origin'] == ORIGIN
        assert 'access-control-allow-credentials' not in response.headers
        bad = client.options('/v1/chrome/claim', headers={'Origin': ORIGIN,
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'cookie'})
        assert bad.status_code == 403


def test_claim_timeout_is_uncertain_and_is_never_retried():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('sensitive remote URL should not be exposed', request=request)
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        response = client.post('/v1/chrome/claim', headers=headers(), json={'account_id': 'default', 'worker_id': 'worker'})
    assert response.status_code == 504
    assert response.json()['error']['code'] == 'claim_outcome_unknown'
    assert len(calls) == 1
    assert 'sensitive remote URL' not in response.text


def test_remote_redirect_is_not_followed():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={'Location': 'https://another.example/steal'})
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        response = client.post('/v1/chrome/claim', headers=headers(), json={'account_id': 'default', 'worker_id': 'worker'})
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'remote_redirect_rejected'
    assert len(calls) == 1


def test_report_forwards_exact_job_and_claim_token():
    job_id = str(uuid.uuid4())
    def handler(request):
        assert request.url.path == f'/v1/chrome/jobs/{job_id}/report'
        payload = json.loads(request.content)
        assert payload['claim_token'] == 'job-scoped-token'
        assert payload['submit_id'] == str(uuid.UUID(int=1))
        return httpx.Response(200, json={'id': job_id, 'status': 'submitted'})
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        response = client.post(f'/v1/chrome/jobs/{job_id}/report', headers=headers(), json={
            'worker_id': 'worker', 'claim_token': 'job-scoped-token', 'status': 'submitted',
            'submit_id': str(uuid.UUID(int=1))})
    assert response.status_code == 200


def test_non_bridge_endpoints_cannot_be_relayed():
    def handler(request):
        raise AssertionError('Should not reach remote')
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        for path in ('/api/admin/keys', '/v1/videos', '/v1/chrome/jobs', '/proxy'):
            assert client.post(path, headers=headers(), json={}).status_code == 404


def test_remote_error_cannot_echo_bridge_keys():
    remote = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403, json={'error': {'message': LOCAL + REMOTE}})))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        response = client.post('/v1/chrome/claim', headers=headers(), json={'account_id': 'default', 'worker_id': 'worker'})
    assert response.status_code == 403
    assert LOCAL not in response.text and REMOTE not in response.text


def test_invalid_request_does_not_echo_values_or_forward():
    def handler(request):
        raise AssertionError('Should not reach remote')
    remote = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_relay_app(config(), http=remote)) as client:
        response = client.post('/v1/chrome/claim', headers=headers(), json={'account_id': 'default', 'worker_id': 'worker', 'cookie': 'private-value'})
    assert response.status_code == 422 and 'private-value' not in response.text
