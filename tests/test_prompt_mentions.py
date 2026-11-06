"""Reference aliases must bind actual uploads, not merely matching prompt text."""
from copy import deepcopy
import json

import pytest

from dreamina_api.prompt_mentions import has_prompt_mentions,parse_prompt_mentions,verify_prompt_mentions
from dreamina_api.chrome_verify import verify_record
from dreamina_api.payloads import build_generation
from dreamina_api.video_inputs import normalize_options,parse_references,validate_references
from test_openai_api import setup,video_request
from test_video_v4 import png


REFS=[{'kind':'image','asset_id':'image-a'}, {'kind':'video','asset_id':'video-a'},
      {'kind':'image','asset_id':'image-b'}]
PROMPT='让@图片2走向@图片1，参考@视频1，再看@图片2。'


def unified():
    # The native page is allowed to reorder its material table independently of
    # the user's source order. Titles / trusted IDs determine identity.
    return {'material_list':[
        {'material_type':'image','image_info':{'title':'image-b','image_uri':'uri-b'}},
        {'material_type':'image','image_info':{'title':'image-a','image_uri':'uri-a'}},
        {'material_type':'video','video_info':{'title':'video-a','vid':'vid-a'}},
    ],'meta_list':[
        {'meta_type':'text','text':'让'},
        {'meta_type':'image','text':'','material_ref':{'material_idx':0}},
        {'meta_type':'text','text':'走'}, {'meta_type':'text','text':'向'},
        {'meta_type':'image','text':'','material_ref':{'material_idx':1}},
        {'meta_type':'text','text':'，参考'},
        {'meta_type':'video','text':'','material_ref':{'material_idx':2}},
        {'meta_type':'text','text':'，再看'},
        {'meta_type':'image','text':'','material_ref':{'material_idx':0}},
        {'meta_type':'text','text':'。'},
    ]}


def test_numbering_is_independent_by_kind_and_repeat_mentions_keep_order():
    result=parse_prompt_mentions(PROMPT,REFS)
    mentions=[s for s in result if s['type']!='text']
    assert [(s['type'],s['index'],s['material_index'],s['asset_id']) for s in mentions]==[
        ('image',2,2,'image-b'),('image',1,0,'image-a'),('video',1,1,'video-a'),('image',2,2,'image-b')]
    assert ''.join(s['text'] if s['type']=='text' else s['token'] for s in result)==PROMPT
    assert parse_prompt_mentions('@视频1@图片1',REFS)[0]['material_index']==1


@pytest.mark.parametrize('prompt',['普通文字','email@example.com','问 @alice 怎么写','@Image1','@one'])
def test_plain_prompts_and_unreserved_mentions_remain_literal(prompt):
    assert not has_prompt_mentions(prompt)
    assert parse_prompt_mentions(prompt,[])==[{'type':'text','text':prompt}]
    verify_prompt_mentions(prompt,[],None)


@pytest.mark.parametrize('prompt',['@图片0','@图片01','@图片-1','@图片１','@图片1１','@图片1.5',
    '@图片1．5','@视频0','@图片','@视频','@视频 1','@图片3','@视频2','@图片9999999999999999999'])
def test_invalid_reserved_mentions_are_not_silently_plain_text(prompt):
    with pytest.raises(ValueError):parse_prompt_mentions(prompt,REFS)


def test_no_uploaded_reference_rejects_alias_before_task_allocation(tmp_path):
    env=setup(tmp_path)
    response=env.client.post('/v1/videos',json=video_request(prompt='让@图片1走起来'),headers=env.headers)
    assert response.status_code==400 and response.json()['error']['code']=='invalid_video_parameters'
    assert env.store.list_tasks()==[] and env.bridge.calls==[]
    assert env.store.list_keys()[0]['used_requests']==0


def test_out_of_range_uploaded_alias_rejects_before_allowance_or_file_save(tmp_path):
    env=setup(tmp_path)
    response=env.client.post('/v1/videos',data=video_request(prompt='让@图片2走起来'),
        files=[('reference_images',('a.png',png(),'image/png'))],headers=env.headers)
    assert response.status_code==400
    assert env.store.list_tasks()==[] and env.bridge.calls==[]
    assert env.store.list_keys()[0]['used_requests']==0
    assert not list(tmp_path.rglob('*.bin'))


def test_repeated_alias_preserves_raw_prompt_and_one_uploaded_reference(tmp_path):
    env=setup(tmp_path)
    prompt='@图片1 向前走，再让@图片1回头。'
    response=env.client.post('/v1/videos',data=video_request(prompt=prompt),
        files=[('reference_images',('ignored-caller-name.png',png(),'image/png'))],headers=env.headers)
    assert response.status_code==200
    spec=env.bridge.calls[0]['spec']
    assert spec['prompt']==prompt and len(spec['references'])==1
    segments=parse_prompt_mentions(spec['prompt'],spec['references'])
    assert len([s for s in segments if s['type']=='image'])==2


def test_structured_prompt_checks_order_but_accepts_split_text_and_material_reorder():
    verify_prompt_mentions(PROMPT,REFS,unified())


@pytest.mark.parametrize('mutation',['plain_text','wrong_index','missing_mention','extra_mention','wrong_text',
    'negative_index','boolean_index','string_index','unknown_type','unknown_material','duplicate_title','changed_title'])
def test_incorrect_or_unbound_structure_is_rejected(mutation):
    actual=unified()
    if mutation=='plain_text':actual['meta_list']=[{'meta_type':'text','text':PROMPT}]
    elif mutation=='wrong_index':actual['meta_list'][1]['material_ref']['material_idx']=1
    elif mutation=='missing_mention':actual['meta_list'].pop(1)
    elif mutation=='extra_mention':actual['meta_list'].insert(0,deepcopy(actual['meta_list'][1]))
    elif mutation=='wrong_text':actual['meta_list'][0]['text']='不要'
    elif mutation=='negative_index':actual['meta_list'][1]['material_ref']['material_idx']=-1
    elif mutation=='boolean_index':actual['meta_list'][1]['material_ref']['material_idx']=True
    elif mutation=='string_index':actual['meta_list'][1]['material_ref']['material_idx']='0'
    elif mutation=='unknown_type':actual['meta_list'][1]['meta_type']='audio'
    elif mutation=='unknown_material':actual['meta_list'][1]['material_ref']['material_idx']=99
    elif mutation=='duplicate_title':actual['material_list'][0]['image_info']['title']='image-a'
    elif mutation=='changed_title':actual['material_list'][0]['image_info']['title']='different-upload'
    with pytest.raises(ValueError):verify_prompt_mentions(PROMPT,REFS,actual)


def test_video_without_native_title_requires_upload_receipt_identity():
    actual=unified();actual['material_list'][2]['video_info'].pop('title')
    with pytest.raises(ValueError):verify_prompt_mentions(PROMPT,REFS,actual)
    binding={'video-a':{'kind':'video','resource_id':'vid-a'}}
    verify_prompt_mentions(PROMPT,REFS,actual,binding)
    with pytest.raises(ValueError):verify_prompt_mentions(PROMPT,REFS,actual,{'video-a':{'kind':'video','resource_id':'another-vid'}})


def test_two_untitled_videos_can_only_be_distinguished_by_their_upload_receipts():
    refs=[{'kind':'video','asset_id':'one'},{'kind':'video','asset_id':'two'}]
    actual={'material_list':[{'material_type':'video','video_info':{'vid':'second'}},
                              {'material_type':'video','video_info':{'vid':'first'}}],
            'meta_list':[{'meta_type':'video','material_ref':{'material_idx':1}},
                         {'meta_type':'text','text':' then '},{'meta_type':'video','material_ref':{'material_idx':0}}]}
    bindings={'one':{'kind':'video','resource_id':'first'},'two':{'kind':'video','resource_id':'second'}}
    verify_prompt_mentions('@视频1 then @视频2',refs,actual,bindings)
    wrong=deepcopy(bindings);wrong['one']['resource_id']='second'
    with pytest.raises(ValueError):verify_prompt_mentions('@视频1 then @视频2',refs,actual,wrong)


@pytest.mark.parametrize('binding',[[],{'video-a':{'kind':'image','resource_id':'vid-a'}},
    {'video-a':{'kind':'video','resource_id':''}},{'video-a':{'kind':'video','resource_id':1}}])
def test_malformed_upload_binding_is_rejected(binding):
    with pytest.raises(ValueError):verify_prompt_mentions(PROMPT,REFS,unified(),binding)


def test_completed_record_cannot_pass_with_raw_alias_text_and_missing_structured_mentions():
    spec={'prompt':PROMPT,'model':'seedance-2.0-mini','duration':4,'ratio':'16:9','resolution':'720p','mode':'omni'}
    prepared=build_generation({**spec,'prompt':'fixture'})
    draft=json.loads(prepared['body']['draft_content'])
    item=draft['component_list'][0]['abilities']['gen_video']['text_to_video_params']['video_gen_inputs'][0]
    item['prompt']=PROMPT;item['unified_edit_input']=unified()
    job={'submit_id':prepared['submit_id'],'created_at':1000,'request':{**spec,'references':REFS}}
    record={'submit_id':prepared['submit_id'],'created_time':1001,'draft_content':draft}
    verify_record(job,record)
    item['unified_edit_input']['meta_list']=[{'meta_type':'text','text':PROMPT}]
    with pytest.raises(ValueError):verify_record(job,record)


def test_completed_record_recovers_untitled_video_binding_from_durable_job_receipt():
    spec={'prompt':PROMPT,'model':'seedance-2.0-mini','duration':4,'ratio':'16:9','resolution':'720p','mode':'omni'}
    prepared=build_generation({**spec,'prompt':'fixture'})
    draft=json.loads(prepared['body']['draft_content'])
    item=draft['component_list'][0]['abilities']['gen_video']['text_to_video_params']['video_gen_inputs'][0]
    item['prompt']='';item['unified_edit_input']=unified()
    item['unified_edit_input']['material_list'][2]['video_info'].pop('title')
    job={'submit_id':prepared['submit_id'],'created_at':1000,'request':{**spec,'references':REFS},
         'reference_bindings':{'video-a':{'kind':'video','resource_id':'vid-a'}}}
    record={'submit_id':prepared['submit_id'],'created_time':1001,'draft_content':draft}
    verify_record(job,record)
    job.pop('reference_bindings')
    with pytest.raises(ValueError):verify_record(job,record)


def test_validator_preserves_metadata_and_does_not_attach_mutable_binding_state():
    refs=parse_references({},[{'field':'reference_images','filename':'one.png','content_type':'image/png','bytes':png()}])
    spec=normalize_options({'prompt':'让@图片1走起来'})
    before=deepcopy(spec)
    validate_references(spec,refs)
    assert spec==before
