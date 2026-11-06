"""Browser LF serialization must not discard the original task receipt."""
import asyncio
from copy import deepcopy
import html as html_module
import json

import pytest

from dreamina_api.browser_driver import NativeUI,UIUnavailable,generation_request
from dreamina_api.chrome_verify import verify_record
from dreamina_api.credit_quote import quote_request_fingerprint
from dreamina_api.payloads import build_generation
from dreamina_api.prompt_mentions import verify_prompt_mentions
from dreamina_api.prompt_text import canonical_prompt_text,prompt_text_equal
from test_driver_reliability import browser_fixture,generation_route,html,fetch_body,success,noop
from test_native_content_confirmation import document,fixture as confirmation_fixture
from test_native_mentions import open_fixture,spec as mention_spec

URL='https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate'


def pair(prompt,actual,*,unified=True,references=True):
    spec={'model':'seedance-2.0-mini','prompt':prompt,'duration':4,'resolution':'720p','ratio':'16:9','mode':'omni',
          'references':[{'kind':'image','asset_id':'image-a'},{'kind':'image','asset_id':'image-b'}] if references else []}
    prepared=build_generation({**spec,'references':[]},workspace_id='123456')
    body=prepared['body'];draft=json.loads(body['draft_content']);params=draft['component_list'][0]['abilities']['gen_video']['text_to_video_params'];item=params['video_gen_inputs'][0]
    item['prompt']='' if unified else actual
    if unified or references:
        item['unified_edit_input']={'material_list':[{'material_type':'image','image_info':{'title':ref['asset_id'],'image_uri':'uri-'+ref['asset_id']}} for ref in spec['references']],
                                  'meta_list':[{'meta_type':'text','text':actual}] if unified else []}
    else:item.pop('unified_edit_input',None)
    body['draft_content']=json.dumps(draft,ensure_ascii=False)
    job={'submit_id':prepared['submit_id'],'created_at':1000,'claimed_at':1000,'request':spec,'telemetry':{'conversation_id':'123456'}}
    record={'submit_id':prepared['submit_id'],'created_time':1001,'workspace_id':'123456','draft_content':draft}
    return spec,body,job,record


@pytest.mark.parametrize('line_ending',['\r\n','\r','\n'])
@pytest.mark.parametrize('unified',[True,False])
def test_long_multiline_reference_prompt_matches_request_and_history(line_ending,unified):
    prompt=line_ending.join(['段落'+str(i)+'：A  B，不改变空格或字符。' for i in range(33)])
    actual=prompt.replace('\r\n','\n').replace('\r','\n')+'\n'
    spec,body,job,record=pair(prompt,actual,unified=unified)
    before=deepcopy(spec);fingerprint=quote_request_fingerprint(spec)
    found=generation_request(URL,'POST',json.dumps(body),spec)
    assert found and found['submit_id']==job['submit_id']
    verify_record(job,record)
    assert spec==before and spec['prompt']==prompt
    assert quote_request_fingerprint(spec)==fingerprint


@pytest.mark.parametrize('mutation',[
    'First line\nSecond  line\n\nThird line',   # a blank line was removed
    'First line\n\nSecond line\n\nThird line', # an interior space was removed
    'First line\n\nChanged  line\n\nThird line',
    'First line\n\nSecond\tline\n\nThird line',
    'First line\n\nSecond\u00a0 line\n\nThird line',
    'First line\n\nSecond  line\n\nThird line\u200b',
])
def test_newline_fix_does_not_accept_changed_prompt_meaning_or_interior_whitespace(mutation):
    prompt='First line\r\n\r\nSecond  line\r\n\r\nThird line'
    spec,body,job,record=pair(prompt,mutation)
    assert generation_request(URL,'POST',json.dumps(body),spec) is None
    with pytest.raises(ValueError,match='parameters'):verify_record(job,record)


def test_line_ending_normalization_never_weakens_other_identity_checks():
    spec,body,job,record=pair('A\r\nB','A\nB')
    original=deepcopy(body)
    for field,value in [('video_aspect_ratio','9:16'),('model_req_key','different')]:
        changed=deepcopy(original);draft=json.loads(changed['draft_content']);draft['component_list'][0]['abilities']['gen_video']['text_to_video_params'][field]=value
        changed['draft_content']=json.dumps(draft)
        assert generation_request(URL,'POST',json.dumps(changed),spec) is None
        with pytest.raises(ValueError):verify_record(job,{**record,'draft_content':draft})
    with pytest.raises(ValueError):verify_record(job,{**record,'workspace_id':'999'})
    with pytest.raises(ValueError):verify_record(job,{**record,'created_time':900})
    with pytest.raises(ValueError):verify_record(job,{**record,'submit_id':'wrong'})


def test_helper_only_normalizes_line_endings_and_existing_outer_trim():
    assert canonical_prompt_text(' A\r\nB\rC\n ')==' A\nB\nC\n '
    assert canonical_prompt_text(' A\r\nB\rC\n ',strip_edges=True)=='A\nB\nC'
    assert prompt_text_equal('A\nB\n','A\r\nB')
    assert not prompt_text_equal('A\nB','A\r\n\r\nB')
    assert not prompt_text_equal('A B','A  B')
    assert not prompt_text_equal('é','e\u0301')
    assert not prompt_text_equal(None,'')
    with pytest.raises(ValueError):canonical_prompt_text(None)


def test_structured_mentions_normalize_text_runs_without_changing_binding_or_spacing():
    refs=[{'kind':'image','asset_id':'image-a'},{'kind':'video','asset_id':'video-a'}]
    prompt='Intro\r\n\r\n@图片1  walks\r\nwith @视频1\r\nEnd'
    actual={'material_list':[{'material_type':'image','image_info':{'title':'image-a','image_uri':'uri-a'}},
                             {'material_type':'video','video_info':{'title':'video-a','vid':'vid-a'}}],
            'meta_list':[{'meta_type':'text','text':'Intro\n\n'},
                         {'meta_type':'image','material_ref':{'material_idx':0}},
                         {'meta_type':'text','text':'  walks\nwith '},
                         {'meta_type':'video','material_ref':{'material_idx':1}},
                         {'meta_type':'text','text':'\nEnd'}]}
    verify_prompt_mentions(prompt,refs,actual)
    changed=deepcopy(actual);changed['meta_list'][2]['text']=' walks\nwith '
    with pytest.raises(ValueError):verify_prompt_mentions(prompt,refs,changed)
    changed=deepcopy(actual);changed['material_list'][0]['image_info']['title']='other-upload'
    with pytest.raises(ValueError):verify_prompt_mentions(prompt,refs,changed)
    changed=deepcopy(actual);changed['meta_list'][1]['material_ref']['material_idx']=1
    with pytest.raises(ValueError):verify_prompt_mentions(prompt,refs,changed)


def test_crlf_split_across_meta_chunks_is_one_newline():
    refs=[{'kind':'image','asset_id':'image-a'}]
    actual={'material_list':[{'material_type':'image','image_info':{'title':'image-a'}}],
            'meta_list':[{'meta_type':'text','text':'Intro\r'},{'meta_type':'text','text':'\n'},
                         {'meta_type':'image','material_ref':{'material_idx':0}}]}
    verify_prompt_mentions('Intro\n@图片1',refs,actual)


def test_real_offline_browser_captures_lf_receipt_for_original_crlf_prompt_once():
    async def run():
        raw='Frame 1\r\n\r\nA ceramic  bird.\r\nFrame 2\r\nThe camera moves.'
        spec,body,job,record=pair(raw,raw.replace('\r\n','\n')+'\n',references=False)
        captures=[];requests=[]
        async with browser_fixture() as context:
            async def reply(route):requests.append(route.request);await success(route)
            await generation_route(context,html('await '+fetch_body(body)+';'),reply)
            page=await context.new_page();await page.goto('https://dreamina.capcut.com/fixture')
            driver=NativeUI(page,noop,submission_timeout_ms=2000)
            result=await driver.submit(spec,[],noop,on_request_observed=captures.append)
            assert result['status']=='submitted' and result['submit_id']==job['submit_id']
            assert len(captures)==len(requests)==1
            assert await page.evaluate('window.clicks')==1
            assert captures[0]['request_summary']['prompt']==raw
            verify_record(job,record)
    asyncio.run(run())


def test_real_offline_mention_verifier_keeps_crlf_prompt_and_native_tags():
    from playwright.async_api import async_playwright
    async def run():
        async with async_playwright() as pw:
            browser,page=await open_fixture(pw)
            try:
                raw='First\r\n@图片2  walks\r\nwith @视频1\r\nEnd'
                request=mention_spec(raw);driver=NativeUI(page,noop,timeout_ms=1500)
                # Model the observed editor serialization: LF / BR text and
                # native reference tags. The fixture is not a rich-text editor.
                content='First<br><span class="node-reference-mention-tag" contenteditable="false">'+html_module.escape(request['references'][2]['asset_id'])+'</span>  walks<br>with <span class="node-reference-mention-tag" contenteditable="false">'+html_module.escape(request['references'][1]['asset_id'])+'</span><br>End'
                await page.locator('#prompt').evaluate('(e,html)=>e.innerHTML=html',content)
                await driver.verify_prompt(request)
                assert request['prompt']==raw
                assert len(await page.locator('.node-reference-mention-tag').all())==2
                await page.locator('#prompt').evaluate("e=>e.append(document.createTextNode('changed'))")
                with pytest.raises(UIUnavailable):await driver.verify_prompt(request)
            finally:await browser.close()
    asyncio.run(run())


def test_known_content_notice_can_confirm_browser_lf_for_crlf_prompt():
    async def run():
        markup,_=document()
        async with confirmation_fixture(markup) as (page,requests):
            raw='A synthetic\r\n\r\nceramic cup.'
            request={'model':'seedance-2.0-mini','prompt':raw,'duration':4,'resolution':'720p','ratio':'16:9','mode':'omni','references':[]}
            await page.locator('#prompt').evaluate("e=>e.style.whiteSpace='pre-wrap'")
            await page.locator('#prompt').evaluate('(e,text)=>e.textContent=text',raw.replace('\r\n','\n'))
            assert await page.locator('#prompt').inner_text()==raw.replace('\r\n','\n')
            await page.locator('#notice').evaluate('e=>e.hidden=false')
            driver=NativeUI(page,noop,timeout_ms=1000);driver.clicked=True
            assert await driver.confirm_content_notice(request,lambda:True,timeout_ms=1000)
            assert await page.evaluate('window.confirmations')==1
    asyncio.run(run())
