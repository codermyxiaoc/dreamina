"""Offline Chromium fixtures for reference selection; no Dreamina requests."""
import asyncio
import json
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from dreamina_api.file_picker import choose_reference_files,ReferencePickerError


def reference_files(tmp_path):
    items=[('first.png',b'first synthetic image'),('second.jpg',b'second synthetic image'),('motion.mp4',b'synthetic video')]
    paths=[]
    for name,data in items:
        path=tmp_path/name;path.write_bytes(data);paths.append(path)
    return paths,items


async def fixture_page(pw,html,received):
    if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
    browser=await pw.chromium.launch(headless=True)
    context=await browser.new_context(offline=True)
    async def route(route):
        if route.request.resource_type=='document':
            await route.fulfill(status=200,content_type='text/html',body=html)
        elif route.request.url=='https://picker.example.test/upload':
            received.append(json.loads(route.request.post_data))
            await route.fulfill(status=200,content_type='application/json',body='{}')
        else:await route.abort()
    await context.route('**/*',route)
    page=await context.new_page();await page.goto('https://picker.example.test/')
    return browser,page


def test_filesystem_picker_supplies_only_explicit_read_only_files_once_and_restores(tmp_path):
    async def run():
        paths,expected=reference_files(tmp_path);received=[]
        html='''<!doctype html><button id="pick">Reference</button><script>
        window.original=window.showOpenFilePicker;
        window.before=Object.getOwnPropertyDescriptor(window,'showOpenFilePicker');
        document.querySelector('#pick').onclick=async()=>{
          window.clicks=(window.clicks||0)+1;
          try{
            const handles=await window.showOpenFilePicker({multiple:true});
            window.handleKeys=handles.map(h=>Object.keys(h).sort());
            window.handleKinds=handles.map(h=>h.kind);
            const second=window.showOpenFilePicker({multiple:true}).then(()=>false,error=>error.name==='AbortError');
            const files=[];
            for(const handle of handles){const file=await handle.getFile();files.push({name:file.name,type:file.type,text:await file.text(),size:file.size});}
            window.secondRejected=await second;
            await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(files)});
            window.finished=true;
          }catch(error){window.failure=error.name;}
        };</script>'''
        async with async_playwright() as pw:
            browser,page=await fixture_page(pw,html,received)
            try:
                result=await choose_reference_files(page,page.locator('#pick'),paths)
                await page.wait_for_function('window.finished===true')
                assert result=={'mechanism':'file-system-access','count':3}
                assert await page.evaluate('window.clicks')==1
                assert await page.evaluate('window.secondRejected') is True
                assert await page.evaluate('window.showOpenFilePicker===window.original') is True
                assert await page.evaluate('window.before.value===Object.getOwnPropertyDescriptor(window,"showOpenFilePicker").value') is True
                assert await page.evaluate('window.handleKeys')==[['getFile','kind','name']]*3
                assert await page.evaluate('window.handleKinds')==['file']*3
                assert len(received)==1
                assert [(f['name'],f['text'].encode()) for f in received[0]]==expected
                assert [f['type'] for f in received[0]]==['image/png','image/jpeg','video/mp4']
                assert str(tmp_path) not in json.dumps(received)
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('native_api',[True,False])
def test_html_filechooser_still_receives_files_in_order_and_restores_picker(tmp_path,native_api):
    async def run():
        paths,expected=reference_files(tmp_path);received=[]
        html='''<!doctype html><button id="pick">Reference</button><input id="files" type="file" multiple hidden><script>
        REMOVE_NATIVE_API
        window.original=window.showOpenFilePicker;
        document.querySelector('#pick').onclick=()=>{window.clicks=(window.clicks||0)+1;document.querySelector('#files').click();};
        document.querySelector('#files').onchange=async event=>{
          const files=[];for(const file of event.target.files)files.push({name:file.name,text:await file.text()});
          await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(files)});window.finished=true;
        };</script>'''.replace('REMOVE_NATIVE_API','' if native_api else 'delete window.showOpenFilePicker;')
        async with async_playwright() as pw:
            browser,page=await fixture_page(pw,html,received)
            try:
                result=await choose_reference_files(page,page.locator('#pick'),paths)
                await page.wait_for_function('window.finished===true')
                assert result=={'mechanism':'html-filechooser','count':3}
                assert await page.evaluate('window.clicks')==1
                assert await page.evaluate('window.showOpenFilePicker===window.original') is True
                assert [(f['name'],f['text'].encode()) for f in received[0]]==expected
            finally:await browser.close()
    asyncio.run(run())


def test_unused_picker_times_out_restores_original_and_does_not_click_twice(tmp_path):
    async def run():
        paths,_=reference_files(tmp_path);received=[]
        html='''<!doctype html><button id="pick" onclick="window.clicks=(window.clicks||0)+1">Reference</button><script>window.original=window.showOpenFilePicker;</script>'''
        async with async_playwright() as pw:
            browser,page=await fixture_page(pw,html,received)
            try:
                with pytest.raises(ReferencePickerError,match='timed out') as error:
                    await choose_reference_files(page,page.locator('#pick'),paths,timeout_ms=1500)
                assert str(tmp_path) not in str(error.value)
                assert 'synthetic image' not in str(error.value)
                assert await page.evaluate('window.clicks')==1
                assert await page.evaluate('window.showOpenFilePicker===window.original') is True
                assert not received
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['filesystem','html'])
def test_single_file_picker_cannot_silently_receive_multiple_files(tmp_path,kind):
    async def run():
        paths,_=reference_files(tmp_path);received=[]
        handler='await window.showOpenFilePicker({multiple:false})' if kind=='filesystem' else "document.querySelector('#files').click()"
        html='''<!doctype html><button id="pick">Reference</button><input type="file" id="files" hidden><script>
        window.original=window.showOpenFilePicker;document.querySelector('#pick').onclick=async()=>{window.clicks=(window.clicks||0)+1;try{HANDLER;}catch(error){window.failure=error.name;}};</script>'''.replace('HANDLER',handler)
        async with async_playwright() as pw:
            browser,page=await fixture_page(pw,html,received)
            try:
                with pytest.raises(ReferencePickerError,match='only one file'):
                    await choose_reference_files(page,page.locator('#pick'),paths)
                assert await page.evaluate('window.clicks')==1
                assert await page.evaluate('window.showOpenFilePicker===window.original') is True
                assert not received
            finally:await browser.close()
    asyncio.run(run())


def test_missing_file_error_does_not_expose_path_or_touch_browser(tmp_path):
    class NoBrowser:
        def __getattr__(self,name):raise AssertionError('Browser must not be touched for unreadable files')
    async def run():
        with pytest.raises(ReferencePickerError,match='could not be read') as error:
            await choose_reference_files(NoBrowser(),None,[tmp_path/'private-reference.png'])
        assert str(tmp_path) not in str(error.value)
        assert 'private-reference' not in str(error.value)
    asyncio.run(run())


def test_cancellation_restores_picker_after_single_click(tmp_path):
    async def run():
        paths,_=reference_files(tmp_path);received=[]
        html='''<!doctype html><button id="pick" onclick="window.clicks=(window.clicks||0)+1">Reference</button><script>window.original=window.showOpenFilePicker;</script>'''
        async with async_playwright() as pw:
            browser,page=await fixture_page(pw,html,received)
            try:
                pending=asyncio.create_task(choose_reference_files(page,page.locator('#pick'),paths))
                await page.wait_for_function('window.clicks===1')
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):await pending
                assert await page.evaluate('window.showOpenFilePicker===window.original') is True
                assert await page.evaluate('window.clicks')==1
                assert not received
            finally:await browser.close()
    asyncio.run(run())
