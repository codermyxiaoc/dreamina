"""Offline browser fault injection; no website credentials or paid requests."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs

import pytest
from playwright.async_api import async_playwright

from dreamina_api.browser_driver import (NativeUI,UIUnavailable,generation_request,
    generation_response,committed_resource_ids,request_belongs_to_page)
from dreamina_api.payloads import build_generation


ORIGIN='https://dreamina-api.us.capcut.com'
PATH='/mweb/v1/aigc_draft/generate'
SPEC={'model':'seedance-2.0-mini','prompt':'A synthetic ceramic cube','duration':4,
      'resolution':'720p','ratio':'16:9','mode':'omni','references':[]}


@asynccontextmanager
async def browser_fixture():
    async with async_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():pytest.skip('Offline Chromium runtime unavailable')
        browser=await pw.chromium.launch(headless=True)
        try:yield await browser.new_context(offline=True)
        finally:await browser.close()


def html(action):
    return '''<!doctype html><body>
    <button aria-label="New session" onclick="document.querySelector('#prompt').textContent='';window.sessions=(window.sessions||0)+1">New session</button>
    <div id="prompt" contenteditable="true" role="textbox" aria-label="Prompt"></div>
    <div role="combobox" aria-label="Create">AI Video</div>
    <div role="combobox" aria-label="Model">Dreamina Seedance 2.0 Mini</div>
    <div role="combobox">Omni reference</div><button>16:9 720P</button><button>4s</button>
    <button id="generate" aria-label="Generate">Generate</button>
    <script>document.querySelector('#generate').onclick=async()=>{window.clicks=(window.clicks||0)+1;ACTION};</script>
    </body>'''.replace('ACTION',action)


def fetch_body(body):
    return "fetch('"+ORIGIN+PATH+"',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify("+json.dumps(body)+")})"


async def generation_route(context,document,handler):
    async def route(route):
        if route.request.resource_type=='document':
            await route.fulfill(status=200,content_type='text/html',body=document)
        elif urlsplit(route.request.url).path==PATH:
            if route.request.method=='OPTIONS':
                await route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type'})
            else:await handler(route)
        else:await route.abort()
    await context.route('**/*',route)


async def success(route):
    body=json.loads(route.request.post_data)
    await route.fulfill(status=200,content_type='application/json',body=json.dumps(
        {'ret':'0','data':{'aigc_data':{'submit_id':body['submit_id'],'history_record_id':'1234'}}}),
        headers={'Access-Control-Allow-Origin':'*'})


async def noop(*_):pass


def test_request_receipt_is_persisted_before_a_missing_response_and_only_activated_once(tmp_path):
    async def run():
        prepared=build_generation(SPEC,workspace_id='123456');body=prepared['body']
        received=asyncio.Event();calls=[];intents=[];requests=[]
        path=tmp_path/'receipt.json'
        def persist(receipt):
            calls.append(deepcopy(receipt));path.write_text(json.dumps(receipt),encoding='utf-8')
            receipt['submit_id']='callback-mutation-must-not-change-driver-state'
            received.set()
        async with browser_fixture() as ctx:
            async def abort(route):
                requests.append(route.request)
                await asyncio.wait_for(received.wait(),1)
                assert path.exists()
                await route.abort()
            await generation_route(ctx,html('try{await '+fetch_body(body)+';}catch{}'),abort)
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            async def intent():intents.append(True)
            driver=NativeUI(page,noop,submission_timeout_ms=200)
            result=await driver.submit(SPEC,[],intent,persist)
            assert result['status']=='uncertain' and result['submit_id']==prepared['submit_id']
            assert json.loads(path.read_text())['submit_id']==prepared['submit_id']
            assert len(calls)==len(requests)==len(intents)==1
            assert await page.evaluate('window.clicks')==1
            with pytest.raises(UIUnavailable,match='already attempted'):
                await driver.submit(SPEC,[],intent,persist)
            assert await page.evaluate('window.clicks')==1 and len(intents)==1
    asyncio.run(run())


def test_duplicate_sdk_requests_for_same_id_invoke_persistence_once():
    async def run():
        prepared=build_generation(SPEC,workspace_id='123456');body=prepared['body'];calls=[];requests=[]
        async with browser_fixture() as ctx:
            async def reply(route):
                requests.append(route.request);await asyncio.sleep(.05);await success(route)
            await generation_route(ctx,html('await Promise.all(['+fetch_body(body)+','+fetch_body(body)+']);'),reply)
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            driver=NativeUI(page,noop,submission_timeout_ms=1000)
            result=await driver.submit(SPEC,[],noop,lambda receipt:calls.append(receipt))
            assert result['status']=='submitted' and result['submit_id']==prepared['submit_id']
            assert len(requests)==2 and len(calls)==1
            assert await page.evaluate('window.clicks')==1
    asyncio.run(run())


def test_distinct_matching_ids_are_persisted_as_ambiguity_without_selecting_a_primary():
    async def run():
        one=build_generation(SPEC,workspace_id='123456');two=build_generation(SPEC,workspace_id='123456');calls=[]
        async with browser_fixture() as ctx:
            async def reply(route):await asyncio.sleep(.1);await success(route)
            await generation_route(ctx,html('await Promise.all(['+fetch_body(one['body'])+','+fetch_body(two['body'])+']);'),reply)
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            driver=NativeUI(page,noop,submission_timeout_ms=1000)
            result=await driver.submit(SPEC,[],noop,lambda receipt:calls.append(receipt))
            ids={one['submit_id'],two['submit_id']}
            assert result['status']=='uncertain' and result['error_code']=='ambiguous_submission'
            assert set(result['observed_submit_ids'])==ids and 'submit_id' not in result
            assert driver.request_observed is None and driver.submission_issue==result
            assert len(calls)==2 and calls[0]['submit_id'] in ids
            assert calls[1]['error_code']=='ambiguous_submission' and 'submit_id' not in calls[1]
            assert set(calls[1]['observed_submit_ids'])==ids
            assert await page.evaluate('window.clicks')==1
    asyncio.run(run())


def test_failed_receipt_persistence_keeps_unique_id_but_never_claims_success_or_leaks_error():
    async def run():
        prepared=build_generation(SPEC,workspace_id='123456');calls=[]
        def broken(receipt):
            calls.append(receipt)
            raise OSError('private-path-and-secret-must-not-be-returned')
        async with browser_fixture() as ctx:
            await generation_route(ctx,html('await '+fetch_body(prepared['body'])+';'),success)
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            driver=NativeUI(page,noop,submission_timeout_ms=1000)
            result=await driver.submit(SPEC,[],noop,broken)
            assert result['status']=='uncertain' and result['error_code']=='receipt_persistence_failed'
            assert result['submit_id']==prepared['submit_id'] and len(calls)==1
            assert driver.request_observed['submit_id']==prepared['submit_id']
            assert driver.submission_issue==result
            assert 'private-path' not in json.dumps(result)
            assert await page.evaluate('window.clicks')==1
    asyncio.run(run())


def test_same_prompt_from_another_page_cannot_supply_this_tasks_receipt():
    async def run():
        prepared=build_generation(SPEC,workspace_id='123456');calls=[];requests=[]
        async with browser_fixture() as ctx:
            async def reply(route):requests.append(route.request);await success(route)
            await generation_route(ctx,html('await window.sendFromOtherPage();'),reply)
            other=await ctx.new_page();await other.goto('https://dreamina.capcut.com/other')
            page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            async def other_submit():await other.evaluate('async()=>{await '+fetch_body(prepared['body'])+';}')
            await page.expose_function('sendFromOtherPage',other_submit)
            driver=NativeUI(page,noop,submission_timeout_ms=200)
            result=await driver.submit(SPEC,[],noop,lambda receipt:calls.append(receipt))
            assert result['status']=='uncertain' and 'submit_id' not in result
            assert not calls and len(requests)==1 and driver.request_observed is None
            assert await page.evaluate('window.clicks')==1
    asyncio.run(run())


def test_frame_identity_is_used_when_known_and_worker_frame_absence_is_tolerated():
    page=object();other=object()
    assert request_belongs_to_page(SimpleNamespace(frame=SimpleNamespace(page=page)),page)
    assert not request_belongs_to_page(SimpleNamespace(frame=SimpleNamespace(page=other)),page)
    class WorkerRequest:
        @property
        def frame(self):raise RuntimeError('Service worker has no frame')
    assert request_belongs_to_page(WorkerRequest(),page)
    assert generation_request(ORIGIN+PATH,'POST','{}',SPEC) is None


@pytest.mark.parametrize('raw',['[]','null','{"submit_id":123}','{"submit_id":{}}','['*1200+']'*1200,'not-json'])
def test_malformed_request_payloads_do_not_escape_event_handlers(raw):
    assert generation_request(ORIGIN+PATH,'POST',raw,SPEC) is None


@pytest.mark.parametrize('field,value',[('resolution',720),('prompt',[]),('unified_edit_input',[]),('unified_edit_input',{'meta_list':[None]})])
def test_malformed_draft_fields_are_rejected_without_an_event_loop_error(field,value):
    prepared=build_generation(SPEC,workspace_id='123456');body=deepcopy(prepared['body']);draft=json.loads(body['draft_content'])
    item=draft['component_list'][0]['abilities']['gen_video']['text_to_video_params']['video_gen_inputs'][0]
    item[field]=value;body['draft_content']=json.dumps(draft)
    assert generation_request(ORIGIN+PATH,'POST',json.dumps(body),SPEC) is None
    assert generation_request('https://[malformed','POST',json.dumps(body),SPEC) is None


@pytest.mark.parametrize('payload',[{'ret':0,'data':[]},{'ret':0,'data':{'aigc_data':[]}},{'ret':0,'data':{'aigc_data':{'history_record_id':{'secret':'x'}}}},{'ret':{'private':'x'}},{'ret':True}])
def test_malformed_response_never_becomes_success(payload):
    result=generation_response({'submit_id':'9be0f7c6-0bb9-4faa-a826-8c23ee75fead'},200,payload)
    assert result['status']=='uncertain'
    assert 'private' not in result.get('error','') and 'secret' not in result.get('error','')


def test_commit_resources_require_real_identifiers_and_reject_failed_uri_status():
    assert committed_resource_ids('CommitImageUpload',{'Result':{'Results':[{}],'PluginResult':[{}]}})==()
    assert committed_resource_ids('CommitImageUpload',{'Result':{'PluginResult':[{'ImageUri':'one'}],'Results':[{'Uri':'one','UriStatus':4000}]}})==()
    assert committed_resource_ids('CommitImageUpload',{'Result':{'PluginResult':[{'ImageUri':'one'}],'Results':[{'UriStatus':4000}]}})==()
    assert committed_resource_ids('CommitImageUpload',{'ResponseMetadata':{'Error':{'Code':'bad'}},'Result':{'PluginResult':[{'ImageUri':'one'}]}})==()
    assert committed_resource_ids('CommitImageUpload',{'Result':{'PluginResult':[{'ImageUri':'one'},{'ImageUri':'two'}],'Results':[{'Uri':'one','UriStatus':2000},{'Uri':'two','UriStatus':2000}]}})==('one','two')
    assert committed_resource_ids('CommitUploadInner',{'Result':{'Results':[{'Vid':'one'},{'Vid':'one'},{'Vid':'two'},{}]}})==('one','two')


@pytest.mark.parametrize('visible_error',[False,True])
def test_batch_upload_counts_resources_not_responses_and_checks_page_failures(tmp_path,visible_error):
    async def run():
        paths=[]
        for name in ('one.png','two.png','motion.mp4'):
            path=tmp_path/name;path.write_bytes(b'offline synthetic contents');paths.append(path)
        events=[];requests=[]
        document='''<!doctype html><body><input type="file" multiple accept="image/*,video/*" id="files"><div role="alert" id="error"></div>
        <script>document.querySelector('#files').onchange=async()=>{
        ERROR
        const commit=(action,session)=>fetch('https://imagex.capcutapi.us/?Action='+action,{method:'POST',body:JSON.stringify({SessionKey:session})});
        await commit('CommitImageUpload','same-image-upload');await commit('CommitImageUpload','same-image-upload');
        await commit('CommitUploadInner','video-upload');window.finished=true;};</script></body>'''.replace('ERROR',"document.querySelector('#error').textContent='Upload failed';" if visible_error else '')
        async with browser_fixture() as ctx:
            async def route_handler(route):
                if route.request.resource_type=='document':await route.fulfill(status=200,content_type='text/html',body=document);return
                action=parse_qs(urlsplit(route.request.url).query)['Action'][0];requests.append(action)
                data={'Result':{'Results':[{'Vid':'video-one'}]}} if action=='CommitUploadInner' else {'Result':{'PluginResult':[{'ImageUri':'image-one'},{'ImageUri':'image-two'}],'Results':[{'Uri':'image-one','UriStatus':2000},{'Uri':'image-two','UriStatus':2000}]}}
                await route.fulfill(status=200,content_type='application/json',body=json.dumps(data),headers={'Access-Control-Allow-Origin':'*'})
            await ctx.route('**/*',route_handler);page=await ctx.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            async def telemetry(value):events.append(value)
            driver=NativeUI(page,telemetry)
            spec={'references':[{'kind':'image'},{'kind':'image'},{'kind':'video'}]}
            if visible_error:
                with pytest.raises(UIUnavailable,match='upload rejected'):
                    await asyncio.wait_for(driver.upload(spec,paths),3)
                assert not any(event['stage']=='references_ready' for event in events)
            else:
                await asyncio.wait_for(driver.upload(spec,paths),3)
                assert requests==['CommitImageUpload','CommitImageUpload','CommitUploadInner']
                assert events[-1]['detail']['commit_counts']=={'image':2,'video':1}
                assert events[-1]['stage']=='references_ready'
    asyncio.run(run())


def test_missing_reference_files_fail_before_any_page_operations():
    async def run():
        with pytest.raises(ValueError,match='Reference files'):
            await NativeUI(None,noop).upload({'references':[{'kind':'image'}]},[])
    asyncio.run(run())
