import asyncio
import json
from pathlib import Path

import httpx
import pytest

from dreamina_api.client import DreaminaClient, DreaminaError, SubmissionUncertain, make_sign, normalize_record
from dreamina_api.config import Settings


def test_sign_matches_captured_headers_when_private_capture_present():
    capture = Path(__file__).resolve().parents[1] / 'evidence' / 'dreamina.capcut.com.har'
    if not capture.exists():
        pytest.skip('Private capture is intentionally not distributed')
    from urllib.parse import urlsplit
    entries = json.loads(capture.read_text(encoding='utf-8-sig'))['log']['entries']
    matched = 0
    for entry in entries:
        headers = {h['name'].lower(): h['value'] for h in entry['request']['headers']}
        if 'sign' not in headers:
            continue
        path = urlsplit(entry['request']['url']).path
        assert make_sign(path, headers['device-time'], appvr=headers['appvr'], pf=headers['pf']) == headers['sign']
        matched += 1
    assert matched > 5


def test_poll_uses_submit_id_and_preserves_video_duration_ms():
    async def run():
        def handler(request):
            assert json.loads(request.content) == {'submit_ids': ['test-submit']}
            assert request.headers['cookie'].startswith('sessionid=test-session;')
            return httpx.Response(200, json={'ret': '0', 'data': {'test-submit': {
                'status': 50, 'item_list': [{'video': {'duration': 5, 'duration_ms': 4042,
                    'transcoded_video': {'origin': {'video_url': 'https://media.example/video.mp4', 'width': 1280, 'height': 720}}}}]}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with DreaminaClient(Settings(session_id='test-session'), http=http) as client:
                result = await client.poll('test-submit')
                assert result['status'] == 'completed'
                assert result['videos'][0]['duration_ms'] == 4042
                assert 'cookie' not in http.headers
    asyncio.run(run())


def test_upstream_rejection_is_not_retried():
    async def run():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'ret': '-6', 'errmsg': 'shark not pass reject'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='test-session'), http=http)
            with pytest.raises(DreaminaError, match='shark not pass reject') as err:
                await client.request('/mweb/v1/aigc_draft/generate', {'submit_id': 'once'})
            assert err.value.code == 'upstream_-6'
            assert len(calls) == 1
    asyncio.run(run())


def test_timeout_preserves_submission_id_without_retry():
    async def run():
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('timed out', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='test-session'), http=http)
            with pytest.raises(SubmissionUncertain) as err:
                await client.request('/mweb/v1/aigc_draft/generate', {'submit_id': 'once'})
            assert err.value.submit_id == 'once'
            assert len(calls) == 1
    asyncio.run(run())


def test_completed_without_media_does_not_claim_download_ready():
    assert normalize_record({'status': 50, 'item_list': []}, 'test')['status'] == 'processing'
    assert normalize_record({'status': 987}, 'test')['status'] == 'unknown'
    assert normalize_record({'status': 30}, 'test')['status'] == 'failed'
    assert normalize_record({'status': 10}, 'test')['status'] == 'failed'
    assert normalize_record({'status': 40}, 'test')['status'] == 'failed'
    assert normalize_record({'status': 100}, 'test')['status'] == 'deleted'


def test_media_download_never_sends_session_cookie(tmp_path):
    async def run():
        def handler(request):
            if request.url.host == 'dreamina-api.us.capcut.com':
                return httpx.Response(200, json={'ret': '0', 'data': {'test': {'status': 50,
                    'item_list': [{'video': {'transcoded_video': {'origin': {'video_url': 'https://media.example/test.mp4'}}}}]}}})
            assert 'cookie' not in request.headers
            assert 'authorization' not in request.headers
            return httpx.Response(200, content=b'video-content')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='test-session'), http=http)
            path = await client.download('test', tmp_path / 'test.mp4')
            assert path.read_bytes() == b'video-content'
    asyncio.run(run())


def test_missing_credentials_does_not_make_network_request():
    async def run():
        def handler(request):
            raise AssertionError('No request should be sent')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(), http=http)
            with pytest.raises(DreaminaError) as err:
                await client.credits()
            assert err.value.code == 'missing_session'
    asyncio.run(run())
