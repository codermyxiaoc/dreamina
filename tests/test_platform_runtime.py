import asyncio
import json
from pathlib import Path
import time
import uuid

import pytest

from cryptography.fernet import Fernet

from dreamina_api.chrome_bridge import BridgeStore
from dreamina_api.platform_store import PlatformStore
from dreamina_api.platform_runtime import PlatformRuntime
import dreamina_api.platform_runtime as runtime_module


def stores(tmp_path, *, old=False):
    store = PlatformStore('sqlite:///:memory:', encryption_key=Fernet.generate_key())
    store.create_account({'id':'default','name':'fixture','region':'US','session_id':'synthetic',
                          'capabilities':['seedance-2.0-mini'],'max_concurrent':8,'max_inflight':8})
    key=store.create_key({'name':'fixture','max_concurrent':100,'rate_limit_per_minute':10000})
    principal=store.authenticate_key(key['key'])
    now=time.time()
    bridge=BridgeStore(tmp_path/'bridge', clock=(lambda:now-180) if old else time.time)
    request={'model':'seedance-2.0-mini','prompt':'fixture prompt','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni'}
    task=store.reserve_task(principal,'video','seedance-2.0-mini',request)
    job=bridge.enqueue(request)
    bridge.heartbeat('default','worker',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate')
    claim=bridge.claim('default','worker')
    bridge.report(job['id'],'worker',claim['claim_token'],status='submitted',submit_id=str(uuid.uuid4()))
    store.update_task(task['id'],bridge_job_id=job['id'],status='submitted')
    return store,bridge,principal,store.get_task(task['id'])


class MissingHistoryClient:
    def __init__(self, *_): pass
    async def __aenter__(self): return self
    async def __aexit__(self,*_): pass
    async def request(self,*_,**__): return {}


def test_missing_history_never_clears_uncertain(tmp_path,monkeypatch):
    store,bridge,_,task=stores(tmp_path)
    store.update_task(task['id'],status='uncertain')
    monkeypatch.setattr(runtime_module,'DreaminaClient',MissingHistoryClient)
    result=asyncio.run(PlatformRuntime(store,bridge).sync_task(task))
    assert result['status']=='uncertain'


def test_unconfirmed_optimistic_card_does_not_stay_processing_forever(tmp_path,monkeypatch):
    store,bridge,_,task=stores(tmp_path,old=True)
    monkeypatch.setattr(runtime_module,'DreaminaClient',MissingHistoryClient)
    result=asyncio.run(PlatformRuntime(store,bridge).sync_task(task))
    assert result['status']=='uncertain'
    assert result['error']['code']=='upstream_confirmation_missing'


def test_reconciler_does_not_lose_old_active_task_behind_new_terminal_rows(tmp_path):
    store,bridge,principal,old=stores(tmp_path)
    for i in range(105):
        task=store.reserve_task(principal,'video','seedance-2.0-mini',{'prompt':str(i)})
        store.update_task(task['id'],status='failed',error={'code':'fixture'})
    assert [t['id'] for t in store.reconciliation_tasks()]==[old['id']]
    assert store.reconciliation_tasks(after_id=old['id'])==[]


def test_late_url_result_cannot_erase_private_image_cache_pointer(tmp_path):
    store,_,_,task=stores(tmp_path)
    store.update_task(task['id'],status='completed',result={'images':[{'url':'https://cdn.example/old','_local_image':'generated-0.bin'}]})
    result=store.update_task(task['id'],status='completed',result={'images':[{'url':'https://cdn.example/new'}]})
    assert result['result']['images'][0]['_local_image']=='generated-0.bin'


@pytest.mark.parametrize('raw',[None,'{broken','[]','{"models":[]}','{"models":true}','['*1100+']'*1100])
def test_optional_native_verification_missing_or_invalid_falls_back(tmp_path,monkeypatch,raw):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    if raw is not None:path.write_text(raw,encoding='utf-8')
    items=runtime_module.model_catalog()
    assert len(items)==5
    for item in items:
        assert item['verification_status']=='native-driver-awaiting-live-verification'
        assert item['verification']['cases']==[]
        assert item['verification']['verified_on'] is None


def test_partial_native_verification_preserves_capabilities_and_runtime_availability(tmp_path,monkeypatch):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    original={item['id']:item for item in runtime_module.model_catalog()}
    case={'resolution':'720p','duration':4,'reference_images':2,'reference_videos':1}
    path.write_text(json.dumps({'models':{
        'seedance-2.0-mini':{'status':'live-verified','verified_on':'2026-10-26','summary':'4秒测试成功。','cases':[case],
                             'available':False,'enabled':False,'capabilities':{'sizes':['all']},'note':'仅代表本次用例。'},
        'unknown-model':{'status':'live-verified','cases':[case]},
    }}),encoding='utf-8')
    actual={item['id']:item for item in runtime_module.model_catalog()}
    assert set(actual)==set(original)
    mini=actual['seedance-2.0-mini']
    assert mini['verification_status']=='live-verified'
    assert mini['verification']['cases']==[case]
    assert mini['verification']['verified_on']=='2026-10-26'
    assert '其他尺寸、时长和参考素材组合未因此获得实测确认' in mini['description']
    assert not any(key in mini['verification'] for key in ('available','enabled','capabilities'))
    for model,item in actual.items():
        assert item['capabilities']==original[model]['capabilities']
        assert item['available']==original[model]['available']
        assert item['enabled']==original[model]['enabled']
        if model!='seedance-2.0-mini':assert item['verification_status']=='native-driver-awaiting-live-verification'


def test_reconciled_video_and_image_have_precise_separate_verification_scopes(tmp_path,monkeypatch):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    fast_case={'resolution':'720p','duration':4,'reference_images':0,'reference_videos':0}
    image_case={'resolution':'2048x2048','duration':None,'reference_images':0,'reference_videos':0}
    note='通过积分流水和上游记录核验恢复，首次 API 回调未成功确认。'
    path.write_text(json.dumps({'models':{
        'seedance-2.0-fast':{'status':'live-verified-reconciled','verified_on':'2026-10-26','cases':[fast_case],'note':note},
        'seedream-5.0-lite':{'status':'live-verified','verified_on':'2026-10-26','cases':[image_case]},
    }}),encoding='utf-8')
    items={item['id']:item for item in runtime_module.model_catalog()}
    fast=items['seedance-2.0-fast'];image=items['seedream-5.0-lite']
    assert fast['verification_status']=='live-verified-reconciled'
    assert fast['verification']['note']==note
    assert '上游记录核对' in fast['description']
    assert fast['verification']['cases']==[fast_case]
    assert image['verification']['cases']==[image_case]
    assert image['capabilities']['edits'] is False
    assert '图片编辑尚未接入' in image['description']


def test_unresolved_ui_evidence_never_claims_permission_failure_or_success(tmp_path,monkeypatch):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    case={'resolution':'720p','duration':4,'ratio':'21:9','reference_images':0,'reference_videos':0}
    path.write_text(json.dumps({'models':{'seedance-2.0':{
        'status':'native-ui-unresolved','summary':'普通套餐面板。','cases':[case],
        'note':'用户提供同账号手动生成截图。','available':False,
    }}}),encoding='utf-8')
    item=next(item for item in runtime_module.model_catalog() if item['id']=='seedance-2.0')
    assert item['verification_status']=='native-ui-unresolved'
    assert item['available'] is True
    assert '不能据此判断账号无模型权限' in item['description']
    assert '已有成功的无扩展实测用例' not in item['description']
    assert item['verification']['cases']==[case]


def test_plan_requirement_records_current_account_block_without_changing_model_availability(tmp_path,monkeypatch):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    original=next(item for item in runtime_module.model_catalog() if item['id']=='seedance-2.5')
    case={'resolution':'480p','duration':4,'reference_images':0,'reference_videos':0}
    note='仅当前测试账号：1:1 用例弹出购买升级面板，未发生成请求，未扣费。'
    path.write_text(json.dumps({'models':{'seedance-2.5':{
        'status':'account-plan-required','verified_on':'2026-10-26',
        'summary':'4 秒、480p、1:1 用例要求升级套餐。','cases':[case],'note':note,
        'available':False,'enabled':False,
    }}}),encoding='utf-8')
    item=next(item for item in runtime_module.model_catalog() if item['id']=='seedance-2.5')
    assert item['verification_status']=='account-plan-required'
    assert item['verification']['status']=='account-plan-required'
    assert item['verification']['cases']==[case]
    assert item['verification']['note']==note
    assert '当前测试账号的用例进入订阅升级界面，未完成生成' in item['description']
    assert '不代表所有账号均不可用' in item['description']
    assert '已有成功' not in item['description'] and '核对确认' not in item['description']
    assert item['available']==original['available']
    assert item['enabled']==original['enabled']
    assert item['capabilities']==original['capabilities']


@pytest.mark.parametrize('record',[
    'live-verified',None,[],{},
    {'status':'unknown-success'},
    {'status':'<script>pretend-success</script>'},
    {'status':['live-verified']},
    {'status':{'value':'live-verified'}},
    {'status':'live-verified','cases':[]},
    {'status':'live-verified','cases':['all sizes']},
    {'status':'live-verified','cases':[{'resolution':'720p','duration':True,'reference_images':0,'reference_videos':0}]},
    {'status':'live-verified','cases':[{'resolution':'720p','duration':0,'reference_images':0,'reference_videos':0}]},
    {'status':'live-verified','cases':[{'resolution':'4k','duration':4,'reference_images':0,'reference_videos':0}]},
])
def test_unknown_or_malformed_verification_cannot_mark_model_verified(tmp_path,monkeypatch,record):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    path.write_text(json.dumps({'models':{'seedance-2.0-mini':record}}),encoding='utf-8')
    mini=next(item for item in runtime_module.model_catalog() if item['id']=='seedance-2.0-mini')
    assert mini['verification_status']=='native-driver-awaiting-live-verification'
    assert mini['verification']['cases']==[]


@pytest.mark.parametrize('model,case',[
    ('seedance-2.0-mini',{'resolution':'720p','duration':4,'reference_images':2,'reference_videos':1,'ratio':'16:9'}),
    ('seedream-5.0-lite',{'resolution':'2048x2048','duration':None,'reference_images':0,'reference_videos':0,'ratio':'1:1'}),
])
def test_verification_case_retains_exact_supported_ratio(tmp_path,monkeypatch,model,case):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    path.write_text(json.dumps({'models':{model:{'status':'live-verified','cases':[case]}}}),encoding='utf-8')
    item=next(item for item in runtime_module.model_catalog() if item['id']==model)
    assert item['verification_status']=='live-verified'
    assert item['verification']['cases']==[case]
    assert item['verification']['cases'][0]['ratio']==case['ratio']


@pytest.mark.parametrize('model,case',[
    ('seedance-2.0-mini',{'resolution':'720p','duration':4,'reference_images':0,'reference_videos':0,'ratio':'7:3'}),
    ('seedream-5.0-lite',{'resolution':'2048x2048','duration':None,'reference_images':0,'reference_videos':0,'ratio':'16:9'}),
])
def test_unknown_case_ratio_cannot_be_dropped_to_claim_broader_verification(tmp_path,monkeypatch,model,case):
    path=tmp_path/'native-verification.json'
    monkeypatch.setattr(runtime_module,'NATIVE_VERIFICATION_PATH',path)
    path.write_text(json.dumps({'models':{model:{'status':'live-verified','cases':[case]}}}),encoding='utf-8')
    item=next(item for item in runtime_module.model_catalog() if item['id']==model)
    assert item['verification_status']=='native-driver-awaiting-live-verification'
    assert item['verification']['cases']==[]
