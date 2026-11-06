"""Offline browser submission outcomes preserve evidence and never purchase plans."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from dreamina_api.browser_driver import NativeUI, explicit_model_upgrade_notice
from dreamina_api.models import model_config
from dreamina_api.payloads import build_generation


GENERATE_PATH='/mweb/v1/aigc_draft/generate'


def fixture_html(spec,onclick):
    return '''<!doctype html><html><head><meta charset="utf-8"></head><body>
    <button aria-label="New session" onclick="document.querySelector('#old')?.remove();document.querySelector('#prompt').textContent='';window.newSessions=(window.newSessions||0)+1">New session</button>
    <div id="old" data-generated-record-key="old-fixture">Old task</div>
    <div contenteditable="true" role="textbox" aria-label="Prompt" id="prompt">Old prompt</div>
    <div role="combobox" aria-label="Create">AI Video</div>
    <div role="combobox" aria-label="Model">MODEL_NAME</div>
    <div role="combobox">Omni reference</div>
    <button>DIMENSIONS</button><button>4s</button>
    <button aria-label="Generate" id="generate">Generate</button>
    <div id="upgrade" hidden><h2>Upgrade your plan</h2>
      <button onclick="window.purchases=(window.purchases||0)+1">Upgrade to Standard · Subscribe</button>
    </div>
    <script>document.querySelector('#generate').onclick=async()=>{window.clicks=(window.clicks||0)+1;ONCLICK};</script>
    </body></html>'''.replace('MODEL_NAME',model_config(spec['model'])['name']).replace(
        'DIMENSIONS',spec['ratio']+' '+spec['resolution'].upper()).replace('ONCLICK',onclick)


async def offline_browser(pw):
    if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
    browser=await pw.chromium.launch(headless=True)
    return browser,await browser.new_context(offline=True)


def test_aborted_generation_response_retains_observed_submit_id_after_one_click():
    async def run():
        spec={'model':'seedance-2.0-mini','prompt':'Synthetic cloudy sky','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni'}
        prepared=build_generation(spec,workspace_id='123456')
        onclick="try{await fetch('https://dreamina-api.us.capcut.com"+GENERATE_PATH+"',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify("+json.dumps(prepared['body'])+")});}catch(error){window.transportFailed=true;}"
        html=fixture_html(spec,onclick)
        requests=[];responses=[];events=[];click_intents=[]
        async with async_playwright() as pw:
            browser,context=await offline_browser(pw)
            async def route_handler(route):
                if route.request.resource_type=='document':
                    await route.fulfill(status=200,content_type='text/html',body=html)
                elif urlsplit(route.request.url).path==GENERATE_PATH:
                    if route.request.method=='OPTIONS':
                        await route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type'})
                    else:
                        requests.append(json.loads(route.request.post_data))
                        await route.abort('failed')
                else:await route.abort()
            await context.route('**/*',route_handler)
            context.on('response',lambda response:responses.append(response.status) if urlsplit(response.url).path==GENERATE_PATH and response.request.method=='POST' else None)
            page=await context.new_page();await page.goto('https://dreamina.capcut.com/ai-tool/generate')
            async def telemetry(value):events.append(value)
            async def intent():click_intents.append(True)
            driver=NativeUI(page,telemetry,submission_timeout_ms=200)
            try:
                result=await asyncio.wait_for(driver.submit(spec,[],intent),timeout=5)
                assert result['status']=='uncertain'
                assert result['submit_id']==prepared['submit_id']
                assert driver.request_observed['submit_id']==prepared['submit_id']
                assert result['conversation_id']=='123456'
                assert result['request_summary']['model']==spec['model']
                assert len(requests)==1 and not responses
                assert await page.evaluate('window.transportFailed') is True
                assert await page.evaluate('window.clicks')==1
                assert await page.evaluate('window.newSessions')==1
                assert await page.evaluate('window.purchases||0')==0
                assert len(click_intents)==1
                assert all(event['stage']!='generation_response' for event in events)
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('notice,expected_status,expected_code',[
    ('Upgrade your plan','uncertain','subscription_panel_unresolved'),
    ('Dreamina Seedance 2.5 requires a plan upgrade.','failed','account_plan_required'),
    ('Upgrade to use this feature: Dreamina Seedance 2.5','failed','account_plan_required'),
    ('Dreamina Seedance 2.0 requires a plan upgrade.','uncertain','subscription_panel_unresolved'),
    ('Dreamina Seedance 2.5 is available. Upgrade to Standard yearly.','uncertain','subscription_panel_unresolved'),
])
def test_subscription_panel_needs_explicit_current_model_evidence(notice,expected_status,expected_code):
    async def run():
        spec={'model':'seedance-2.5','prompt':'Synthetic ceramic cube','duration':4,'resolution':'480p','ratio':'1:1','mode':'omni'}
        html=fixture_html(spec,"document.querySelector('#upgrade').hidden=false;").replace('Upgrade your plan',notice)
        requests=[];events=[];click_intents=[]
        async with async_playwright() as pw:
            browser,context=await offline_browser(pw)
            async def route_handler(route):
                if route.request.resource_type=='document':
                    await route.fulfill(status=200,content_type='text/html',body=html)
                else:
                    requests.append(urlsplit(route.request.url).path)
                    await route.abort()
            await context.route('**/*',route_handler)
            page=await context.new_page();await page.goto('https://dreamina.capcut.com/ai-tool/generate')
            async def telemetry(value):events.append(value)
            async def intent():click_intents.append(True)
            driver=NativeUI(page,telemetry,submission_timeout_ms=200)
            try:
                result=await asyncio.wait_for(driver.submit(spec,[],intent),timeout=5)
                assert result['status']==expected_status
                assert result['error_code']==expected_code
                assert 'submit_id' not in result and driver.request_observed is None
                assert not requests
                assert await page.evaluate('window.clicks')==1
                assert await page.evaluate('window.newSessions')==1
                assert await page.evaluate('window.purchases||0')==0
                assert await page.get_by_role('button',name='Upgrade to Standard · Subscribe').is_visible()
                assert len(click_intents)==1
                assert events[-1]['stage']==expected_code
                assert events[-1]['detail']=={'generation_request_observed':False,'model_permission_confirmed':expected_status=='failed'}
                if expected_status=='uncertain':
                    assert not any(e['stage']=='account_plan_required' for e in events)
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('text,model,expected',[
    ('Dreamina Seedance 2.0 requires a plan upgrade.','Dreamina Seedance 2.0',True),
    ('Seedance 2.0 Fast requires a plan upgrade.','Dreamina Seedance 2.0',False),
    ('Seedance 2.0 Mini requires a plan upgrade.','Dreamina Seedance 2.0',False),
    ('Upgrade your plan to use Dreamina Seedance 2.5.','Dreamina Seedance 2.5',True),
    ('Upgrade your plan to use Dreamina Seedance 2.0 Fast.','Dreamina Seedance 2.0',False),
    ('Dreamina Seedance 2.5 does not require a plan upgrade.','Dreamina Seedance 2.5',False),
    ('Dreamina Seedance 2.5 is only available on the Standard plan.','Dreamina Seedance 2.5',True),
    ('Dreamina Seedance 2.5. Upgrade to Standard yearly.','Dreamina Seedance 2.5',False),
    ('Upgrade to use this feature: Dreamina Seedance 2.5','Dreamina Seedance 2.5',True),
    ('Upgrade to use this feature: Dreamina Seedance 2.0.','Dreamina Seedance 2.0',True),
    ('Upgrade to use this feature: Dreamina Seedance 2.0','Dreamina Seedance 2.5',False),
    ('Upgrade to use this feature: Dreamina Seedance 2.0 Mini','Dreamina Seedance 2.0',False),
    ('Upgrade to use this feature: Dreamina Seedance 2.0 Fast','Dreamina Seedance 2.0',False),
    ('No need to upgrade to use this feature: Dreamina Seedance 2.5','Dreamina Seedance 2.5',False),
    ('Do not upgrade to use this feature: Dreamina Seedance 2.5','Dreamina Seedance 2.5',False),
    ('Upgrade to use this feature: Dreamina Seedance 2.5 is not required','Dreamina Seedance 2.5',False),
])
def test_explicit_upgrade_notice_requires_direct_model_statement(text,model,expected):
    assert explicit_model_upgrade_notice(text,model) is expected


@pytest.mark.parametrize('notice,delay_ms',[
    ('Upgrade your plan',350),
    ('Dreamina Seedance 2.5 requires a plan upgrade.',350),
    ('Upgrade your plan',0),
])
def test_late_or_already_observed_generation_receipt_wins_over_subscription_panel(notice,delay_ms):
    async def run():
        spec={'model':'seedance-2.5','prompt':'Synthetic cloudy sky','duration':4,'resolution':'480p','ratio':'16:9','mode':'omni'}
        prepared=build_generation(spec,workspace_id='123456');events=[];requests=[];receipts=[];intents=[]
        onclick="document.querySelector('#upgrade').hidden=false;setTimeout(async()=>{await fetch('https://dreamina-api.us.capcut.com"+GENERATE_PATH+"',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify("+json.dumps(prepared['body'])+")});},"+str(delay_ms)+");"
        html=fixture_html(spec,onclick).replace('Upgrade your plan',notice)
        async with async_playwright() as pw:
            browser,context=await offline_browser(pw)
            async def route_handler(route):
                if route.request.resource_type=='document':
                    await route.fulfill(status=200,content_type='text/html',body=html)
                elif urlsplit(route.request.url).path==GENERATE_PATH:
                    if route.request.method=='OPTIONS':
                        await route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type'})
                    else:
                        requests.append(route.request)
                        await asyncio.sleep(.1)
                        await route.fulfill(status=200,content_type='application/json',headers={'Access-Control-Allow-Origin':'*'},
                            body=json.dumps({'ret':'0','data':{'aigc_data':{'submit_id':prepared['submit_id'],'history_record_id':'123456'}}}))
                else:await route.abort()
            await context.route('**/*',route_handler)
            page=await context.new_page();await page.goto('https://dreamina.capcut.com/ai-tool/generate')
            async def telemetry(value):events.append(value)
            async def intent():intents.append(True)
            driver=NativeUI(page,telemetry,submission_timeout_ms=1500)
            try:
                result=await driver.submit(spec,[],intent,receipts.append)
                assert result['status']=='submitted' and result['submit_id']==prepared['submit_id']
                assert len(receipts)==len(requests)==len(intents)==1
                assert await page.evaluate('window.clicks')==1
                assert await page.evaluate('window.purchases||0')==0
                assert not any(e['stage'] in ('account_plan_required','subscription_panel_unresolved') for e in events)
            finally:await browser.close()
    asyncio.run(run())
