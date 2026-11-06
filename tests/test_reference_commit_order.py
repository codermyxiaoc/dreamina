"""Late upload acknowledgements cannot become the next reference's identity."""
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

import dreamina_api.browser_driver as driver_module
from dreamina_api.browser_driver import NativeUI,UIUnavailable


def make_spec(kind):
    label='图片' if kind=='image' else '视频'
    return {'prompt':f'参考@{label}1和@{label}2',
            'references':[{'kind':kind,'asset_id':'asset_0_'+'a'*24},
                          {'kind':kind,'asset_id':'asset_1_'+'b'*24}]}


def upload_html(action,complete_second):
    return '''<!doctype html><html><body><input type="file" multiple accept="image/*,video/*" id="files">
    <button aria-label="Generate" onclick="window.generations=(window.generations||0)+1">Generate</button>
    <script>
    window.uploadNumber=0;
    const send=(session,resource)=>fetch('https://imagex.capcutapi.us/?Action=ACTION',{
      method:'POST',body:JSON.stringify({SessionKey:session,fixtureResource:resource})});
    document.querySelector('#files').onchange=async()=>{
      window.uploadNumber++;
      if(window.uploadNumber===1){await send('first-upload','first-resource');return;}
      // Both an exact retransmission and a late response under another request
      // identity arrive before the second file's successful commit.
      await send('first-upload','first-resource');
      await send('new-request-for-old-resource','first-resource');
      window.staleReceiptsFinished=true;
      if(COMPLETE){await new Promise(resolve=>setTimeout(resolve,900));await send('second-upload','second-resource')}
    };
    </script></body></html>'''.replace('ACTION',action).replace('COMPLETE','true' if complete_second else 'false')


async def run_fixture(tmp_path,kind,complete_second,monkeypatch=None):
    spec=make_spec(kind);paths=[]
    for i in range(2):
        path=tmp_path/(str(i)+('.png' if kind=='image' else '.mp4'));path.write_bytes(b'fixture');paths.append(path)
    action='CommitImageUpload' if kind=='image' else 'CommitUploadInner'
    requested=[];second_replay_time=None
    async with async_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():pytest.skip('Fixture Chromium unavailable')
        browser=await pw.chromium.launch(headless=True)
        try:
            context=await browser.new_context(offline=True)
            async def route_handler(route):
                nonlocal second_replay_time
                if route.request.resource_type=='document':
                    return await route.fulfill(status=200,content_type='text/html',body=upload_html(action,complete_second))
                if route.request.method=='OPTIONS':
                    return await route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type'})
                data=json.loads(route.request.post_data);requested.append(data['SessionKey'])
                if data['SessionKey']=='new-request-for-old-resource':second_replay_time=time.monotonic()
                result={'Results':[{'Vid':data['fixtureResource']}]} if kind=='video' else {'PluginResult':[{'ImageUri':data['fixtureResource']}]}
                await route.fulfill(status=200,content_type='application/json',body=json.dumps({'Result':result}),headers={'Access-Control-Allow-Origin':'*'})
            await context.route('**/*',route_handler)
            page=await context.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            events=[]
            async def telemetry(value):events.append(value)
            driver=NativeUI(page,telemetry)
            if not complete_second:
                # Advance only the driver's upload clock after stale responses,
                # leaving asyncio/Playwright timing unchanged.
                def upload_clock():
                    now=time.monotonic()
                    return now+181 if second_replay_time is not None and now-second_replay_time>.65 else now
                monkeypatch.setattr(driver_module,'time',SimpleNamespace(monotonic=upload_clock,time=time.time))
                with pytest.raises(UIUnavailable,match='not fully confirmed'):
                    await asyncio.wait_for(driver.upload(spec,paths),5)
                assert driver.reference_bindings=={spec['references'][0]['asset_id']:{'kind':kind,'resource_id':'first-resource'}}
                assert len([e for e in events if e['stage']=='references_ready'])==1
            else:
                await asyncio.wait_for(driver.upload(spec,paths),6)
                assert driver.reference_bindings=={
                    spec['references'][0]['asset_id']:{'kind':kind,'resource_id':'first-resource'},
                    spec['references'][1]['asset_id']:{'kind':kind,'resource_id':'second-resource'}}
                assert requested==['first-upload','first-upload','new-request-for-old-resource','second-upload']
                assert len([e for e in events if e['stage']=='references_ready'])==2
            assert not driver.clicked and await page.evaluate('window.generations||0')==0
        finally:await browser.close()


@pytest.mark.parametrize('kind',['image','video'])
def test_late_and_replayed_first_commit_cannot_bind_second_file(tmp_path,kind):
    asyncio.run(run_fixture(tmp_path,kind,True))


@pytest.mark.parametrize('kind',['image','video'])
def test_only_stale_receipts_timeout_before_generation_without_second_binding(tmp_path,monkeypatch,kind):
    asyncio.run(run_fixture(tmp_path,kind,False,monkeypatch))
