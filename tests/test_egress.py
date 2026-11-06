import json
import pytest
from dreamina_api.egress import Egress,EgressError,direct_egress,parse_proxy_url

@pytest.mark.parametrize('url,protocol,host,port,user,password',[
    ('http://example.test:80','http','example.test',80,'',''),
    ('HTTPS://user:p%40ss%3Aword@EXAMPLE.test:443','https','example.test',443,'user','p@ss:word'),
    ('socks5://127.0.0.1:1080','socks5h','127.0.0.1',1080,'',''),
    ('socks5h://[2001:db8::1]:1080','socks5h','2001:db8::1',1080,'',''),
    ('http://u:@localhost:1234/','http','localhost',1234,'u',''),
    ('http://u%20name:p%2Fss@proxy.example:3128','http','proxy.example',3128,'u name','p/ss'),
])
def test_strict_proxy_parsing(url,protocol,host,port,user,password):
    x=parse_proxy_url(url);assert (x.protocol,x.host,x.port,x.username,x.password)==(protocol,host,port,user,password)
    assert parse_proxy_url(x.canonical_url)==x
    assert password not in repr(x) if password else True

@pytest.mark.parametrize('url',[
    None,True,'','proxy.test:80','ftp://proxy.test:80','http://proxy.test','http://proxy.test:0',
    'http://proxy.test:65536','http://proxy.test:abc','http://proxy.test:80/path','http://proxy.test:80?q=1','http://proxy.test:80#x',
    'http://127.00.0.1:80','http://999.1.1.1:80','http://[2001:db8::1:80','http://2001:db8::1:80',
    'http://[fe80::1%25eth0]:80','http://user:bad%0Apass@proxy.test:80','http://user:%zz@proxy.test:80','http://:secret@proxy.test:80',
    'http://bad host:80','http://proxy.test:80\nCookie: secret','http://-bad.test:80','http://bad_.test:80','http://a..test:80',
])
def test_invalid_proxy_does_not_echo_secrets(url):
    with pytest.raises(EgressError) as e:parse_proxy_url(url)
    assert 'secret' not in str(e.value) and '%zz' not in str(e.value)

def test_browser_auth_split_and_no_secret_repr():
    x=parse_proxy_url('http://unique-user:unique-pass@proxy.test:3128')
    assert x.browser_proxy()=={'server':'http://proxy.test:3128','username':'unique-user','password':'unique-pass'}
    e=Egress('proxy_'+'a'*32,x.canonical_url,x.browser_proxy(),'proxy-proxy_'+'a'*32,'America/New_York')
    assert 'unique' not in repr(e) and 'unique' not in repr(x)
    assert Egress.from_worker_json(e.to_worker_json())==e

def test_authenticated_socks_refused_for_browser_not_direct_fallback():
    x=parse_proxy_url('socks5://u:secret@127.0.0.1:1080');assert x.protocol=='socks5h'
    with pytest.raises(EgressError) as e:x.browser_proxy()
    assert e.value.code=='browser_proxy_unsupported' and 'secret' not in str(e.value)

def test_worker_context_fail_closed():
    direct=direct_egress();assert Egress.from_worker_json(direct.to_worker_json())==direct
    for change in ({'httpx_proxy':'http://secret:password@proxy.test:80'},{'playwright_proxy':{}},{'group':'primary'},{'locale':'xx'},{'timezone':'Not/AZone'}):
        d=json.loads(direct.to_worker_json());d.update(change)
        with pytest.raises(EgressError):Egress.from_worker_json(json.dumps(d))
    x=parse_proxy_url('http://u:p@proxy.test:80');e=Egress('proxy_'+'a'*32,x.canonical_url,x.browser_proxy(),'proxy-proxy_'+'a'*32)
    d=json.loads(e.to_worker_json());d['playwright_proxy']['password']='different'
    with pytest.raises(EgressError):Egress.from_worker_json(json.dumps(d))
