"""Bounded redaction for operator request/event logs. Never log credentials."""
import re
from urllib.parse import urlsplit,urlunsplit

SECRET=re.compile(r'cookie|authorization|sessionid|session_id|token|secret|password|signature|api.?key|x-bogus|x-gnarly|b64|data_url|bytes',re.I)

def sanitize(value, depth=0):
    if depth>12:return '[depth limit]'
    if isinstance(value,dict):return {str(k):('[redacted]' if SECRET.search(str(k)) and not (k in ('completion_tokens','total_tokens','input_tokens','output_tokens','size_bytes','file_size_bytes') and isinstance(v,int)) else sanitize(v,depth+1)) for k,v in list(value.items())[:150]}
    if isinstance(value,(tuple,list)):return [sanitize(x,depth+1) for x in value[:100]]
    if isinstance(value,str):
        if value.startswith('data:'):return '[media data omitted]'
        value=re.sub(r'(?i)((?:https?|socks5h?)://)[^\s/@]+@',r'\1[redacted]@',value)
        if value.startswith(('http://','https://')):
            try:
                p=urlsplit(value);return urlunsplit((p.scheme,p.hostname or '',p.path,'',''))[:2000]
            except ValueError:return '[invalid URL]'
        value=re.sub(r'(?i)Bearer\s+\S+|\bsk-[A-Za-z0-9_-]+','[redacted]',value)
        value=re.sub(r'(?i)(sessionid|cookie|authorization|password|token)\s*[=:]\s*[^\s;,]+',r'\1=[redacted]',value)
        return value[:20000]
    if value is None or isinstance(value,(bool,int,float)):return value
    return '[unsupported value]'
