"""Only the observed first-use content notice may continue one native activation."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from dreamina_api.browser_driver import NativeUI,UIUnavailable,CONTENT_CONFIRMATION_NOTICE
from dreamina_api.payloads import build_generation


PATH='/mweb/v1/aigc_draft/generate'
SPEC={'model':'seedance-2.0-mini','prompt':'A synthetic ceramic cup.','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni','references':[]}


def document(*,mutation='',confirm='send',send_early=False):
    body=build_generation(SPEC,workspace_id='123456')['body']
    html='''<!doctype html><body>
      <button aria-label="New session" onclick="document.querySelector('#prompt').textContent='';window.newSessions=(window.newSessions||0)+1">New session</button>
      <div id="prompt" contenteditable="true" role="textbox" aria-label="Prompt"></div>
      <div role="combobox" aria-label="Create">AI Video</div>
      <div id="model" role="combobox" aria-label="Model">Dreamina Seedance 2.0 Mini</div>
      <div role="combobox">Omni reference</div>
      <button>16:9 720P</button><button>4s</button>
      <button id="generate" aria-label="Generate">Generate</button>
      <div id="notice" role="dialog" aria-label="Before you continue" hidden style="position:fixed;inset:0;z-index:5000;background:white">
        <h2>Before you continue</h2><p>NOTICE</p>
        <button id="cancel" onclick="window.cancels=(window.cancels||0)+1">Cancel</button><button id="confirm">Confirm</button>
      </div>
      <div id="second" role="dialog" aria-label="Other dialog" hidden>Unknown confirmation</div>
      <script>
      const dialog=document.querySelector('#notice');
      const send=()=>fetch('https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(BODY)}).catch(()=>{});
      document.querySelector('#generate').onclick=()=>{window.activations=(window.activations||0)+1;dialog.hidden=false;MUTATION;if(EARLY)send();};
      document.querySelector('#confirm').onclick=()=>{window.confirmations=(window.confirmations||0)+1;if(ACTION==='send'){dialog.hidden=true;send();}};
      </script></body>'''.replace('NOTICE',CONTENT_CONFIRMATION_NOTICE).replace('BODY',json.dumps(body)).replace('MUTATION',mutation).replace('EARLY',str(send_early).lower()).replace('ACTION',json.dumps(confirm))
    return html,body


@asynccontextmanager
async def fixture(html,*,abort=False):
    async with async_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():pytest.skip('Offline browser runtime unavailable')
        browser=await pw.chromium.launch(headless=True)
        try:
            context=await browser.new_context(offline=True)
            requests=[]
            async def route(route):
                if urlsplit(route.request.url).path!=PATH:await route.abort();return
                headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type'}
                if route.request.method=='OPTIONS':await route.fulfill(status=204,headers=headers);return
                body=json.loads(route.request.post_data);requests.append(body)
                if abort:await route.abort();return
                await route.fulfill(status=200,headers=headers,content_type='application/json',body=json.dumps({'ret':'0','data':{'aigc_data':{'submit_id':body['submit_id'],'history_record_id':'123'}}}))
            await context.route('**/*',route)
            page=await context.new_page();await page.set_content(html)
            yield page,requests
        finally:await browser.close()


@pytest.mark.parametrize('abort',[False,True])
def test_exact_content_notice_continues_one_activation_and_persists_original_receipt(abort):
    async def run():
        html,body=document();events=[];receipts=[];intents=[]
        async with fixture(html,abort=abort) as (page,requests):
            async def telemetry(value):events.append(value)
            async def intent():intents.append(True)
            driver=NativeUI(page,telemetry,timeout_ms=3000,submission_timeout_ms=5000)
            result=await driver.submit(SPEC,[],intent,on_request_observed=lambda receipt:receipts.append(receipt))
            assert result['status']==('uncertain' if abort else 'submitted')
            assert result['submit_id']==body['submit_id'] and receipts[0]['submit_id']==body['submit_id']
            assert len(requests)==len(receipts)==len(intents)==1
            assert await page.evaluate('window.activations')==1 and await page.evaluate('window.confirmations')==1
            assert await page.evaluate('window.cancels||0')==0 and driver._content_confirmation_attempted
            assert sum(e['stage']=='content_confirmation' for e in events)==1
    asyncio.run(run())


@pytest.mark.parametrize('mutation',[
    "dialog.querySelector('p').textContent='Different consent text';",
    "dialog.setAttribute('aria-label','Upgrade plan');",
    "dialog.querySelector('p').textContent+=' Upgrade now to pay for this model.';",
    "dialog.querySelector('p').textContent+=' Sign in or solve CAPTCHA.';",
    "document.querySelector('#confirm').textContent='Buy credits';",
    "document.querySelector('#cancel').remove();",
    "dialog.append(document.querySelector('#confirm').cloneNode(true));",
    "document.querySelector('#second').hidden=false;",
    "dialog.insertAdjacentHTML('beforeend','<input type=checkbox aria-label=Extra>');",
    "document.querySelector('#confirm').disabled=true;",
])
def test_unknown_or_ambiguous_confirmation_never_clicks_any_button(mutation):
    async def run():
        html,_=document(mutation=mutation)
        async with fixture(html) as (page,requests):
            async def noop(*_):pass
            driver=NativeUI(page,noop,timeout_ms=200,submission_timeout_ms=300)
            result=await driver.submit(SPEC,[],noop)
            assert result['status']=='uncertain' and not result.get('submit_id')
            assert not driver._content_confirmation_attempted and not requests
            assert await page.evaluate('window.activations')==1
            assert await page.evaluate('window.confirmations||0')==0 and await page.evaluate('window.cancels||0')==0
    asyncio.run(run())


@pytest.mark.parametrize('mutation',[
    "document.querySelector('#prompt').textContent='A different prompt';",
    "document.querySelector('#model').textContent='Dreamina Seedance 2.5';",
])
def test_changed_prompt_or_model_cannot_be_confirmed(mutation):
    async def run():
        html,_=document(mutation=mutation)
        async with fixture(html) as (page,requests):
            async def noop(*_):pass
            driver=NativeUI(page,noop,timeout_ms=500,submission_timeout_ms=1000)
            with pytest.raises(UIUnavailable):await driver.submit(SPEC,[],noop)
            assert not driver._content_confirmation_attempted and not requests
            assert await page.evaluate('window.activations')==1 and await page.evaluate('window.confirmations||0')==0
    asyncio.run(run())


def test_notice_remaining_after_confirmation_is_not_confirmed_again():
    async def run():
        html,_=document(confirm='stay')
        async with fixture(html) as (page,requests):
            async def noop(*_):pass
            driver=NativeUI(page,noop,timeout_ms=1500,submission_timeout_ms=2500)
            result=await driver.submit(SPEC,[],noop)
            assert result['status']=='uncertain' and not requests
            assert await page.evaluate('window.activations')==1 and await page.evaluate('window.confirmations')==1
    asyncio.run(run())


def test_already_observed_request_wins_without_confirmation():
    async def run():
        html,_=document(send_early=True)
        async with fixture(html) as (page,requests):
            async def noop(*_):pass
            driver=NativeUI(page,noop,timeout_ms=300,submission_timeout_ms=800)
            result=await driver.submit(SPEC,[],noop)
            assert result['status']=='submitted' and len(requests)==1
            assert await page.evaluate('window.confirmations||0')==0 and not driver._content_confirmation_attempted
    asyncio.run(run())


def test_click_timeout_is_marked_before_click_and_cannot_retry_when_overlay_disappears():
    async def run():
        html,_=document()
        async with fixture(html) as (page,requests):
            async def noop(*_):pass
            driver=NativeUI(page,noop,timeout_ms=200)
            await (await driver.prompt()).fill(SPEC['prompt'])
            await page.locator('#notice').evaluate("e=>e.hidden=false")
            await page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=block style=\"position:fixed;inset:0;z-index:6000\"></div>')")
            driver.clicked=True
            assert await driver.confirm_content_notice(SPEC,lambda:True,timeout_ms=100) is False
            assert driver._content_confirmation_attempted
            await page.locator('#block').evaluate('e=>e.remove()')
            assert await driver.confirm_content_notice(SPEC,lambda:True,timeout_ms=100) is False
            assert await page.evaluate('window.confirmations||0')==0 and not requests
    asyncio.run(run())


def test_no_initial_activation_or_finished_receipt_cannot_confirm():
    async def run():
        html,_=document()
        async with fixture(html) as (page,requests):
            async def noop(*_):pass
            driver=NativeUI(page,noop);await page.locator('#notice').evaluate('e=>e.hidden=false')
            assert await driver.confirm_content_notice(SPEC,lambda:True,timeout_ms=100) is False
            driver.clicked=True
            assert await driver.confirm_content_notice(SPEC,lambda:False,timeout_ms=100) is False
            assert not driver._content_confirmation_attempted and not requests
    asyncio.run(run())
