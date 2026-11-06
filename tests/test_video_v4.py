import base64
from pathlib import Path
import sqlite3
import struct
import time
import zlib
import pytest
from cryptography.fernet import Fernet

from dreamina_api.video_inputs import normalize_options,parse_references,validate_references,save_references,asset_path_for_task
from dreamina_api.openai_api import video_object
from dreamina_api.platform_store import PlatformStore,PlatformError
from dreamina_api.audit import sanitize
from dreamina_api.chrome_bridge import BridgeStore,BridgeError


def png(w=512,h=512):
    def chunk(kind,body):return struct.pack('!I',len(body))+kind+body+struct.pack('!I',zlib.crc32(kind+body)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\x12\x56\x78'*w)*h))+chunk(b'IEND',b'')


@pytest.mark.parametrize('model,max_duration',[('seedance-2.5',30),('seedance-2.0-mini',15),('seedance-2.0-fast',15),('seedance-2.0',15)])
def test_all_models_and_native_options(model,max_duration):
    spec=normalize_options({'model':model,'prompt':' scene ','duration':max_duration,'resolution':'720p','ratio':'9:16'})
    assert spec['new_session'] is True and spec['size']=='720x1280' and spec['prompt']=='scene'
    with pytest.raises(ValueError):normalize_options({'model':model,'prompt':'x','duration':max_duration+1})


@pytest.mark.parametrize('fields',[{'duration':8,'seconds':'4'},{'size':'1280x720','resolution':'1080p'}, {'size':'1280x720','ratio':'1:1'},{'duration':True},{'resolution':'4k'}])
def test_conflicting_or_unverified_options_rejected(fields):
    with pytest.raises(ValueError):normalize_options({'prompt':'x',**fields})


def test_multiple_images_are_private_and_content_addressed(tmp_path):
    raw=png();url='data:image/png;base64,'+base64.b64encode(raw).decode()
    data={'references':[{'type':'image','data_url':url,'name':'../../secret'},{'type':'image','data_url':url}]}
    refs=parse_references(data,[]);spec=normalize_options({'prompt':'two references'})
    validate_references(spec,refs)
    assert len(refs)==2 and refs[0]['metadata']['asset_id']!=refs[1]['metadata']['asset_id']
    assert refs[0]['metadata']['name']=='reference-1.png'
    task='video_'+'a'*32;save_references(tmp_path,task,refs)
    assert asset_path_for_task(tmp_path,task,refs[0]['metadata']).read_bytes()==raw
    with pytest.raises(ValueError):asset_path_for_task(tmp_path,'../../private',refs[0]['metadata'])


def test_reference_video_from_retained_generated_fixture():
    path=Path(__file__).resolve().parents[1]/'outputs'/'openai-video-verified.mp4'
    if not path.exists():pytest.skip('Private generated fixture unavailable')
    refs=parse_references({},[{'field':'reference_videos','filename':'reference.mp4','content_type':'video/mp4','bytes':path.read_bytes()}])
    validate_references(normalize_options({'prompt':'reference motion'}),refs)
    assert refs[0]['metadata']['duration']>2000 and refs[0]['metadata']['fps']>0


@pytest.mark.parametrize('ref',[{'type':'image','data_url':'https://localhost/private'}, {'type':'image','path':'C:/private'}, {'type':'image','data_url':'data:image/png;base64,bm90LXBuZw=='}])
def test_unsafe_reference_input_is_not_fetched_or_opened(ref):
    with pytest.raises(ValueError):parse_references({'references':[ref]},[])


def test_unknown_response_fields_are_omitted_and_failure_remains_job():
    base={'id':'vid_fixture','model':'m','status':'failed','created_at':1,'error':{'code':'2052','message':'Output rejected'},'request':{}}
    obj=video_object(base)
    assert obj['status']=='failed' and obj['error']==base['error']
    for field in ('seconds','size','duration','resolution','usage','billing_status','progress'):assert field not in obj
    base.update(status='in_progress',progress=61,progress_details={'source':'page','observed_at':time.time(),'value':61})
    assert video_object(base)['progress']==61
    base.update(status='completed',usage={'completion_tokens':42},billing_status='settled')
    assert video_object(base)['progress']==100 and video_object(base)['usage']['completion_tokens']==42


def fixture_store(tmp_path):
    store=PlatformStore('sqlite:///'+str(tmp_path/'db.sqlite'),encryption_key=Fernet.generate_key())
    store.create_account({'id':'default','name':'Synthetic','session_id':'synthetic','capabilities':['seedance-2.0-mini']})
    key=store.create_key({'name':'test'});principal=store.authenticate_key(key['key'])
    task=store.reserve_task(principal,'video','seedance-2.0-mini',{'prompt':'test'})
    return store,task


def test_progress_billing_and_usage_need_evidence(tmp_path):
    store,task=fixture_store(tmp_path)
    assert store.get_task(task['id'])['progress'] is None
    for fields in ({'usage':{'completion_tokens':100}},{'billing_status':'settled'},{'progress_details':{'source':'guess'}}):
        with pytest.raises(PlatformError):store.update_task(task['id'],**fields)
    store.update_task(task['id'],progress=57,progress_details={'source':'page','observed_at':time.time()})
    assert store.get_task(task['id'])['progress']==57
    result=store.update_task(task['id'],status='completed')
    assert result['progress']==100 and result['billing_status'] is None and result['usage'] is None


def test_request_audit_redacts_secrets_not_prompt_or_counts(tmp_path):
    store,task=fixture_store(tmp_path)
    detail={'prompt':'A landscape','Authorization':'Bearer private','Cookie':'sessionid=secret','url':'https://cdn.example/x?signature=secret',
            'references':[{'data_url':'data:image/png;base64,SECRET'}],'usage':{'completion_tokens':123}}
    event=store.append_event(task['id'],'request',detail)
    assert event['detail']['prompt']=='A landscape'
    encoded=str(event['detail']);assert 'private' not in encoded and 'SECRET' not in encoded and 'signature=secret' not in encoded
    assert event['detail']['usage']['completion_tokens']==123
    assert store.list_events(task['id'])[0]['stage']=='request'


def test_claim_guards_reference_access_and_telemetry(tmp_path):
    bridge=BridgeStore(tmp_path/'bridge');job=bridge.enqueue({'prompt':'test'})
    bridge.heartbeat('default','worker',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate')
    claim=bridge.claim('default','worker')
    with pytest.raises(BridgeError):bridge.verify_claim(job['id'],'other',claim['claim_token'])
    result=bridge.telemetry(job['id'],'worker',claim['claim_token'],{'stage':'new_session','observed_at':time.time(),'conversation_id':'123456'})
    assert result['telemetry']['conversation_id']=='123456'
    assert result['request']['new_session'] is True
