"""Bounded upstream diagnostics and fail-closed generation retry evidence.

The current Dreamina ``ret=-6`` response is deliberately not retryable. Named
transient codes alone, HTTP 5xx, empty history lookups, and unchanged balances
never establish that a generation was not accepted. The optional retry contract
requires the provider to actually return ``task_accepted: false``; we do not
infer that field, or claim it is present in today's observed Dreamina responses.
"""
from __future__ import annotations

import hashlib
import math
import re

SCHEMA_VERSION = 1
EXPLICIT_REJECTION = 'explicit_task_accepted_false'
TRANSIENT_CODES = frozenset({'server_busy','rate_limited','too_many_requests','service_unavailable'})
_CATEGORIES = frozenset({'unknown','transient','account','permission','verification','credit','parameters','policy','risk'})
_SAFE_CODE = re.compile(r'(?:-?\d{1,10}|[A-Za-z][A-Za-z0-9_]{0,79})\Z')
_REQUEST_ID = re.compile(r'(?:[a-fA-F0-9]{8,64}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})\Z')

# Only recognized diagnostic fragments survive. Arbitrary upstream prose can
# echo prompts, names, URLs or credentials; its digest is enough to correlate
# an unrecognized response until a safe new diagnostic phrase is reviewed.
_PHRASES = (
    ('policy','ErrMessage_APP_OutputAudioRisk',r'\bErrMessage_APP_OutputAudioRisk\b'),
    ('risk','shark not pass reject',r'\bshark\s+not\s+pass\s+reject\b'),
    ('verification','human verification required',r'\b(?:captcha|human\s+verification|verification\s+required)\b'),
    ('account','login required or session expired',r'\b(?:login\s+required|not\s+logged\s+in|invalid\s+session|session\s+expired|unauthorized|authentication\s+failed)\b'),
    ('permission','permission or subscription required',r'\b(?:permission\s+denied|subscription\s+required|upgrade\s+(?:your\s+)?plan|model\s+not\s+allowed)\b'),
    ('credit','insufficient credits',r'\b(?:insufficient\s+(?:credits?|balance)|not\s+enough\s+credits?)\b|积分不足|余额不足'),
    ('policy','content policy rejected',r'\b(?:content\s+(?:policy|moderation)|output\s+audio\s+risk|policy\s+(?:violation|rejected)|safety\s+(?:check|filter|rejection))\b|审核未通过|内容违规'),
    ('parameters','invalid parameter',r'\b(?:invalid\s+(?:argument|parameter)|unsupported\s+(?:parameter|resolution|duration|model))\b'),
    ('transient','server busy',r'\b(?:server\s+busy|service\s+unavailable|too\s+many\s+requests|rate\s+limited|temporarily\s+unavailable)\b'),
    ('unknown','request not accepted',r'\b(?:task|request)\s+(?:was\s+)?not\s+accepted\b'),
)
_CODE_CATEGORIES = {
    '2052':'policy','errmessage_app_outputaudiorisk':'policy',
    'invalid_session':'account','session_expired':'account','unauthorized':'account','login_required':'account',
    'permission_denied':'permission','subscription_required':'permission','model_not_allowed':'permission',
    'captcha_required':'verification','verification_required':'verification',
    'insufficient_credits':'credit','insufficient_balance':'credit',
    'invalid_parameter':'parameters','invalid_argument':'parameters','unsupported_model':'parameters',
    'content_policy_violation':'policy','content_rejected':'policy','output_audio_risk':'policy',
    'shark_not_pass_reject':'risk','risk_rejected':'risk',
}


def _code(value, limit=80):
    if isinstance(value,bool) or not isinstance(value,(str,int)):return None
    try:value=str(value)
    except (ValueError,OverflowError):return None
    if len(value)>limit or not _SAFE_CODE.fullmatch(value):return None
    # Opaque API keys must never be retained in an alleged error-code field.
    if re.search(r'(?i)(?:token|password|cookie|sessionid|authorization|secret|api_?key)',value):return None
    if len(value)>=24 and re.fullmatch(r'[a-fA-F0-9]+',value):return None
    return value


def _message(value):
    if not isinstance(value,str) or not value:return None,None
    value=value[:8192]
    fragments=[]
    for _,safe,pattern in _PHRASES:
        if re.search(pattern,value,re.I) and safe not in fragments:fragments.append(safe)
    return ('; '.join(fragments)[:400] or '[unrecognized upstream message redacted]',hashlib.sha256(value.encode('utf-8',errors='replace')).hexdigest())


def _category(ret, code, message):
    values=[str(x).lower() for x in (ret,code) if x is not None]
    # Nonretryable evidence always wins over a transient label elsewhere.
    categories=[_CODE_CATEGORIES[x] for x in values if x in _CODE_CATEGORIES]
    for category,_,pattern in _PHRASES:
        if category not in ('unknown','transient') and re.search(pattern,message or '',re.I):categories.append(category)
    if categories:return categories[0]
    if ret=='-6':return 'unknown'
    if any(x in TRANSIENT_CODES for x in values):return 'transient'
    return 'unknown'


def _retry_after(value):
    if type(value) not in (int,float) or not 0<value<=300 or not math.isfinite(value):return None
    return max(1,math.ceil(value))


def has_generation_acceptance(data):
    """Any possible generation acceptance blocks replay, even malformed hints."""
    queue=[data];seen=set();count=0
    while queue:
        current=queue.pop();count+=1
        if count>100:return True
        if not isinstance(current,(dict,list)):continue
        if id(current) in seen:return True
        seen.add(id(current))
        if isinstance(current,list):
            queue.extend(current[:101]);continue
        for key,value in current.items():
            if key in ('submit_id','history_record_id','task_id','job_id','generation_id') and value not in (None,''):return True
            if key in ('accepted','task_accepted') and value is not False:return True
            if key in ('task_status','generation_status','status') and isinstance(value,str) and value.lower() in ('submitted','accepted','queued','processing','running','completed','succeeded'):return True
            if key in ('aigc_data','task','generation','job') and value not in (None,{},[], ''):return True
            if isinstance(value,(dict,list)):queue.append(value)
    return False


def normalize_upstream_error(data, http_status):
    """Extract safe fields from the original JSON; do not retain arbitrary data."""
    source=data if isinstance(data,dict) else {}
    status=http_status if type(http_status) is int and 100<=http_status<=599 else None
    ret=_code(source.get('ret'),40)
    nested=source.get('error') if isinstance(source.get('error'),dict) else {}
    error_code=_code(source.get('error_code',source.get('code',nested.get('code'))))
    messages=[source.get('errmsg'),source.get('message'),nested.get('message')]
    raw='; '.join(x[:8192] for x in messages if isinstance(x,str))[:8192]
    message,digest=_message(raw)
    category=_category(ret,error_code,raw)
    conflict=has_generation_acceptance(source)
    explicit=source.get('task_accepted') is False and ret not in (None,'','0') and not conflict
    result={'schema_version':SCHEMA_VERSION,'ret':ret,'http_status':status,'category':category,
            'acceptance':'not_accepted' if explicit else 'unknown',
            'acceptance_evidence':EXPLICIT_REJECTION if explicit else None,
            'retryable':bool(explicit and category=='transient' and ret!='-6' and status is not None),
            'acceptance_conflict':bool(conflict)}
    if message:result.update(message=message,message_sha256=digest)
    if error_code:result['error_code']=error_code
    identifier=source.get('request_id',source.get('requestId',source.get('log_id')))
    if isinstance(identifier,str) and _REQUEST_ID.fullmatch(identifier):result['request_id']=identifier
    delay=_retry_after(source.get('retry_after_seconds',source.get('retry_after')))
    if delay is not None:result['retry_after_seconds']=delay
    return result


def sanitize_upstream_error(value):
    """Canonicalize a worker receipt; never trust its category/retryable flag.

    This does not create a new proof. Only the exact schema and explicit marker
    produced while parsing the raw response can retain a not-accepted claim.
    The authenticated worker/claim and task ownership are checked separately.
    """
    if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version']!=SCHEMA_VERSION:return None
    status=value.get('http_status');status=status if type(status) is int and 100<=status<=599 else None
    ret=_code(value.get('ret'),40);code=_code(value.get('error_code'))
    message,_=_message(value.get('message'))
    category=_category(ret,code,message)
    conflict=value.get('acceptance_conflict') is not False or has_generation_acceptance(value)
    explicit=(value.get('acceptance')=='not_accepted' and value.get('acceptance_evidence')==EXPLICIT_REJECTION
              and ret not in (None,'','0') and status is not None and not conflict)
    allowed=explicit and category=='transient' and ret!='-6' and value.get('retryable') is True and value.get('category')=='transient'
    result={'schema_version':SCHEMA_VERSION,'ret':ret,'http_status':status,'category':category,
            'acceptance':'not_accepted' if explicit else 'unknown','acceptance_evidence':EXPLICIT_REJECTION if explicit else None,'retryable':bool(allowed),
            'acceptance_conflict':bool(conflict)}
    if message:result['message']=message
    digest=value.get('message_sha256')
    if isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{64}',digest):result['message_sha256']=digest
    if code:result['error_code']=code
    identifier=value.get('request_id')
    if isinstance(identifier,str) and _REQUEST_ID.fullmatch(identifier):result['request_id']=identifier
    delay=_retry_after(value.get('retry_after_seconds'))
    if delay is not None:result['retry_after_seconds']=delay
    return result


def retry_proof(value):
    """Whether a canonical diagnosis allows a separately bounded retry decision."""
    clean=sanitize_upstream_error(value)
    return bool(clean and clean['retryable'] is True)
