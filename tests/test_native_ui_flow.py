"""Real Playwright, wholly offline synthetic webpage: no Dreamina traffic/credits."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from dreamina_api.browser_driver import NativeUI, UIUnavailable
import dreamina_api.browser_driver as browser_driver
from dreamina_api.payloads import build_generation


PLACEHOLDER = '<span class="rich-placeholder-widget ProseMirror-widget" contenteditable="false">Describe your idea or use @ to reference a file</span>'


def covered_new_session_html(remaining=''):
    return '''<!doctype html><html><body>
    <div class="project-section-actions" style="position:relative;width:150px;height:40px">
      <button id="new-session" aria-label="New session" style="width:100%;height:100%"
        onclick="window.newSessions=(window.newSessions||0)+1;document.querySelector('#old')?.remove();document.querySelector('#prompt').replaceChildren(document.querySelector('#new-prompt').content.cloneNode(true))">New session</button>
      <span aria-hidden="true" style="position:absolute;inset:0;background:transparent"></span>
    </div>
    <div id="old" data-generated-record-key="old-fixture">Old task</div>
    <div contenteditable="true" role="textbox" aria-label="Prompt" id="prompt">Old prompt</div>
    <template id="new-prompt">''' + PLACEHOLDER + remaining + '</template></body></html>'


def test_new_session_uses_keyboard_through_pointer_overlay_and_preserves_placeholder():
    async def run():
        events=[]
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
            browser=await pw.chromium.launch(headless=True)
            context=await browser.new_context(offline=True)
            page=await context.new_page()
            await page.set_content(covered_new_session_html())
            async def telemetry(value):events.append(value)
            try:
                # Reproduce the real hover container intercepting a normal pointer click.
                with pytest.raises(PlaywrightTimeoutError):
                    await page.get_by_role('button',name='New session',exact=True).click(timeout=200)
                assert await page.evaluate('window.newSessions||0')==0
                await NativeUI(page,telemetry,timeout_ms=1000).new_session()
                assert await page.evaluate('window.newSessions')==1
                assert await page.locator('[data-generated-record-key]').count()==0
                assert await page.locator('#prompt .rich-placeholder-widget.ProseMirror-widget').count()==1
                assert await page.locator('#prompt').text_content()=='Describe your idea or use @ to reference a file'
                assert len(events)==1 and events[0]['stage']=='new_session'
                assert events[0]['detail']=={'empty_editor':True,'existing_records':0,'existing_references':0}
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('remaining',[
    'Real user text',
    '<span class="reference-mention" contenteditable="false">@Image1</span>',
    '<span class="rich-placeholder-widget" contenteditable="false">Not the known placeholder</span>',
])
def test_new_session_does_not_ignore_real_text_or_noneditable_mentions(monkeypatch,remaining):
    async def run():
        events=[]
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
            browser=await pw.chromium.launch(headless=True)
            context=await browser.new_context(offline=True)
            page=await context.new_page()
            await page.set_content(covered_new_session_html(remaining))
            async def telemetry(value):events.append(value)
            # Expire only the driver's empty-session deadline after one real DOM check.
            # Replacing its module reference leaves asyncio's global clock untouched.
            clock=iter([100.0,100.0,121.0])
            monkeypatch.setattr(browser_driver,'time',SimpleNamespace(monotonic=lambda:next(clock),time=lambda:100.0))
            try:
                with pytest.raises(UIUnavailable,match='new empty conversation'):
                    await NativeUI(page,telemetry,timeout_ms=1000).new_session()
                assert await page.evaluate('window.newSessions')==1
                assert await page.locator('[data-generated-record-key]').count()==0
                assert await page.locator('#prompt .rich-placeholder-widget.ProseMirror-widget').count()==1
                assert remaining in await page.locator('#prompt').inner_html()
                assert events==[]
            finally:await browser.close()
    asyncio.run(run())


def test_native_new_session_and_single_submission_without_extension():
    async def run():
        spec={'model':'seedance-2.0-mini','prompt':'Synthetic cloud scene','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni'}
        prepared=build_generation(spec,workspace_id='123456')
        events=[];forwarded=[];click_intents=[]
        html='''<!doctype html><html><body>
        <button aria-label="New session" onclick="document.querySelector('#old').remove();document.querySelector('#prompt').textContent='';window.newSessions=(window.newSessions||0)+1">New session</button>
        <div id="old" data-generated-record-key="old-fixture">Old task</div>
        <div contenteditable="true" role="textbox" aria-label="Prompt" id="prompt">Old prompt</div>
        <div role="combobox" aria-label="Create">AI Video</div>
        <div role="combobox" aria-label="Model">Dreamina Seedance 2.0 Mini</div>
        <div role="combobox">Omni reference</div>
        <button>16:9 720P</button><button>4s</button>
        <button aria-label="Generate" id="generate">Generate</button>
        <script>document.querySelector('#generate').onclick=async()=>{window.clicks=(window.clicks||0)+1;
        await fetch('https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(PAYLOAD)});};</script>
        </body></html>'''.replace('PAYLOAD',json.dumps(prepared['body']))
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
            browser=await pw.chromium.launch(headless=True)
            context=await browser.new_context(offline=True)
            async def route_handler(route):
                if route.request.resource_type=='document':await route.fulfill(status=200,content_type='text/html',body=html)
                elif '/aigc_draft/generate' in route.request.url:
                    forwarded.append(json.loads(route.request.post_data))
                    await route.fulfill(status=200,content_type='application/json',body=json.dumps({'ret':'0','data':{'aigc_data':{'submit_id':prepared['submit_id'],'history_record_id':'999'}}}),headers={'Access-Control-Allow-Origin':'*'})
                else:await route.abort()
            await context.route('**/*',route_handler)
            page=await context.new_page();await page.goto('https://dreamina.capcut.com/ai-tool/generate')
            async def telemetry(values):events.append(values)
            async def intent():click_intents.append(True)
            driver=NativeUI(page,telemetry)
            try:
                assert (await driver.check())['ready']
                result=await driver.submit(spec,[],intent)
                assert result['status']=='submitted' and result['submit_id']==prepared['submit_id']
                assert len(forwarded)==1 and len(click_intents)==1
                assert await page.evaluate('window.newSessions')==1
                assert await page.evaluate('window.clicks')==1
                assert await page.locator('#prompt').inner_text()==spec['prompt']
                assert [e['stage'] for e in events]==['new_session','parameters_ready','generation_response']
                assert all('cookie' not in json.dumps(e).lower() for e in events)
            finally:await browser.close()
    asyncio.run(run())


def test_native_multiple_reference_files_wait_for_image_and_video_commits(tmp_path):
    async def run():
        paths=[]
        for name in ('one.png','two.png','motion.mp4'):
            path=tmp_path/name;path.write_bytes(b'synthetic fixture');paths.append(path)
        events=[];requests=[]
        html='''<!doctype html><input type="file" multiple accept="image/*,video/*" id="files">
        <script>document.querySelector('#files').onchange=async e=>{
          for(const file of e.target.files){
            const action=file.name.endsWith('.mp4')?'CommitUploadInner':'CommitImageUpload';
            await fetch('https://imagex.capcutapi.us/?Action='+action,{method:'POST',body:file.name});
          }
        };</script>'''
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
            browser=await pw.chromium.launch(headless=True);context=await browser.new_context(offline=True)
            async def route_handler(route):
                if route.request.resource_type=='document':return await route.fulfill(status=200,content_type='text/html',body=html)
                requests.append(route.request.post_data)
                result={'Results':[{'Vid':'synthetic-vid'}]} if 'CommitUploadInner' in route.request.url else {'PluginResult':[{'ImageUri':'synthetic-uri'}]}
                await route.fulfill(status=200,content_type='application/json',body=json.dumps({'Result':result}),headers={'Access-Control-Allow-Origin':'*'})
            await context.route('**/*',route_handler)
            page=await context.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            async def telemetry(value):events.append(value)
            try:
                await NativeUI(page,telemetry).upload({'references':[{'kind':'image'},{'kind':'image'},{'kind':'video'}]},paths)
                assert requests==['one.png','two.png','motion.mp4']
                assert events[-1]['stage']=='references_ready'
                assert events[-1]['detail']['commit_counts']=={'image':2,'video':1}
            finally:await browser.close()
    asyncio.run(run())


def test_video_bootstrap_requires_both_successful_first_party_configs():
    async def run():
        events=[]
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
            browser=await pw.chromium.launch(headless=True)
            context=await browser.new_context(offline=True)
            async def route_handler(route):
                if route.request.resource_type=='document':
                    await route.fulfill(status=200,content_type='text/html',body='<!doctype html><body>Offline fixture</body>')
                else:
                    await route.fulfill(status=200,content_type='application/json',body='{"ret":"0","data":{}}',headers={'Access-Control-Allow-Origin':'*'})
            await context.route('**/*',route_handler)
            page=await context.new_page()
            async def telemetry(value):events.append(value)
            driver=NativeUI(page,telemetry,require_bootstrap=True)
            try:
                await page.goto('https://dreamina.capcut.com/fixture')
                first='/mweb/v1/get_common_config'
                second='/mweb/v1/video_generate/get_common_config'
                await page.evaluate('url=>fetch(url).then(response=>response.json())','https://dreamina-api.us.capcut.com'+first)
                await asyncio.gather(*list(driver.bootstrap_tasks))
                assert driver.bootstrap=={first}
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(driver.video_bootstrap(),timeout=.1)
                assert events==[]
                await page.evaluate('url=>fetch(url).then(response=>response.json())','https://dreamina-api.us.capcut.com'+second)
                await asyncio.gather(*list(driver.bootstrap_tasks))
                await asyncio.wait_for(driver.video_bootstrap(),timeout=2)
                assert driver.bootstrap=={first,second}
                assert len(events)==1 and events[0]['stage']=='video_initialization_ready'
                assert events[0]['detail']=={'confirmed_paths':sorted([first,second])}
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('origin,status,body',[
    ('https://dreamina-api.us.capcut.com.attacker.test',200,{'ret':'0'}),
    ('https://unrelated.example.test',200,{'ret':'0'}),
    ('https://dreamina-api.us.capcut.com',200,{'ret':'-6'}),
    ('https://dreamina-api.us.capcut.com',200,{'ret':1}),
    ('https://dreamina-api.us.capcut.com',503,{'ret':'0'}),
    ('https://dreamina-api.us.capcut.com',200,{}),
    ('https://dreamina-api.us.capcut.com',200,[]),
])
def test_video_bootstrap_ignores_wrong_origin_or_unsuccessful_config(origin,status,body):
    async def run():
        events=[]
        first='/mweb/v1/get_common_config'
        second='/mweb/v1/video_generate/get_common_config'
        async with async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
            browser=await pw.chromium.launch(headless=True)
            context=await browser.new_context(offline=True)
            async def route_handler(route):
                if route.request.resource_type=='document':
                    await route.fulfill(status=200,content_type='text/html',body='<!doctype html><body>Offline fixture</body>')
                elif route.request.url.endswith(first):
                    await route.fulfill(status=200,content_type='application/json',body='{"ret":0}',headers={'Access-Control-Allow-Origin':'*'})
                else:
                    await route.fulfill(status=status,content_type='application/json',body=json.dumps(body),headers={'Access-Control-Allow-Origin':'*'})
            await context.route('**/*',route_handler)
            page=await context.new_page()
            async def telemetry(value):events.append(value)
            driver=NativeUI(page,telemetry,require_bootstrap=True)
            try:
                await page.goto('https://dreamina.capcut.com/fixture')
                for url in ['https://dreamina-api.us.capcut.com'+first,origin+second]:
                    await page.evaluate('url=>fetch(url).then(response=>response.json())',url)
                await asyncio.gather(*list(driver.bootstrap_tasks))
                assert driver.bootstrap=={first}
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(driver.video_bootstrap(),timeout=.1)
                assert events==[]
            finally:await browser.close()
    asyncio.run(run())
