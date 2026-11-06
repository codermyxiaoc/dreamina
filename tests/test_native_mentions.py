"""Native reference chips and signed-request evidence, wholly offline."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, Error as PlaywrightError

from dreamina_api.browser_driver import NativeUI,UIUnavailable,generation_request
from dreamina_api.payloads import build_generation
from dreamina_api.prompt_mentions import parse_prompt_mentions


REFS=[{'kind':'image','asset_id':'asset_0_'+'a'*24},
      {'kind':'video','asset_id':'asset_1_'+'b'*24},
      {'kind':'image','asset_id':'asset_2_'+'c'*24}]
PROMPT='让@图片2向@图片1走去，参考@视频1，再让@图片2回头。'
GENERATE_URL='https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate'


def spec(prompt=PROMPT):
    return {'model':'seedance-2.0-mini','prompt':prompt,'duration':4,'ratio':'16:9',
            'resolution':'720p','mode':'omni','references':deepcopy(REFS)}


def html(mode='normal'):
    return '''<!doctype html><html><head><meta charset="utf-8"><style>
      #prompt{border:1px solid black;min-height:70px;width:600px}#menu{background:#ddd}
      [role=option]{padding:8px}.node-reference-mention-tag{background:#abc;padding:2px}
    </style></head><body>
    <div id="prompt" role="textbox" aria-label="Prompt" contenteditable="true">old</div>
    <div id="menu" role="listbox" hidden></div>
    <button aria-label="Generate" onclick="window.generations=(window.generations||0)+1">Generate</button>
    <script>
    const refs=REFS,mode=MODE,editor=document.querySelector('#prompt'),menu=document.querySelector('#menu');
    let insertion;
    window.selectedAssets=[];window.opens=0;window.leftCaretBeforeChip=0;
    editor.addEventListener('input',event=>{
      if(event.data!=='@')return;
      window.opens++;insertion=getSelection().getRangeAt(0).cloneRange();
      menu.replaceChildren();menu.hidden=false;
      if(mode==='missing')return;
      const choices=mode==='duplicate'?[...refs,refs[0]]:refs;
      for(const ref of choices){
        const option=document.createElement('div');option.setAttribute('role','option');
        if(ref.kind==='video'){const duration=document.createElement('span');duration.textContent='00:02 ';option.append(duration)}
        const name=document.createElement('span');name.textContent=mode==='wrong'?'other_'+ref.asset_id:ref.asset_id;option.append(name);
        option.addEventListener('mousedown',event=>event.preventDefault());
        option.addEventListener('click',()=>{
          const range=insertion.cloneRange();range.setStart(range.startContainer,range.startOffset-1);range.deleteContents();
          const chip=document.createElement('span');chip.className='node-reference-mention-tag';chip.contentEditable='false';
          const label=document.createElement('span');label.textContent=ref.asset_id;chip.append(label);range.insertNode(chip);
          // Real page behavior observed in the diagnostic: selection stays just
          // before the new chip, so the next literal text must explicitly go end.
          range.setStartBefore(chip);range.collapse(true);getSelection().removeAllRanges();getSelection().addRange(range);
          window.leftCaretBeforeChip++;window.selectedAssets.push(ref.asset_id);menu.hidden=true;editor.focus();
        });
        menu.append(option);
      }
    });
    </script></body></html>'''.replace('REFS',json.dumps(REFS)).replace('MODE',json.dumps(mode))


async def open_fixture(pw,mode='normal'):
    if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright fixture Chromium unavailable')
    browser=await pw.chromium.launch(headless=True)
    context=await browser.new_context(offline=True)
    page=await context.new_page();await page.set_content(html(mode))
    return browser,page


def test_writer_binds_repeated_mixed_mentions_and_restores_caret_after_each_chip():
    async def run():
        async with async_playwright() as pw:
            browser,page=await open_fixture(pw);events=[]
            async def telemetry(value):events.append(value)
            driver=NativeUI(page,telemetry,timeout_ms=1500)
            try:
                await driver.write_prompt(spec());await driver.verify_prompt(spec())
                assert await page.evaluate('window.selectedAssets')==[REFS[2]['asset_id'],REFS[0]['asset_id'],REFS[1]['asset_id'],REFS[2]['asset_id']]
                assert await page.evaluate('window.leftCaretBeforeChip')==4
                assert await page.evaluate('window.opens')==4
                assert await page.locator('#prompt').inner_text()==''.join(s['text'] if s['type']=='text' else s['asset_id'] for s in parse_prompt_mentions(PROMPT,REFS))
                assert events[-1]['stage']=='prompt_mentions_ready'
                assert events[-1]['detail']=={'mention_count':4,'bindings_verified':True}
                assert not driver.clicked and await page.evaluate('window.generations||0')==0
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('prompt',['普通提示词没有引用','email@example.test 与 @someone','图片1不是标签'])
def test_no_alias_uses_plain_text_without_opening_mention_menu(prompt):
    async def run():
        async with async_playwright() as pw:
            browser,page=await open_fixture(pw)
            async def noop(_):pass
            driver=NativeUI(page,noop,timeout_ms=500)
            try:
                await driver.write_prompt(spec(prompt))
                assert await page.locator('#prompt').inner_text()==prompt
                assert await page.evaluate('window.opens')==0
                assert await page.locator('.node-reference-mention-tag').count()==0
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['missing','wrong','duplicate'])
def test_unavailable_or_ambiguous_native_option_stops_before_submission_intent(mode):
    async def run():
        async with async_playwright() as pw:
            browser,page=await open_fixture(pw,mode);intents=[]
            async def noop(*_):pass
            async def intent():intents.append(True)
            driver=NativeUI(page,noop,timeout_ms=250,submission_timeout_ms=50)
            driver.new_session=noop;driver.configure=noop;driver.upload=noop;driver.verify_configuration=noop
            try:
                with pytest.raises((UIUnavailable,PlaywrightError)):
                    await driver.submit(spec('让@图片1走起来'),[],intent)
                assert not intents and not driver.clicked
                assert await page.evaluate('window.generations||0')==0
                assert driver._promotion_guard_depth==0
            finally:await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('change',['text','chip_label','raw_text','missing_chip','extra_chip'])
def test_readback_rejects_changed_literal_text_or_reference_chip(change):
    async def run():
        async with async_playwright() as pw:
            browser,page=await open_fixture(pw)
            async def noop(_):pass
            driver=NativeUI(page,noop,timeout_ms=1500);value=spec('让@图片1走起来')
            try:
                await driver.write_prompt(value)
                await page.locator('#prompt').evaluate('''(editor,change)=>{
                  const chip=editor.querySelector('.node-reference-mention-tag');
                  if(change==='text')editor.append('不同的提示词');
                  if(change==='chip_label')chip.textContent='another_asset';
                  if(change==='raw_text')chip.replaceWith(document.createTextNode('@图片1'));
                  if(change==='missing_chip')chip.remove();
                  if(change==='extra_chip')editor.append(chip.cloneNode(true));
                }''',change)
                with pytest.raises(UIUnavailable,match='tags changed'):await driver.verify_prompt(value)
                assert not driver.clicked
            finally:await browser.close()
    asyncio.run(run())


def request_fixture():
    value=spec();prepared=build_generation({k:v for k,v in value.items() if k!='references'}|{'prompt':'placeholder'})
    body=prepared['body'];draft=json.loads(body['draft_content'])
    item=draft['component_list'][0]['abilities']['gen_video']['text_to_video_params']['video_gen_inputs'][0]
    materials=[{'material_type':r['kind'],r['kind']+'_info':({'image_uri':'uri-'+str(i),'title':r['asset_id']} if r['kind']=='image' else {'vid':'vid-'+str(i)})} for i,r in enumerate(REFS)]
    metas=[]
    for part in parse_prompt_mentions(value['prompt'],REFS):
        if part['type']=='text':metas.append({'meta_type':'text','text':part['text']})
        else:metas.append({'meta_type':part['type'],'text':'','material_ref':{'material_idx':part['material_index']}})
    item['prompt']='';item['unified_edit_input']={'material_list':materials,'meta_list':metas};body['draft_content']=draft
    bindings={r['asset_id']:{'kind':r['kind'],'resource_id':('uri-' if r['kind']=='image' else 'vid-')+str(i)} for i,r in enumerate(REFS)}
    return value,body,item,bindings


def test_matching_request_receipt_keeps_independent_durable_bindings():
    value,body,_,bindings=request_fixture()
    matched=generation_request(GENERATE_URL,'POST',json.dumps(body),value,bindings)
    assert matched and matched['request_summary']['reference_count']==3
    assert matched['request_summary']['prompt']==PROMPT
    assert matched['reference_bindings']==bindings
    bindings[REFS[1]['asset_id']]['resource_id']='changed-after-capture'
    assert matched['reference_bindings'][REFS[1]['asset_id']]['resource_id']=='vid-1'


@pytest.mark.parametrize('mutation',['missing_binding','wrong_vid','wrong_image_uri','wrong_ref_index','raw_alias_text'])
def test_generation_request_rejects_wrong_assets_or_raw_alias_fallback(mutation):
    value,body,item,bindings=request_fixture()
    if mutation=='missing_binding':bindings={}
    elif mutation=='wrong_vid':bindings[REFS[1]['asset_id']]['resource_id']='some-other-video'
    elif mutation=='wrong_image_uri':bindings[REFS[0]['asset_id']]['resource_id']='another-image'
    elif mutation=='wrong_ref_index':item['unified_edit_input']['meta_list'][1]['material_ref']['material_idx']=0
    elif mutation=='raw_alias_text':
        item['prompt']=PROMPT;item['unified_edit_input']['meta_list']=[{'meta_type':'text','text':PROMPT}]
    assert generation_request(GENERATE_URL,'POST',json.dumps(body),value,bindings) is None
