"""Offline regressions for submission identity, state handling and preflight.

All HTTP is handled by MockTransport. Credentials, paths and IDs are synthetic;
receipts are written only beneath pytest's temporary directory.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dreamina_api import service
from dreamina_api.client import DreaminaClient, DreaminaError, SubmissionUncertain
from dreamina_api.config import Settings
from dreamina_api.payloads import build_generation


@pytest.fixture
def receipt_root(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'ROOT', tmp_path)
    return tmp_path / 'outputs' / 'tasks'


def receipts(folder):
    return [json.loads(path.read_text(encoding='utf-8')) for path in folder.glob('*.json')]


@pytest.mark.parametrize('response_body', [
    {'ret': '0', 'data': {}},
    {'ret': '0', 'data': {'aigc_data': {}}},
    {'ret': '0', 'data': None},
    {'ret': '0', 'data': []},
    {'ret': '0', 'data': {'aigc_data': [1]}},
    [],
])
def test_ambiguous_submission_preserves_id_receipt_and_never_retries(response_body, receipt_root):
    async def run():
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=response_body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='synthetic-session', workspace_id='123'), http=http)
            with pytest.raises(SubmissionUncertain) as caught:
                await service.create_video(client, {'prompt': 'Synthetic test prompt'})
        assert len(calls) == 1
        assert caught.value.submit_id == calls[0]['submit_id']
        stored = receipts(receipt_root)
        assert len(stored) == 1
        assert stored[0]['status'] == 'uncertain'
        assert stored[0]['id'] == caught.value.submit_id
        assert stored[0]['error'] == 'submission_uncertain'
    asyncio.run(run())


def test_definite_rejection_is_recorded_as_rejected_without_retry(receipt_root):
    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'ret': '-6', 'errmsg': 'synthetic rejection'})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='synthetic-session'), http=http)
            with pytest.raises(DreaminaError) as caught:
                await service.create_video(client, {'prompt': 'Test'})
        assert not isinstance(caught.value, SubmissionUncertain)
        assert len(calls) == 1
        assert receipts(receipt_root)[0]['status'] == 'rejected'
    asyncio.run(run())


def test_poll_failure_returns_accepted_task_identity_and_durable_receipt(receipt_root):
    async def run():
        calls = []
        submit_id = None

        def handler(request):
            nonlocal submit_id
            calls.append(request.url.path)
            if request.url.path.endswith('/aigc_draft/generate'):
                submit_id = json.loads(request.content)['submit_id']
                return httpx.Response(200, json={'ret': '0', 'data': {'aigc_data': {'status': 20}}})
            assert json.loads(request.content) == {'submit_ids': [submit_id]}
            raise httpx.ReadTimeout('synthetic poll timeout', request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='synthetic-session'), http=http)
            result = await service.create_video(client, {'prompt': 'Test'}, wait_seconds=30)
        assert result['id'] == submit_id
        assert result['resume_with_submit_id'] == submit_id
        assert result['status'] == 'processing'
        assert result['poll_error']['code'] == 'network_error'
        assert calls.count('/mweb/v1/aigc_draft/generate') == 1
        assert len(calls) == 2
        assert receipts(receipt_root) == [result]
    asyncio.run(run())


@pytest.mark.parametrize(('upstream_status', 'state'), [(10, 'failed'), (30, 'failed'), (40, 'failed'), (100, 'deleted')])
def test_terminal_upstream_states_stop_wait_without_sleep_or_second_poll(upstream_status, state, monkeypatch):
    sleep = AsyncMock(side_effect=AssertionError('Terminal records must not keep polling'))
    monkeypatch.setattr(asyncio, 'sleep', sleep)

    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'ret': '0', 'data': {'synthetic-task': {
                'status': upstream_status, 'fail_code': 'synthetic-failure', 'fail_starling_key': 'synthetic-message',
            }}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='synthetic-session'), http=http)
            result = await client.wait('synthetic-task', timeout=900)
        assert result['status'] == state
        assert result['upstream_status'] == upstream_status
        if state == 'failed':
            assert result['failure']['code'] == 'synthetic-failure'
        assert len(calls) == 1
        sleep.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('region', ['US', 'SG', 'JP', 'HK'])
def test_generation_query_and_headers_follow_configured_region(region):
    async def run():
        calls = []

        def handler(request):
            calls.append(request)
            assert request.url.params['region'] == region
            assert request.headers['loc'] == region
            assert request.headers['store-country-code'] == region.lower()
            expected_host = 'dreamina-api.us.capcut.com' if region == 'US' else 'mweb-api-sg.capcut.com'
            assert request.url.host == expected_host
            return httpx.Response(200, json={'ret': '0', 'data': {'aigc_data': {'status': 20}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DreaminaClient(Settings(session_id='synthetic-session', region=region), http=http)
            prepared = build_generation({'prompt': 'Test'})
            # Guard specifically against the prepared request's captured US default.
            prepared['query']['region'] = 'US'
            result = await client.submit(prepared)
        assert result['id'] == prepared['submit_id']
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('invalid_fields', [
    {'duration': -1},
    {'model': 'nonexistent-model'},
    {'mode': 'frames'},
    {'references_count': 10},
])
def test_invalid_request_is_rejected_before_upload_or_submit(invalid_fields, tmp_path, monkeypatch, receipt_root):
    asset = tmp_path / 'synthetic-image.png'
    asset.write_bytes(b'not uploaded by this test')
    spec = {'prompt': 'Test', 'references': [str(asset)]}
    invalid_fields = dict(invalid_fields)
    count = invalid_fields.pop('references_count', None)
    if count is not None:
        spec['references'] = [str(asset)] * count
    spec.update(invalid_fields)
    resolver = AsyncMock(side_effect=AssertionError('No asset may be uploaded before preflight succeeds'))
    monkeypatch.setattr(service, 'resolve_assets', resolver)
    client = SimpleNamespace(settings=Settings(), submit=AsyncMock())

    async def run():
        with pytest.raises(ValueError):
            await service.create_video(client, spec)
    asyncio.run(run())
    resolver.assert_not_awaited()
    client.submit.assert_not_awaited()
    assert not receipt_root.exists()


def test_uploaded_resource_metadata_is_revalidated_before_generation(tmp_path, monkeypatch, receipt_root):
    asset = tmp_path / 'synthetic-image.png'
    asset.write_bytes(b'local placeholder')
    # Preflight knows the path exists; actual upload metadata then reveals an
    # image that violates Seedance 2.5's minimum dimensions.
    resolver = AsyncMock(return_value={
        'prompt': 'Test', 'model': 'seedance-2.5',
        'references': [{'type': 'image', 'image_uri': 'synthetic/test.png', 'width': 100, 'height': 100}],
    })
    monkeypatch.setattr(service, 'resolve_assets', resolver)
    client = SimpleNamespace(settings=Settings(), submit=AsyncMock())

    async def run():
        with pytest.raises(ValueError, match='min_width'):
            await service.create_video(client, {'prompt': 'Test', 'model': 'seedance-2.5', 'references': [str(asset)]})
    asyncio.run(run())
    resolver.assert_awaited_once()
    client.submit.assert_not_awaited()
    assert not receipt_root.exists()
