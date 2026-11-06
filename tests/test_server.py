"""In-process API regressions; never reads real settings or contacts upstream."""

import asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from dreamina_api import server
from dreamina_api.client import SubmissionUncertain
from dreamina_api.config import Settings


@pytest.fixture(autouse=True)
def synthetic_settings(monkeypatch):
    def load(cls):
        return Settings(session_id='synthetic-account-a', cookie='sessionid=synthetic-account-a',
                        workspace_id='111', api_key='synthetic-local-key')
    monkeypatch.setattr(Settings, 'load', classmethod(load))
    monkeypatch.setattr(server.platform_store,'settings_for_account',lambda aid:Settings(
        session_id='synthetic-'+aid,workspace_id='111' if aid=='account-a' else '',account_id=aid))


AUTH = {'Authorization': 'Bearer synthetic-local-key'}


def test_request_session_override_clears_old_cookie_and_workspace():
    request = Request({'type': 'http', 'path':'/v1/chrome/jobs', 'headers': [
        (b'authorization', b'Bearer synthetic-local-key'),
        (b'x-dreamina-session', b'synthetic-account-b'),
    ]})
    config = server.authorize(request)
    assert config.session_id == 'synthetic-account-b'
    assert config.cookie == ''
    assert config.workspace_id == ''


def test_internal_requests_require_managed_account_and_refuse_session_override():
    with TestClient(server.app) as api:
        missing = api.post('/internal/videos/prepare', headers=AUTH,json={'prompt':'Test'})
        assert missing.status_code==400
        headers={**AUTH,'X-Dreamina-Account':'account-a'}
        original = api.post('/internal/videos/prepare', headers=headers, json={'prompt': 'Test'})
        assert original.status_code == 200
        assert original.json()['body']['extend']['workspace_id'] == 111
        override = api.post('/internal/videos/prepare', headers={**headers, 'X-Dreamina-Session':'synthetic-account-b'},json={'prompt':'Test'})
        assert override.status_code==400
        changed = api.post('/internal/videos/prepare', headers={**AUTH, 'X-Dreamina-Account': 'account-b'}, json={'prompt': 'Test'})
        assert changed.status_code == 200
        assert 'workspace_id' not in changed.json()['body']['extend']
        explicit = api.post('/internal/videos/prepare', headers={**AUTH, 'X-Dreamina-Account': 'account-b'},
                            json={'prompt': 'Test', 'workspace_id': '222'})
        assert explicit.status_code == 200
        assert explicit.json()['body']['extend']['workspace_id'] == 222
        # Request-scoped overrides must not alter the following caller's settings.
        original_again = api.post('/internal/videos/prepare', headers=headers, json={'prompt': 'Test'})
        assert original_again.json()['body']['extend']['workspace_id'] == 111


def test_uncertain_submission_response_exposes_recoverable_submit_id(monkeypatch):
    class Context:
        def __init__(self, settings):
            self.settings = settings
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    generate = AsyncMock(side_effect=SubmissionUncertain('synthetic-accepted-task'))
    monkeypatch.setattr(server, 'DreaminaClient', Context)
    monkeypatch.setattr(server, 'create_video', generate)
    with TestClient(server.app) as api:
        response = api.post('/internal/videos/generations', headers={**AUTH,'X-Dreamina-Account':'account-a'}, json={'prompt': 'Test'})
    assert response.status_code == 504
    assert response.json()['submit_id'] == 'synthetic-accepted-task'
    assert response.json()['error']['code'] == 'submission_uncertain'
    assert generate.await_count == 1


@pytest.mark.parametrize('headers', [
    {},
    {'Authorization': 'Bearer wrong-key', 'X-Dreamina-Session': 'synthetic-account-b'},
    {**AUTH, 'Origin': 'https://unrelated.example'},
])
def test_authorization_blocks_requests_before_generation(headers, monkeypatch):
    generate = AsyncMock(side_effect=AssertionError('Rejected callers must never generate'))
    monkeypatch.setattr(server, 'create_video', generate)
    with TestClient(server.app) as api:
        response = api.post('/internal/videos/generations', headers=headers, json={'prompt': 'Test'})
    assert response.status_code in (401, 403)
    generate.assert_not_awaited()
