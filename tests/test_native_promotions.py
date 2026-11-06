"""Offline promotion races and parameter readback; no real website requests."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from dreamina_api.browser_driver import NativeUI, UIUnavailable
from dreamina_api.models import model_config
from dreamina_api.payloads import build_generation


SPEC={'model':'seedance-2.0','prompt':'Synthetic cube','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni','references':[]}
PATH='/mweb/v1/aigc_draft/generate'


def document(*,change_model=False,close_menu=False,late=False,after_generate=False):
    body=build_generation(SPEC,workspace_id='123456')['body']
    current='Dreamina Seedance 2.0 Mini' if change_model else model_config(SPEC['model'])['name']
    return '''<!doctype html><body>
    <button aria-label="New session" onclick="document.querySelector('#prompt').textContent=''">New session</button>
    <div id="prompt" contenteditable="true" role="textbox" aria-label="Prompt"></div>
    <div role="combobox" aria-label="Create" id="create">AI Video</div>
    <button role="combobox" aria-label="Model" id="model">CURRENT</button>
    <div role="combobox" id="mode">Omni reference</div>
    <button id="dimensions">16:9 720P</button><button id="duration">4s</button>
    <button id="generate" aria-label="Generate">Generate</button><button id="neutral">Neutral</button>
    <div role="listbox" id="models" hidden><button role="option" id="target">Dreamina Seedance 2.0</button></div>
    <div role="dialog" id="promo" hidden style="position:fixed;inset:0;background:white;z-index:9999">
      <p>Save up to 40% on plans &amp; Unlock Seedance 2.5</p>
      <button id="offer" onclick="window.purchases=(window.purchases||0)+1">Get offer</button>
      <button id="close">Close</button>
    </div>
    <script>
    const promo=document.querySelector('#promo'), menu=document.querySelector('#models'), target=document.querySelector('#target');
    document.querySelector('#model').onclick=()=>{
      window.opens=(window.opens||0)+1;menu.hidden=false;
      if(window.opens===1){
        if(LATE){target.disabled=true;setTimeout(()=>{promo.hidden=false;target.disabled=false},250)}
        else {promo.hidden=false;}
      }
    };
    document.querySelector('#close').onclick=()=>{window.closes=(window.closes||0)+1;promo.hidden=true;if(CLOSE_MENU)menu.hidden=true};
    target.onclick=()=>{document.querySelector('#model').textContent=target.textContent;menu.hidden=true;window.selections=(window.selections||0)+1};
    document.querySelector('#generate').onclick=async()=>{
      window.generations=(window.generations||0)+1;
      if(AFTER_GENERATE)promo.hidden=false;
      await fetch('https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(PAYLOAD)});
    };
    </script></body>'''.replace('CURRENT',current).replace('PAYLOAD',json.dumps(body)).replace(
        'CLOSE_MENU',str(close_menu).lower()).replace('LATE',str(late).lower()).replace('AFTER_GENERATE',str(after_generate).lower())


@asynccontextmanager
async def fixture(html):
    async with async_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():pytest.skip('Offline Chromium runtime unavailable')
        browser=await pw.chromium.launch(headless=True)
        try:
            ctx=await browser.new_context(offline=True);requests=[]
            async def route(route):
                if urlsplit(route.request.url).path!=PATH:await route.abort();return
                headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type'}
                if route.request.method=='OPTIONS':await route.fulfill(status=204,headers=headers);return
                requests.append(route.request)
                data=json.loads(route.request.post_data)
                await route.fulfill(status=200,headers=headers,content_type='application/json',body=json.dumps(
                    {'ret':'0','data':{'aigc_data':{'submit_id':data['submit_id'],'history_record_id':'123'}}}))
            await ctx.route('**/*',route)
            page=await ctx.new_page();await page.set_content(html)
            yield page,requests
        finally:await browser.close()


@pytest.mark.parametrize('close_menu,late',[(False,False),(True,False),(False,True),(True,True)])
def test_promotion_after_menu_open_and_during_click_wait_is_dismissed_without_purchase(close_menu,late):
    async def run():
        async with fixture(document(change_model=True,close_menu=close_menu,late=late)) as (page,requests):
            events=[];intents=[]
            async def telemetry(value):events.append(value)
            ui=NativeUI(page,telemetry,timeout_ms=4000,submission_timeout_ms=1000)
            async def intent():
                assert ui._promotion_guard_depth==0
                assert not await page.locator('#promo').is_visible()
                intents.append(True)
            result=await ui.submit(SPEC,[],intent)
            assert result['status']=='submitted'
            assert len(intents)==len(requests)==1
            assert await page.evaluate('window.generations')==1
            assert await page.evaluate('window.closes')==1
            assert await page.evaluate('window.selections')==1
            assert await page.evaluate('window.purchases||0')==0
            assert await page.evaluate('window.opens')==(2 if close_menu else 1)
            assert any(e['stage']=='promotion_dismissed' for e in events)
    asyncio.run(run())


@pytest.mark.parametrize('field,value',[
    ('model','Dreamina Seedance 2.0 Fast'),('create','AI Image'),
    ('dimensions','16:9 480P'),('duration','8s'),('mode','First and last frames'),
])
def test_parameter_reset_after_configuration_stops_before_intent_and_removes_handler(field,value):
    async def run():
        async with fixture(document()) as (page,requests):
            async def noop(*_):pass
            ui=NativeUI(page,noop,timeout_ms=300,submission_timeout_ms=200);intents=[]
            async def upload(*_):await page.locator('#'+field).evaluate('(e,value)=>e.textContent=value',value)
            ui.upload=upload
            async def intent():intents.append(True)
            with pytest.raises(UIUnavailable,match='changed during preparation'):
                await ui.submit(SPEC,[],intent)
            assert ui._promotion_guard_depth==0 and not ui.clicked
            assert not intents and not requests
            assert await page.evaluate('window.generations||0')==0
            # A leaked handler would close this new promotion and let the click through.
            await page.locator('#promo').evaluate('(e)=>e.hidden=false')
            with pytest.raises(PlaywrightTimeoutError):await page.locator('#neutral').click(timeout=150)
            assert await page.evaluate('window.closes||0')==0
    asyncio.run(run())


@pytest.mark.parametrize('change',[
    "document.querySelector('#promo p').textContent='Upgrade to Standard';",
    "document.querySelector('#offer').textContent='Subscribe';",
    "document.querySelector('#promo p').textContent='Save up to 40%; security verification';",
    "document.querySelector('#close').remove();",
    "document.querySelector('#promo').append(document.querySelector('#close').cloneNode(true));",
])
def test_unrecognized_verification_or_ambiguous_dialog_is_not_closed_or_purchased(change):
    async def run():
        async with fixture(document()) as (page,requests):
            await page.evaluate('()=>{'+change+"document.querySelector('#promo').hidden=false;}")
            async def noop(*_):pass
            ui=NativeUI(page,noop,timeout_ms=300,submission_timeout_ms=200);intents=[]
            async def intent():intents.append(True)
            with pytest.raises(UIUnavailable):await ui.submit(SPEC,[],intent)
            assert not ui.clicked and not intents and not requests
            assert await page.evaluate('window.closes||0')==0
            assert await page.evaluate('window.purchases||0')==0
            assert await page.locator('#promo').is_visible()
            assert ui._promotion_guard_depth==0
    asyncio.run(run())


def test_promotion_after_activation_is_not_dismissed_and_receipt_is_preserved():
    async def run():
        async with fixture(document(after_generate=True)) as (page,requests):
            async def noop(*_):pass
            ui=NativeUI(page,noop,submission_timeout_ms=1000)
            result=await ui.submit(SPEC,[],noop)
            assert result['status']=='submitted' and len(requests)==1
            assert await page.locator('#promo').is_visible()
            assert await page.evaluate('window.closes||0')==0
            assert await page.evaluate('window.purchases||0')==0
            assert await page.evaluate('window.generations')==1
    asyncio.run(run())


def test_slow_initial_menu_loading_is_waited_for_without_toggling_it_closed():
    async def run():
        html=document(change_model=True).replace(
            'window.opens=(window.opens||0)+1;menu.hidden=false;',
            'window.opens=(window.opens||0)+1;setTimeout(()=>menu.hidden=false,400);return;')
        async with fixture(html) as (page,requests):
            async def noop(*_):pass
            ui=NativeUI(page,noop,timeout_ms=1000,submission_timeout_ms=1000)
            result=await ui.submit(SPEC,[],noop)
            assert result['status']=='submitted' and len(requests)==1
            assert await page.evaluate('window.opens')==1
            assert await page.evaluate('window.selections')==1
            assert await page.evaluate('window.generations')==1
    asyncio.run(run())
