"""Explicit account egress; credentials never appear in repr or error messages."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import re
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class EgressError(ValueError):
    def __init__(self,message='Proxy configuration is invalid.',*,code='invalid_proxy',status=400):
        super().__init__(message);self.code=code;self.status=status


def _secret_component(value):
    if value is None:return ''
    if re.search(r'%(?![0-9a-fA-F]{2})',value):raise EgressError('Proxy credentials contain invalid percent encoding.')
    try:value=unquote(value,encoding='utf-8',errors='strict')
    except UnicodeError:raise EgressError('Proxy credentials must use valid UTF-8 percent encoding.') from None
    if len(value)>1024 or any(ord(c)<32 or ord(c)==127 for c in value):raise EgressError('Proxy credentials contain unsupported characters.')
    return value


@dataclass(frozen=True,repr=False)
class ProxySpec:
    protocol:str
    host:str
    port:int
    username:str=field(default='',repr=False)
    password:str=field(default='',repr=False)

    @property
    def server(self):
        host='['+self.host+']' if ':' in self.host else self.host
        return f'{self.protocol}://{host}:{self.port}'

    @property
    def canonical_url(self):
        server=self.server
        if not self.username:return server
        auth=quote(self.username,safe='')+':'+quote(self.password,safe='')+'@'
        return server.replace('://','://'+auth,1)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.canonical_url.encode('utf-8')).hexdigest()

    def browser_proxy(self):
        if self.protocol in ('socks5','socks5h') and self.username:
            raise EgressError('Authenticated SOCKS proxies are not supported by this browser release; use an authenticated HTTP(S) proxy.',code='browser_proxy_unsupported')
        result={'server':self.server.replace('socks5h://','socks5://',1)}
        if self.username:result.update(username=self.username,password=self.password)
        return result

    def __repr__(self):return f'ProxySpec(protocol={self.protocol!r}, host={self.host!r}, port={self.port!r}, authenticated={bool(self.username)!r})'


def parse_proxy_url(text)->ProxySpec:
    if not isinstance(text,str) or not 1<=len(text)<=8192:raise EgressError()
    text=text.strip()
    if not text or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in text):raise EgressError('Proxy URL cannot contain whitespace or control characters.')
    try:parts=urlsplit(text);port=parts.port;host=parts.hostname
    except ValueError:raise EgressError('Proxy URL host, IPv6 brackets or port are invalid.') from None
    scheme=parts.scheme.lower()
    if parts.netloc.count('@')>1:raise EgressError('Reserved characters in proxy credentials must be percent encoded.')
    if scheme not in ('http','https','socks5','socks5h'):raise EgressError('Proxy protocol must be http, https, socks5 or socks5h.')
    if parts.path not in ('','/') or parts.query or parts.fragment:raise EgressError('Proxy URL cannot contain a path, query or fragment.')
    if not host or port is None or not 1<=port<=65535:raise EgressError('Proxy URL requires a host and a port between 1 and 65535.')
    if '%' in host or '\\' in parts.netloc:raise EgressError('Proxy host cannot contain escaped or scoped addresses.')
    try:address=ipaddress.ip_address(host);host=address.compressed
    except ValueError:
        if ':' in host or re.fullmatch(r'[0-9.]+',host):raise EgressError('Proxy IP address is invalid.') from None
        try:host=host.encode('idna').decode('ascii').lower().rstrip('.')
        except UnicodeError:raise EgressError('Proxy hostname is invalid.') from None
        if len(host)>253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in host.split('.')):raise EgressError('Proxy hostname is invalid.')
    username=_secret_component(parts.username);password=_secret_component(parts.password)
    if parts.password is not None and not username:raise EgressError('Proxy password requires a nonempty username.')
    return ProxySpec('socks5h' if scheme=='socks5' else scheme,host,port,username,password)


def valid_timezone(value):
    if value is None or value=='':return None
    if not isinstance(value,str) or len(value)>80:raise EgressError('Browser timezone is invalid.')
    try:ZoneInfo(value)
    except (ZoneInfoNotFoundError,ValueError):raise EgressError('Browser timezone must be an installed IANA timezone.') from None
    return value


@dataclass(frozen=True,repr=False)
class Egress:
    proxy_id:str|None
    httpx_proxy:str|None=field(repr=False)
    playwright_proxy:dict|None=field(repr=False)
    group:str
    timezone:str|None=None
    locale:str='en-US'
    revision:str='direct'

    def __repr__(self):return f'Egress(proxy_id={self.proxy_id!r}, group={self.group!r}, timezone={self.timezone!r}, locale={self.locale!r})'

    def to_worker_json(self):
        return json.dumps({'proxy_id':self.proxy_id,'httpx_proxy':self.httpx_proxy,'playwright_proxy':self.playwright_proxy,
                           'group':self.group,'timezone':self.timezone,'locale':self.locale,'revision':self.revision},separators=(',',':'))

    @classmethod
    def from_worker_json(cls,value):
        if not isinstance(value,str) or len(value)>16384:raise EgressError('Worker egress environment is invalid.')
        try:data=json.loads(value)
        except (ValueError,TypeError):raise EgressError('Worker egress environment is invalid.') from None
        allowed={'proxy_id','httpx_proxy','playwright_proxy','group','timezone','locale','revision'}
        if not isinstance(data,dict) or set(data)-allowed:raise EgressError('Worker egress environment fields are invalid.')
        pid=data.get('proxy_id');url=data.get('httpx_proxy');browser=data.get('playwright_proxy')
        if pid is None:
            if url is not None or browser is not None or data.get('group')!='direct':raise EgressError('Direct egress contains a proxy mismatch.')
        else:
            if not isinstance(pid,str) or not re.fullmatch(r'proxy_[0-9a-f]{32}',pid):raise EgressError('Worker proxy identity is invalid.')
            spec=parse_proxy_url(url)
            if browser!=spec.browser_proxy() or data.get('group')!='proxy-'+pid:raise EgressError('Worker proxy transports do not match.')
            url=spec.canonical_url
        if data.get('locale','en-US')!='en-US':raise EgressError('This browser driver requires en-US.')
        revision=data.get('revision','direct')
        if not isinstance(revision,str) or len(revision)>120:raise EgressError('Worker egress revision is invalid.')
        return cls(pid,url,browser,data['group'],valid_timezone(data.get('timezone')),'en-US',revision)


def direct_egress(*,timezone=None,locale='en-US'):
    if locale!='en-US':raise EgressError('This browser driver requires en-US.')
    return Egress(None,None,None,'direct',valid_timezone(timezone),locale)


def egress_for_account(store,account_id,*,for_existing_task=False)->Egress:
    return store.resolve_account_egress(account_id,for_existing_task=for_existing_task)
