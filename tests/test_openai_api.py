import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from dreamina_api.openai_api import create_openai_router, augment_openapi
from dreamina_api.platform_store import PlatformStore


class FakeBridge:
    def __init__(self):
        self.calls = []

    def enqueue(self, spec, *, account_id, job_id=None):
        self.calls.append({'spec': spec, 'account_id': account_id})
        return {'id': job_id or str(uuid.uuid4()), 'status': 'queued'}


def setup(tmp_path, **options):
    store = PlatformStore('sqlite:///:memory:', encryption_key=Fernet.generate_key())
    store.create_account({'id': 'test-account', 'name': 'Test', 'session_id': 'test-session',
                          'max_concurrent': 8, 'max_inflight': 8, 'capabilities': ['seedance-2.0-mini', 'seedream-5.0-lite']})
    created = store.create_key({'name': 'Test caller', 'max_concurrent': 20})
    bridge = FakeBridge()
    app = FastAPI()
    app.include_router(create_openai_router(store, bridge, media_directory=tmp_path, **options))
    return SimpleNamespace(store=store, bridge=bridge, client=TestClient(app),
                           token=created['key'], key_id=created['item']['id'],
                           headers={'Authorization': 'Bearer ' + created['key']})


def video_request(**changes):
    return {'model': 'seedance-2.0-mini', 'prompt': 'Clouds drift across a blue sky.',
            'seconds': '4', 'size': '1280x720', **changes}


def image_request(**changes):
    return {'model': 'seedream-5.0-lite', 'prompt': 'A blue ceramic vase.',
            'n': 1, 'size': '2048x2048', **changes}


def test_bearer_required_on_models_and_create(tmp_path):
    env = setup(tmp_path)
    for method, path in [('get', '/v1/models'), ('post', '/v1/videos')]:
        response = getattr(env.client, method)(path)
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'invalid_api_key'
        assert response.json()['error']['type'] == 'authentication_error'


def test_video_json_protocol_and_idempotent_dispatch(tmp_path):
    env = setup(tmp_path)
    headers = dict(env.headers, **{'Idempotency-Key': 'same-paid-job'})
    first = env.client.post('/v1/videos', json=video_request(), headers=headers)
    second = env.client.post('/v1/videos', json=video_request(), headers=headers)
    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body['id'].startswith('video_')
    assert body['object'] == 'video'
    assert body['status'] == 'queued'
    assert body['seconds'] == '4'
    assert body['size'] == '1280x720'
    assert first.headers['x-request-id'] == body['id']
    assert second.json()['id'] == body['id']
    assert len(env.bridge.calls) == 1
    assert env.bridge.calls[0] == {'account_id': 'test-account', 'spec': {
        'model': 'seedance-2.0-mini', 'prompt': video_request()['prompt'], 'duration': 4,
        'ratio': '16:9', 'resolution': '720p', 'mode': 'omni','references':[],'asset_owner_task_id':body['id'],'new_session':True}}
    conflict = env.client.post('/v1/videos', json=video_request(prompt='Different prompt'), headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()['error']['code'] == 'idempotency_conflict'
    assert len(env.bridge.calls) == 1


def test_video_multipart_protocol(tmp_path):
    env = setup(tmp_path)
    response = env.client.post('/v1/videos', files={key: (None, value) for key, value in video_request().items()}, headers=env.headers)
    assert response.status_code == 200
    assert response.json()['seconds'] == '4'
    assert len(env.bridge.calls) == 1


def test_public_prompt_whitespace_normalized_before_dispatch(tmp_path):
    calls = []
    async def provider(spec, settings, *, task):
        calls.append(spec['prompt'])
        return {'images': [{'url': 'https://cdn.example/image.jpeg'}]}
    env = setup(tmp_path, image_generate=provider)
    video = env.client.post('/v1/videos', json=video_request(prompt='  Clouds drift.\n '), headers=env.headers)
    assert video.status_code == 200
    assert env.bridge.calls[0]['spec']['prompt'] == 'Clouds drift.'
    image = env.client.post('/v1/images/generations', json=image_request(prompt='  A blue vase.\n '), headers=env.headers)
    assert image.status_code == 200
    assert calls == ['A blue vase.']


@pytest.mark.parametrize('kind,prompt', [('image', 'x' * 2001), ('image', '😀' * 1001),
                                        ('video', '😀' * 2001), ('image', '\ud800')])
def test_prompt_limits_reject_before_reservation_and_dispatch(tmp_path, kind, prompt):
    calls = []
    async def provider(spec, settings, *, task):
        calls.append(task['id'])
        raise AssertionError('Invalid prompt must not reach the provider')
    env = setup(tmp_path, image_generate=provider)
    request = image_request(prompt=prompt) if kind == 'image' else video_request(prompt=prompt)
    endpoint = '/v1/images/generations' if kind == 'image' else '/v1/videos'
    # Escape the lone surrogate exactly as it can arrive in valid JSON syntax.
    response = env.client.post(endpoint, content=json.dumps(request).encode('ascii'),
                               headers=dict(env.headers, **{'Content-Type': 'application/json'}))
    assert response.status_code == 400
    assert response.json()['error']['param'] == 'prompt'
    assert env.store.list_tasks() == []
    assert env.store.list_keys()[0]['used_requests'] == 0
    assert env.bridge.calls == []
    assert calls == []


@pytest.mark.parametrize('change,status,code', [
    ({'seconds': '99'}, 400, 'invalid_video_parameters'),
    ({'size': '11x11'}, 400, 'invalid_video_parameters'),
    ({'input_reference': {'image_url': 'https://example.invalid/image.png'}}, 400, 'invalid_video_parameters'),
    ({'model': 'sora-2'}, 404, 'model_not_found'),
    ({'extra_unrecognized': True}, 400, 'unsupported_parameter'),
])
def test_unsupported_video_never_dispatches(tmp_path, change, status, code):
    env = setup(tmp_path)
    response = env.client.post('/v1/videos', json=video_request(**change), headers=env.headers)
    assert response.status_code == status
    assert response.json()['error']['code'] == code
    assert env.bridge.calls == []
    assert env.store.list_tasks() == []


def test_key_ownership_protects_retrieve_and_content(tmp_path):
    downloads = []
    async def download(task):
        downloads.append(task)
        return b'mp4'
    env = setup(tmp_path, download_video=download)
    job_id = env.client.post('/v1/videos', json=video_request(), headers=env.headers).json()['id']
    other = env.store.create_key({'name': 'Other user'})['key']
    wrong_headers = {'Authorization': 'Bearer ' + other}
    for path in (f'/v1/videos/{job_id}', f'/v1/videos/{job_id}/content'):
        response = env.client.get(path, headers=wrong_headers)
        assert response.status_code == 404
    assert downloads == []
    assert env.client.get('/v1/videos', headers=wrong_headers).json()['data'] == []


def test_video_status_sync_and_download(tmp_path):
    state = {}
    async def sync(task):
        return state['store'].update_task(task['id'], status='completed', result={'videos': [{'url': 'https://cdn.example/video.mp4'}]})
    async def download(task):
        assert task['status'] == 'completed'
        return b'\x00\x00\x00\x18ftypmp42example'
    env = setup(tmp_path, sync_task=sync, download_video=download)
    state['store'] = env.store
    job_id = env.client.post('/v1/videos', json=video_request(), headers=env.headers).json()['id']
    result = env.client.get(f'/v1/videos/{job_id}', headers=env.headers)
    assert result.json()['status'] == 'completed'
    assert result.json()['progress'] == 100
    response = env.client.get(f'/v1/videos/{job_id}/content', headers=env.headers)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'video/mp4'
    assert response.content[4:8] == b'ftyp'


def test_completed_video_reports_downloaded_origin_size_without_changing_request_tier(tmp_path):
    state = {}
    async def sync(task):
        return state['store'].update_task(task['id'],status='completed',result={'videos':[
            {'url':'https://cdn.example/origin.mp4','width':1470,'height':630},
            {'url':'https://cdn.example/other.mp4','width':1920,'height':1080},
        ]})
    env=setup(tmp_path,sync_task=sync);state['store']=env.store
    created=env.client.post('/v1/videos',json=video_request(size='1680x720'),headers=env.headers)
    assert created.status_code==200 and created.json()['size']=='1680x720'
    task_id=created.json()['id']
    response=env.client.get('/v1/videos/'+task_id,headers=env.headers)
    assert response.status_code==200
    assert response.json()['status']=='completed'
    assert response.json()['size']=='1470x630'
    assert response.json()['resolution']=='720p'
    assert env.store.get_task(task_id)['request']['size']=='1680x720'
    assert env.client.get('/v1/videos',headers=env.headers).json()['data'][0]['size']=='1470x630'
    assert len(env.bridge.calls)==1


def test_uncertain_is_explicit_and_not_redispatched(tmp_path):
    env = setup(tmp_path)
    headers = dict(env.headers, **{'Idempotency-Key': 'uncertain-job'})
    job_id = env.client.post('/v1/videos', json=video_request(), headers=headers).json()['id']
    env.store.update_task(job_id, status='uncertain')
    response = env.client.post('/v1/videos', json=video_request(), headers=headers)
    assert response.json()['status'] == 'failed'
    assert response.json()['metadata']['verification_required'] is True
    assert len(env.bridge.calls) == 1


def test_lost_bridge_enqueue_response_preserves_id_and_no_retry(tmp_path):
    env = setup(tmp_path)
    calls = []
    def lost_response(spec, *, account_id, job_id=None):
        calls.append(spec)
        raise RuntimeError('Lost queue response')
    env.bridge.enqueue = lost_response
    headers = dict(env.headers, **{'Idempotency-Key': 'lost-queue-response'})
    response = env.client.post('/v1/videos', json=video_request(), headers=headers)
    assert response.status_code == 409
    task_id = response.json()['task_id']
    assert response.json()['error']['code'] == 'dispatch_uncertain'
    replay = env.client.post('/v1/videos', json=video_request(), headers=headers)
    assert replay.json()['id'] == task_id
    assert replay.json()['metadata']['verification_required'] is True
    assert len(calls) == 1


def test_image_sync_url_response_and_idempotent_replay(tmp_path):
    calls = []
    async def provider(spec, settings, *, task):
        calls.append(task['id'])
        assert settings.session_id == 'test-session'
        assert spec['size'] == '2048x2048'
        return {'images': [{'url': 'https://cdn.example/image.jpeg', 'revised_prompt': spec['prompt']}], 'output_format': 'jpeg'}
    env = setup(tmp_path, image_generate=provider)
    headers = dict(env.headers, **{'Idempotency-Key': 'image-once'})
    response = env.client.post('/v1/images/generations', json=image_request(), headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json()['created'], int)
    assert response.json()['data'][0]['url'] == 'https://cdn.example/image.jpeg'
    assert response.json()['output_format'] == 'jpeg'
    again = env.client.post('/v1/images/generations', json=image_request(), headers=headers)
    assert again.json() == response.json()
    assert len(calls) == 1


def test_image_base64_cached_outside_database(tmp_path):
    binary = b'\xff\xd8\xff' + b'x' * 4_000_000
    async def provider(spec, settings, *, task):
        return {'images': [{'b64_json': base64.b64encode(binary).decode(), 'content_type': 'image/jpeg'}], 'output_format': 'jpeg'}
    env = setup(tmp_path, image_generate=provider)
    response = env.client.post('/v1/images/generations', json=image_request(response_format='b64_json'), headers=env.headers)
    assert response.status_code == 200
    assert base64.b64decode(response.json()['data'][0]['b64_json']) == binary
    task = env.store.list_tasks()[0]
    assert 'b64_json' not in task['result']['images'][0]
    assert task['result']['images'][0]['_local_image'] == 'generated-0.bin'
    assert (tmp_path / task['id'] / 'generated-0.bin').read_bytes() == binary
    restored = env.client.get(f'/v1/images/jobs/{task["id"]}/result', headers=env.headers)
    assert restored.json() == response.json()


def test_b64_conversion_uses_injected_safe_downloader(tmp_path):
    downloads = []
    async def provider(spec, settings, *, task):
        return {'images': [{'url': 'https://cdn.example/image.jpg'}]}
    async def download(image, settings):
        downloads.append(image['url'])
        return b'image-data'
    env = setup(tmp_path, image_generate=provider, image_download=download)
    response = env.client.post('/v1/images/generations', json=image_request(response_format='b64_json'), headers=env.headers)
    assert response.status_code == 200
    assert base64.b64decode(response.json()['data'][0]['b64_json']) == b'image-data'
    assert downloads == ['https://cdn.example/image.jpg']
    task_id = response.headers['x-request-id']
    result = env.store.get_task(task_id)['result']
    assert result['images'][0]['_local_image'] == 'generated-0.bin'
    assert 'b64_json' not in result['images'][0]
    assert (tmp_path / task_id / 'generated-0.bin').read_bytes() == b'image-data'
    restored = env.client.get(f'/v1/images/jobs/{task_id}/result', headers=env.headers)
    assert restored.status_code == 200
    assert restored.json() == response.json()
    assert downloads == ['https://cdn.example/image.jpg']


def test_cached_base64_result_survives_cdn_unavailability(tmp_path):
    calls = []
    async def provider(spec, settings, *, task):
        return {'images': [{'url': 'https://cdn.example/temporary-signed-image.jpg'}]}
    async def download(image, settings):
        calls.append(image['url'])
        if len(calls) > 1:
            raise RuntimeError('Original signed media URL expired')
        return b'persisted-image'
    env = setup(tmp_path, image_generate=provider, image_download=download)
    response = env.client.post('/v1/images/generations', json=image_request(response_format='b64_json'), headers=env.headers)
    assert response.status_code == 200
    task_id = response.headers['x-request-id']
    restored = env.client.get(f'/v1/images/jobs/{task_id}/result', headers=env.headers)
    assert restored.status_code == 200
    assert base64.b64decode(restored.json()['data'][0]['b64_json']) == b'persisted-image'
    assert len(calls) == 1


def test_completed_image_download_failure_returns_resumable_id_without_regenerating(tmp_path):
    generations, downloads = [], []
    async def provider(spec, settings, *, task):
        generations.append(task['id'])
        return {'images': [{'url': 'https://cdn.example/image.jpg'}]}
    async def download(image, settings):
        downloads.append(image['url'])
        if len(downloads) == 1:
            raise RuntimeError('upstream https://cdn.example/?token=do-not-leak')
        return b'confirmed-image-data'
    env = setup(tmp_path, image_generate=provider, image_download=download)
    response = env.client.post('/v1/images/generations', json=image_request(response_format='b64_json'), headers=env.headers)
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'result_unavailable'
    task_id = response.json()['task_id']
    assert response.headers['x-request-id'] == task_id
    assert 'do-not-leak' not in response.text
    assert env.store.get_task(task_id)['status'] == 'completed'
    recovered = env.client.get(f'/v1/images/jobs/{task_id}/result', headers=env.headers)
    assert recovered.status_code == 200
    assert base64.b64decode(recovered.json()['data'][0]['b64_json']) == b'confirmed-image-data'
    assert len(generations) == 1


def test_image_edit_unavailable_is_not_fake_success_or_charged(tmp_path):
    async def provider(spec, settings, *, task):
        raise AssertionError('Unavailable edit must not call provider')
    env = setup(tmp_path, image_generate=provider)
    response = env.client.post('/v1/images/edits', data={'prompt': 'Edit image'},
                               files={'image': ('image.png', b'png', 'image/png')}, headers=env.headers)
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'capability_unavailable'
    assert env.store.list_tasks() == []


def test_enabled_edit_keeps_uploaded_file_out_of_task_json(tmp_path):
    calls = []
    async def provider(spec, settings, *, task):
        calls.append(spec)
        assert Path(spec['input_images'][0]).read_bytes() == b'fixture-png'
        assert task['request']['input_images'][0]['sha256']
        return {'images': [{'url': 'https://cdn.example/edited.png'}]}
    models = [{'id': 'seedream-5.0-lite', 'kind': 'image', 'enabled': True,
               'capabilities': {'sizes': ['2048x2048'], 'n': [1], 'edits': True}}]
    env = setup(tmp_path, image_generate=provider, model_catalog=models)
    response = env.client.post('/v1/images/edits', data={'prompt': 'Edit image'},
                               files={'image': ('../../escape.png', b'fixture-png', 'image/png')}, headers=env.headers)
    assert response.status_code == 200
    assert len(calls) == 1
    assert Path(calls[0]['input_images'][0]).name == '0.png'
    assert 'fixture-png' not in json.dumps(env.store.list_tasks()[0]['request'])


def test_image_timeout_is_recoverable_and_never_resubmits(tmp_path):
    calls = []
    async def provider(spec, settings, *, task):
        calls.append(task['id'])
        await asyncio.sleep(1)
    env = setup(tmp_path, image_generate=provider, image_timeout=0.001)
    headers = dict(env.headers, **{'Idempotency-Key': 'slow-image'})
    response = env.client.post('/v1/images/generations', json=image_request(), headers=headers)
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'generation_timeout'
    task_id = response.json()['task_id']
    assert env.client.get(f'/v1/images/jobs/{task_id}', headers=env.headers).json()['status'] == 'uncertain'
    repeat = env.client.post('/v1/images/generations', json=image_request(), headers=headers)
    assert repeat.status_code == 409
    assert repeat.json()['task_id'] == task_id
    assert len(calls) == 1


@pytest.mark.parametrize('changes,param', [({'n': 2}, 'n'), ({'stream': True}, 'stream'),
                                         ({'size': '1024x1024'}, 'size'), ({'quality': 'hd'}, 'quality'),
                                         ({'output_format': 'png'}, 'output_format')])
def test_unsupported_image_options_are_not_silently_ignored(tmp_path, changes, param):
    async def provider(*args, **kwargs):
        raise AssertionError('Invalid request reached provider')
    env = setup(tmp_path, image_generate=provider)
    response = env.client.post('/v1/images/generations', json=image_request(**changes), headers=env.headers)
    assert response.status_code == 400
    assert response.json()['error']['param'] == param
    assert env.store.list_tasks() == []


def test_models_only_show_enabled_key_permitted_capabilities(tmp_path):
    env = setup(tmp_path, image_generate=lambda *args: None)
    env.store.update_key(env.key_id, {'allowed_models': ['seedance-2.0-mini']})
    response = env.client.get('/v1/models', headers=env.headers)
    assert [x['id'] for x in response.json()['data']] == ['seedance-2.0-mini']
    assert response.json()['data'][0]['capabilities']['seconds'] == ['4']


def test_invalid_query_and_json_have_openai_error_shape(tmp_path):
    env = setup(tmp_path)
    response = env.client.get('/v1/videos?limit=nope', headers=env.headers)
    assert response.status_code == 400
    assert set(response.json()['error']) == {'message', 'type', 'param', 'code'}
    bad = env.client.post('/v1/videos', content=b'{broken', headers=dict(env.headers, **{'Content-Type': 'application/json'}))
    assert bad.status_code == 400
    assert bad.json()['error']['code'] == 'invalid_json'


def test_provider_exception_does_not_echo_credentials_or_urls(tmp_path):
    async def provider(*args, **kwargs):
        raise RuntimeError('sessionid=private-secret https://example.com/?token=private')
    env = setup(tmp_path, image_generate=provider)
    response = env.client.post('/v1/images/generations', json=image_request(), headers=env.headers)
    assert response.status_code == 409
    assert 'private-secret' not in response.text
    assert 'example.com' not in response.text
    assert env.store.list_tasks()[0]['status'] == 'uncertain'


@pytest.mark.parametrize('failure', ['image_error', 'image_timeout', 'video_dispatch'])
def test_reserved_task_failures_without_idempotency_key_are_not_retryable_5xx(tmp_path, failure):
    calls = []
    async def provider(spec, settings, *, task):
        calls.append(task['id'])
        if failure == 'image_timeout':
            await asyncio.sleep(1)
        raise RuntimeError('Synthetic provider failure after reservation')
    env = setup(tmp_path, image_generate=provider, image_timeout=0.001 if failure == 'image_timeout' else 5)
    if failure == 'video_dispatch':
        def dispatch(spec, *, account_id, job_id=None):
            calls.append('browser-action')
            raise RuntimeError('Dispatch response lost')
        env.bridge.enqueue = dispatch
        response = env.client.post('/v1/videos', json=video_request(), headers=env.headers)
    else:
        response = env.client.post('/v1/images/generations', json=image_request(), headers=env.headers)
    assert response.status_code == 409
    assert response.status_code < 500 and response.status_code not in (408, 429)
    assert response.json()['task_id'] == response.headers['x-request-id']
    assert len(calls) == 1
    assert len(env.store.list_tasks()) == 1


def test_service_unavailable_before_reservation_remains_503(tmp_path):
    env = setup(tmp_path)
    env.store.update_account('test-account', {'enabled': False})
    response = env.client.post('/v1/videos', json=video_request(), headers=env.headers)
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'no_available_account'
    assert 'task_id' not in response.json()
    assert 'x-request-id' not in response.headers
    assert env.store.list_tasks() == []
    assert env.bridge.calls == []


def test_openapi_exposes_owned_gateway_bearer_without_global_admin_security(tmp_path):
    env = setup(tmp_path)
    app = env.client.app
    @app.get('/health')
    def health():
        return {'ok': True}
    schema = augment_openapi(app.openapi())
    scheme = schema['components']['securitySchemes']['GatewayBearer']
    assert scheme['type'] == 'http' and scheme['scheme'] == 'bearer'
    assert 'sk-dr' in scheme['bearerFormat']
    assert '不是 OpenAI' in scheme['description']
    for path, method in [('/v1/models', 'get'), ('/v1/videos', 'post'),
                         ('/v1/images/generations', 'post'), ('/v1/images/edits', 'post'),
                         ('/v1/videos/{video_id}/content', 'get')]:
        assert schema['paths'][path][method]['security'] == [{'GatewayBearer': []}]
    assert 'security' not in schema
    assert 'security' not in schema['paths']['/health']['get']


def test_openapi_documents_real_json_and_multipart_request_fields(tmp_path):
    env = setup(tmp_path)
    schema = augment_openapi(env.client.app.openapi())
    bodies = schema['paths']['/v1/videos']['post']['requestBody']['content']
    assert set(bodies) == {'application/json', 'multipart/form-data'}
    video = schema['components']['schemas']['GatewayVideoCreateRequest']
    assert {'model', 'prompt', 'seconds', 'size'} <= set(video['properties'])
    assert video['properties']['model']['default'] == 'seedance-2.0-mini'
    image = schema['components']['schemas']['GatewayImageGenerationRequest']
    assert {'model', 'prompt', 'n', 'size', 'response_format'} <= set(image['properties'])
    assert image['properties']['response_format']['enum'] == ['url', 'b64_json']
    assert image['properties']['n']['enum'] == [1]
    assert schema['paths']['/v1/videos']['post']['parameters'][-1]['name'] == 'Idempotency-Key'


def test_openapi_responses_and_disabled_edit_are_explicit_and_idempotent(tmp_path):
    env = setup(tmp_path)
    schema = augment_openapi(env.client.app.openapi())
    video = schema['components']['schemas']['GatewayVideo']
    assert video['properties']['status']['enum'] == ['queued', 'in_progress', 'completed', 'failed']
    assert schema['paths']['/v1/images/generations']['post']['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/GatewayImagesResponse')
    edit = schema['paths']['/v1/images/edits']['post']
    assert edit['x-capability-status'] == 'unavailable'
    assert '503' in edit['responses'] and '200' not in edit['responses']
    assert '历史' in schema['paths']['/v1/videos']['post']['description']
    before = json.dumps(schema, sort_keys=True)
    assert json.dumps(augment_openapi(schema), sort_keys=True) == before
