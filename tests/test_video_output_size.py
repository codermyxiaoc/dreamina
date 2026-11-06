"""Completed video sizes describe the downloaded origin, not a tier estimate."""
from copy import deepcopy

import pytest

from dreamina_api.openai_api import video_object


def task(status='completed',result=None,requested=True):
    return {'id':'video_fixture','status':status,'model':'seedance-2.0','created_at':1,
            'request':{'seconds':'4','resolution':'720p',**({'size':'1680x720'} if requested else {})},
            'result':result}


@pytest.mark.parametrize('invalid',[None,True,False,'1470',1470.0,0,-1,float('nan'),float('inf'),-float('inf'),[],{}])
@pytest.mark.parametrize('dimension',['width','height'])
def test_untrusted_output_dimensions_fall_back_to_requested_size(invalid,dimension):
    video={'width':1470,'height':630,dimension:invalid}
    value=video_object(task(result={'videos':[video]}))
    assert value['size']=='1680x720'
    assert value['resolution']=='720p'


@pytest.mark.parametrize('actual',[None,[],['unexpected'],'unexpected',{'videos':None},
    {'videos':{}},{'videos':'unexpected'},{'videos':[]},{'videos':[None]},
    {'videos':[{'height':630}]},{'videos':[{}, {'width':1470,'height':630}]}])
def test_missing_or_malformed_first_origin_preserves_size(actual):
    assert video_object(task(result=actual))['size']=='1680x720'


@pytest.mark.parametrize('status',['queued','claimed','submitted','in_progress','uncertain','failed','cancelled'])
def test_unfinished_or_failed_video_keeps_requested_size(status):
    assert video_object(task(status,result={'videos':[{'width':1470,'height':630}]}))['size']=='1680x720'


def test_actual_size_can_be_returned_when_requested_size_is_absent_without_mutation():
    value=task(result={'videos':[{'width':1470,'height':630}]},requested=False)
    original=deepcopy(value)
    response=video_object(value)
    assert response['size']=='1470x630' and response['resolution']=='720p'
    assert value==original


def test_unknown_requested_and_output_size_is_omitted():
    assert 'size' not in video_object(task(requested=False))
