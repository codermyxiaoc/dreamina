from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import FileResponse
from dreamina_api.public_cors import PublicAPICORSMiddleware,parse_public_origins
from dreamina_api.admin_api import create_admin_router
from test_openai_api import setup,video_request

ORIGIN='https://client.example'
PREFLIGHT={'Origin':ORIGIN,'Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'authorization'}

def fixture(tmp_path,origins=None):
    env=setup(tmp_path);env.client.app.add_middleware(PublicAPICORSMiddleware,origins=origins)
    env.client.app.include_router(create_admin_router(env.store))
    return env

@pytest.mark.parametrize('path,method,headers',[
    ('/v1/models','GET','authorization'),('/v1/models/','GET','authorization'),
    ('/v1/videos','POST','authorization,content-type,idempotency-key'),
    ('/v1/videos/video_test','GET','authorization'),('/v1/videos/video_test/content','GET','authorization,range'),
    ('/v1/images/generations','POST','authorization,content-type,x-stainless-lang'),
    ('/v1/images/edits','POST','authorization'),('/v1/images/jobs/img_test','GET','authorization'),
    ('/v1/images/jobs/img_test/result','GET','authorization'),
])
def test_public_preflight_without_bearer_does_not_allocate_or_dispatch(tmp_path,path,method,headers):
    env=fixture(tmp_path)
    r=env.client.options(path,headers={'Origin':ORIGIN,'Access-Control-Request-Method':method,'Access-Control-Request-Headers':headers})
    assert r.status_code==200 and r.headers['access-control-allow-origin']=='*'
    assert 'authorization' in r.headers['access-control-allow-headers'].lower()
    assert 'access-control-allow-credentials' not in r.headers
    assert not env.bridge.calls and not env.store.list_tasks()

def test_actual_success_and_auth_error_are_readable_without_weakening_auth(tmp_path):
    env=fixture(tmp_path)
    denied=env.client.get('/v1/models',headers={'Origin':ORIGIN})
    assert denied.status_code==401 and denied.headers['access-control-allow-origin']=='*'
    response=env.client.get('/v1/models',headers={**env.headers,'Origin':ORIGIN})
    assert response.status_code==200 and response.headers['access-control-allow-origin']=='*'
    assert 'access-control-allow-credentials' not in response.headers
    assert not env.bridge.calls

def test_creation_and_idempotent_conflict_keep_cors_headers(tmp_path):
    env=fixture(tmp_path);headers={**env.headers,'Origin':ORIGIN,'Idempotency-Key':'cors-test'}
    first=env.client.post('/v1/videos',json=video_request(),headers=headers)
    repeated=env.client.post('/v1/videos',json=video_request(),headers=headers)
    conflict=env.client.post('/v1/videos',json=video_request(prompt='changed'),headers=headers)
    assert first.status_code==repeated.status_code==200 and conflict.status_code==409
    assert first.json()['id']==repeated.json()['id'] and len(env.bridge.calls)==1
    for response in (first,repeated,conflict):
        assert response.headers['access-control-allow-origin']=='*'
        assert 'x-request-id' in response.headers['access-control-expose-headers'].lower()

@pytest.mark.parametrize('path',['/api/admin/accounts','/api/admin/login','/v1/chrome/jobs','/v1/assets/upload','/internal/videos/generations','/admin/','/v1/videos/one/content/extra'])
def test_non_public_routes_never_receive_public_cors_headers(tmp_path,path):
    env=fixture(tmp_path);r=env.client.options(path,headers=PREFLIGHT)
    assert r.status_code in (404,405)
    assert 'access-control-allow-origin' not in r.headers
    if path=='/api/admin/login':
        r=env.client.post(path,json={'username':'admin','password':'ignored'},headers={'Origin':ORIGIN})
        assert r.status_code==403 and 'access-control-allow-origin' not in r.headers

def test_allowlist_and_disabled_preflight_are_enforced(tmp_path):
    for origins in ([ORIGIN],[]):
        env=fixture(tmp_path,origins)
        allowed=env.client.options('/v1/models',headers=PREFLIGHT)
        assert allowed.status_code==(200 if origins else 400)
        bad=env.client.options('/v1/models',headers={**PREFLIGHT,'Origin':'https://other.example'})
        assert bad.status_code==400 and 'access-control-allow-origin' not in bad.headers
        if origins:assert allowed.headers['access-control-allow-origin']==ORIGIN and 'Origin' in allowed.headers['vary']

def test_unsupported_preflight_method_is_not_allowed(tmp_path):
    env=fixture(tmp_path)
    assert env.client.options('/v1/videos',headers={**PREFLIGHT,'Access-Control-Request-Method':'DELETE'}).status_code==400

def test_download_stream_and_range_expose_metadata(tmp_path):
    p=tmp_path/'video.mp4';p.write_bytes(b'0123456789')
    app=FastAPI();app.add_middleware(PublicAPICORSMiddleware)
    @app.get('/v1/videos/{id}/content')
    def download(id):return FileResponse(p,media_type='video/mp4',headers={'X-Request-ID':id})
    with TestClient(app) as client:
        r=client.get('/v1/videos/one/content',headers={'Origin':ORIGIN,'Range':'bytes=0-3'})
        assert r.status_code==206 and r.content==b'0123'
        assert r.headers['access-control-allow-origin']=='*'
        assert 'content-range' in r.headers['access-control-expose-headers'].lower()

@pytest.mark.parametrize('value',['not-json','"*"','["*","https://one.example"]','["null"]','["https://host/path"]','["https://user:pass@host"]','["https://host?x=y"]','["ftp://host"]','[1]'])
def test_invalid_origin_configuration_is_rejected(value):
    with pytest.raises(ValueError):parse_public_origins(value)
