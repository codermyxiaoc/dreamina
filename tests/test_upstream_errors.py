from copy import deepcopy
import json
import math

import pytest

from dreamina_api.browser_driver import generation_response
from dreamina_api.upstream_errors import (
    EXPLICIT_REJECTION, normalize_upstream_error, retry_proof, sanitize_upstream_error,
)

REQUEST={'submit_id':'926db625-6b1b-4e7e-a167-441be72d5522'}


def transient(**changes):
    return {'ret':'server_busy','task_accepted':False,'errmsg':'Server busy. Request was not accepted.',**changes}


def test_explicit_transient_rejection_is_bounded_machine_evidence():
    value=normalize_upstream_error(transient(request_id='abcdef0123456789',retry_after_seconds=12),503)
    assert value['category']=='transient' and value['acceptance']=='not_accepted'
    assert value['acceptance_evidence']==EXPLICIT_REJECTION
    assert value['retry_after_seconds']==12 and value['request_id']=='abcdef0123456789'
    assert retry_proof(value)
    assert sanitize_upstream_error(value)==value


@pytest.mark.parametrize('payload',[
    {'ret':'-6'}, {'ret':'-6','errmsg':'shark not pass reject'},
    transient(ret='-6'), transient(ret='-6',error_code='server_busy'),
    transient(ret='2052',errmsg='ErrMessage_APP_OutputAudioRisk'),
    transient(error_code='insufficient_credits'),transient(errmsg='Insufficient balance'),
    transient(errmsg='Captcha required'),transient(errmsg='Session expired'),
    transient(errmsg='Subscription required'),transient(errmsg='Invalid parameter'),
    transient(errmsg='Content policy violation'),transient(errmsg='shark not pass reject'),
])
def test_nonretryable_causes_win_over_transient_fields(payload):
    result=normalize_upstream_error(payload,200)
    assert result['retryable'] is False and not retry_proof(result)


@pytest.mark.parametrize('field,value',[
    ('task_accepted',None),('task_accepted',0),('task_accepted','false'),('task_accepted',True),
    ('ret',0),('ret',True),('ret',None),('ret',{}),('ret','not_documented_error'),
])
def test_missing_or_malformed_nonacceptance_contract_cannot_retry(field,value):
    assert not retry_proof(normalize_upstream_error(transient(**{field:value}),503))


@pytest.mark.parametrize('http_status',[429,500,502,503,504])
def test_http_error_or_empty_history_is_not_nonacceptance_proof(http_status):
    for data in (None,{}, {'ret':'server_busy'}, {'ret':'server_busy','data':{'aigc_data':{}}}):
        result=normalize_upstream_error(data,http_status)
        assert result['acceptance']=='unknown' and not result['retryable']


@pytest.mark.parametrize('conflict',[
    {'accepted':True},{'accepted':None},{'data':{'task_accepted':True}},
    {'history_record_id':'123'}, {'submit_id':REQUEST['submit_id']}, {'data':{'task_id':123}},
    {'data':{'aigc_data':{'history_record_id':'123'}}}, {'data':{'aigc_data':{'error':'ambiguous'}}},
    {'data':{'aigc_data':['malformed']}}, {'data':{'status':'queued'}},
    {'data':{'generation_status':'processing'}}, {'data':{'job':{'id':'x'}}},
])
def test_contradictory_or_malformed_acceptance_hints_prevent_second_submit(conflict):
    data=transient(**conflict)
    parsed=generation_response(REQUEST,200,data)
    assert parsed['status']=='uncertain'
    assert parsed['error_code']=='contradictory_generation_response'
    assert parsed['submit_id']==REQUEST['submit_id']
    assert parsed['upstream_error']['acceptance_conflict'] is True
    assert not retry_proof(parsed['upstream_error'])


def test_success_behavior_remains_exact_identity_checked():
    good={'ret':0,'data':{'aigc_data':{'submit_id':REQUEST['submit_id'],'history_record_id':'123'}}}
    assert generation_response(REQUEST,200,good)['status']=='submitted'
    assert generation_response(REQUEST,503,good)['status']=='uncertain'
    bad=deepcopy(good);bad['data']['aigc_data']['submit_id']='different'
    assert generation_response(REQUEST,200,bad)['status']=='uncertain'


def test_diagnostics_do_not_copy_credentials_urls_names_or_prompt_text():
    secrets=['alice@example.test','John Smith','sk-secret-123456','abcdefsecret','https://private.example/file?signature=secret']
    raw='shark not pass reject; Cookie=sessionid=abcdefsecret; Authorization: Bearer sk-secret-123456; User John Smith alice@example.test; https://private.example/file?signature=secret'
    data={'ret':'-6','errmsg':raw,'cookie':'abcdefsecret','message':'prompt: private creative input',
          'error_code':'sk-secret-123456','request_id':'alice@example.test','data':{'password':'never-copy'}}
    result=generation_response(REQUEST,200,data)
    serialized=json.dumps(result)
    assert result['upstream_error']['message']=='shark not pass reject'
    assert result['upstream_error']['category']=='risk'
    assert len(result['upstream_error']['message_sha256'])==64
    assert all(secret not in serialized for secret in secrets+['private creative input','never-copy'])


def test_unrecognized_text_is_hashed_and_redacted_not_stored_as_prose():
    result=normalize_upstream_error({'ret':'-6','errmsg':'The issue concerns Jane Doe and private footage.'},200)
    assert result['message']=='[unrecognized upstream message redacted]'
    assert 'Jane' not in json.dumps(result)
    assert sanitize_upstream_error(result)==result


@pytest.mark.parametrize('field,value',[
    ('schema_version',True),('schema_version',2),('schema_version','1'),
    ('retryable',1),('retryable','true'),('category','unknown'),
    ('acceptance','unknown'),('acceptance_evidence','missing_history'),
    ('acceptance_conflict',None),('acceptance_conflict',0),('acceptance_conflict',True),
    ('http_status',True),('http_status',float('nan')),('http_status',0),('http_status','503'),
    ('ret','-6'),('error_code','2052'),('message','shark not pass reject'),
    ('message','insufficient credits'),('submit_id',REQUEST['submit_id']),
])
def test_worker_proof_is_revalidated_not_trusted(field,value):
    proof=normalize_upstream_error(transient(),200);proof[field]=value
    assert not retry_proof(proof)


@pytest.mark.parametrize('value',[None,True,0,-2,float('nan'),float('inf'),301,10**10000,'15',{},[]],ids=lambda x:type(x).__name__)
def test_retry_delay_must_be_finite_and_bounded(value):
    proof=normalize_upstream_error(transient(retry_after_seconds=value),200)
    assert 'retry_after_seconds' not in proof


def test_deep_or_cyclic_evidence_fails_closed_and_diagnostics_are_bounded():
    cyclic=transient();cyclic['data']=cyclic
    assert not retry_proof(normalize_upstream_error(cyclic,200))
    long=transient(errmsg='Personal names '*10000)
    encoded=json.dumps(normalize_upstream_error(long,200))
    assert len(encoded)<1000 and 'Personal names' not in encoded


def test_missing_conflict_marker_cannot_upgrade_old_worker_receipt():
    proof=normalize_upstream_error(transient(),200);proof.pop('acceptance_conflict')
    assert not retry_proof(proof)
