import asyncio,json
from types import SimpleNamespace
import httpx
import pytest
from cryptography.fernet import Fernet
from dreamina_api.egress import parse_proxy_url
from dreamina_api.proxy_health import ProxyHealthManager,probe_proxy
from dreamina_api.platform_store import PlatformStore,PlatformError


def test_proxy_probe_all_requests_explicit_proxy_trust_env_false(monkeypatch):
    calls=[];options=[];real=httpx.AsyncClient
    def factory(**kw):options.append(kw);return real(**kw)
    monkeypatch.setattr(httpx,'AsyncClient',factory)
    def reply(request):
        calls.append(request)
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        if request.url.host=='api64.ipify.org':return httpx.Response(200,json={'ip':'203.0.113.5'})
        if request.url.host=='ipwho.is':return httpx.Response(200,json={'success':True,'ip':'203.0.113.5','country_code':'US','timezone':{'id':'America/New_York'}})
        return httpx.Response(403)
    result=asyncio.run(probe_proxy(parse_proxy_url('http://private-user:private-pass@127.0.0.1:1080'),geo_url='https://ipwho.is/',transport=httpx.MockTransport(reply)))
    assert result['ok'] and result['country']=='US' and result['timezone']=='America/New_York'
    assert len(calls)==3 and all(r.url.scheme=='https' for r in calls)
    assert options[0]['proxy']=='http://private-user:private-pass@127.0.0.1:1080' and options[0]['trust_env'] is False
    assert options[0]['follow_redirects'] is False


@pytest.mark.parametrize('kind,code',[
    ('ip407','proxy_auth_failed'),('ip500',None),('ipinvalid',None),
    ('target407','proxy_auth_failed'),('target500',None),('targetredirect',None),
    ('proxyerror','proxy_auth_failed')])
def test_probe_failures_no_credentials_in_result(kind,code):
    def reply(request):
        if kind=='proxyerror':raise httpx.ProxyError('407 auth for secret-user:secret-password')
        if request.url.host=='api64.ipify.org':
            if kind=='ip407':return httpx.Response(407)
            if kind=='ip500':return httpx.Response(500)
            return httpx.Response(200,json={'ip':'bad' if kind=='ipinvalid' else '203.0.113.6'})
        return httpx.Response(407 if kind=='target407' else 500 if kind=='target500' else 302,headers={'Location':'https://unexpected.example/'})
    result=asyncio.run(probe_proxy(parse_proxy_url('http://secret-user:secret-password@proxy.example:80'),transport=httpx.MockTransport(reply)))
    assert 'secret' not in json.dumps(result)
    if code:assert result=={'ok':False,'error_code':code}
    else:
        assert result['ok'] is True
        if kind in ('ip500','ipinvalid'):assert result['ip_known'] is False and result['warning_code']=='proxy_ip_unavailable'


@pytest.mark.parametrize('geo',[
    {'success':True,'ip':'203.0.113.99','country_code':'JP','timezone':{'id':'Asia/Tokyo'}},
    {'success':False,'ip':'203.0.113.6','country_code':'JP','timezone':{'id':'Asia/Tokyo'}},
    {'success':True,'country_code':'JP','timezone':{'id':'Asia/Tokyo'}},
])
def test_geo_failure_or_mismatched_ip_does_not_invent_geography(geo):
    def reply(request):
        if request.url.host=='api64.ipify.org':return httpx.Response(200,json={'ip':'203.0.113.6'})
        if request.url.host=='ipwho.is':return httpx.Response(200,json=geo)
        return httpx.Response(200)
    result=asyncio.run(probe_proxy(parse_proxy_url('http://proxy.example:80'),geo_url='https://ipwho.is/',transport=httpx.MockTransport(reply)))
    assert result['ok'] and 'country' not in result and 'timezone' not in result


def fixture():
    clock=SimpleNamespace(now=1000.0);s=PlatformStore('sqlite:///:memory:',encryption_key=Fernet.generate_key(),clock=lambda:clock.now)
    p=s.create_proxy({'name':'One','url':'http://proxy.example:80'})
    s.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.5','latency_ms':1,'timezone':'UTC','country':'US'})
    s.create_account_with_proxy({'id':'one','name':'one','session_id':'fixture'},p['id'])
    return s,p,clock


def test_probe_coalesces_and_periodic_fail_recover_preserves_manual_state():
    async def run():
        s,p,clock=fixture();calls=[];outcome={'ok':False,'error_code':'proxy_connect_failed'}
        async def probe(spec):calls.append(spec.server);await asyncio.sleep(.01);return dict(outcome)
        m=ProxyHealthManager(s,probe=probe,clock=lambda:clock.now)
        await asyncio.gather(*(m.test(p['id']) for _ in range(8)));assert len(calls)==1
        for _ in range(2):clock.now+=601;await m.run_once()
        assert s.get_proxy(p['id'])['status']=='failing'
        assert any(a['code']=='proxy_unhealthy' for a in m.latest['alerts'])
        assert s.get_account('one')['enabled']
        outcome.clear();outcome.update(ok=True,exit_ip='203.0.113.5',latency_ms=1,timezone='UTC')
        clock.now+=601;await m.run_once();assert s.get_proxy(p['id'])['status']=='failing'
        clock.now+=601;await m.run_once();assert s.get_proxy(p['id'])['status']=='active'
        s.update_proxy(p['id'],{'status':'disabled'});count=len(calls);clock.now+=601;await m.run_once();assert len(calls)==count
        await m.test(p['id']);assert s.get_proxy(p['id'])['status']=='disabled'
    asyncio.run(run())


def test_expiry_fallback_uses_binding_callback_without_auto_confirmation():
    async def run():
        s,p,clock=fixture();q=s.create_proxy({'name':'Backup','url':'http://backup.example:80'})
        s.record_proxy_probe(q['id'],{'ok':True,'exit_ip':'203.0.113.6','latency_ms':2,'timezone':'UTC','country':'US'})
        s.update_proxy(p['id'],{'backup_proxy_id':q['id'],'fallback_mode':'proxy','expires_at':1001})
        calls=[]
        async def probe(_):return {'ok':True,'exit_ip':'203.0.113.5','latency_ms':1,'timezone':'UTC'}
        async def binder(aid,pid,**kw):calls.append((aid,pid,kw));raise PlatformError('Busy',code='account_busy',status=409)
        m=ProxyHealthManager(s,probe=probe,binder=binder,clock=lambda:clock.now)
        clock.now=1002;result=await m.run_once()
        assert calls==[('one',q['id'],{'confirm_country_change':False,'confirm_capacity_override':False})]
        assert any(a['code']=='proxy_fallback_pending' for a in result['alerts'])
        await m.run_once();assert len(calls)==1
        with pytest.raises(PlatformError) as error:s.resolve_account_egress('one')
        assert error.value.code=='proxy_expired'
    asyncio.run(run())


def test_health_configuration_requires_https_without_credentials():
    s,_,_=fixture()
    for url in ('http://ip.example/','https://secret:password@ip.example/','https://ip.example/#fragment'):
        with pytest.raises(ValueError) as error:ProxyHealthManager(s,config={'PROXY_IP_CHECK_URL':url})
        assert 'secret' not in str(error.value)
    assert ProxyHealthManager(s).geo_url=='https://ipwho.is/'


def test_missing_timezone_blocks_binding_without_faking_host_timezone():
    s,_,_=fixture();p=s.create_proxy({'url':'http://unknown.example:80'})
    s.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.50','latency_ms':1})
    with pytest.raises(PlatformError) as error:s.begin_proxy_change('one',p['id'])
    assert error.value.code=='proxy_timezone_unknown'

def test_shutdown_closes_inflight_probe_without_publishing_false_health():
    async def run():
        s,p,_=fixture();entered=asyncio.Event()
        async def probe(_):entered.set();await asyncio.Future()
        m=ProxyHealthManager(s,probe=probe);test=asyncio.create_task(m.test(p['id']));await entered.wait()
        await m.close()
        with pytest.raises(asyncio.CancelledError):await test
        assert not m._inflight and s.get_proxy(p['id'])['consecutive_failures']==0
    asyncio.run(run())


def test_ip_probe_fallback_and_ip_echo_outage_do_not_stop_reachable_proxy():
    async def run():
        requests=[]
        def reply(request):
            requests.append(str(request.url))
            if request.url.host=='first.example':return httpx.Response(429)
            if request.url.host=='second.example':return httpx.Response(200,json={'ip':'203.0.113.5'})
            return httpx.Response(403)
        result=await probe_proxy(parse_proxy_url('http://proxy.example:80'),ip_urls=['https://first.example/','https://second.example/'],transport=httpx.MockTransport(reply))
        assert result['ok'] and result['ip_known'] and result['exit_ip']=='203.0.113.5'
        assert requests[1:]==['https://first.example/','https://second.example/']
        s,p,_=fixture()
        for _ in range(5):
            result=await probe_proxy(parse_proxy_url('http://proxy.example:80'),ip_urls=['https://first.example/'],transport=httpx.MockTransport(reply))
            assert result['ok'] and result['exit_ip'] is None and result['warning_code']=='proxy_ip_unavailable'
            s.record_proxy_probe(p['id'],result)
        current=s.get_proxy(p['id'])
        assert current['status']=='active' and current['consecutive_failures']==0 and current['exit_ip']=='203.0.113.5' and current['ip_known'] is False
        assert s.resolve_account_egress('one').proxy_id==p['id']
        s.create_account({'id':'new','name':'new'})
        with pytest.raises(PlatformError) as error:s.begin_proxy_change('new',p['id'])
        assert error.value.code=='proxy_ip_unavailable'
        fresh=s.create_proxy({'url':'http://fresh.example:80'});s.record_proxy_probe(fresh['id'],result)
        assert s.get_proxy(fresh['id'])['exit_ip'] is None
        with pytest.raises(PlatformError):s.begin_proxy_change('new',fresh['id'])
    asyncio.run(run())


@pytest.mark.parametrize('kind',[httpx.ProxyError,httpx.ConnectError])
def test_socks_username_password_failure_has_sanitized_auth_code(kind,monkeypatch):
    real_client=httpx.AsyncClient
    def injected_client(**options):
        # This unit test injects the SOCKS transport error; it does not need
        # an installed SOCKS transport or a network connection.
        assert options.pop('proxy').startswith('socks5h://')
        return real_client(**options)
    monkeypatch.setattr(httpx,'AsyncClient',injected_client)
    def reply(_):raise kind('Invalid username/password for private-user:private-pass')
    result=asyncio.run(probe_proxy(parse_proxy_url('socks5://private-user:private-pass@proxy.example:1080'),transport=httpx.MockTransport(reply)))
    assert result=={'ok':False,'error_code':'proxy_auth_failed'}


def test_plural_ip_probe_configuration_has_priority_and_validates_all_urls():
    s,_,_=fixture()
    m=ProxyHealthManager(s,config={'PROXY_IP_CHECK_URLS':'https://one.example/, https://two.example/','PROXY_IP_CHECK_URL':'https://old.example/'})
    assert m.ip_urls==('https://one.example/','https://two.example/')
    legacy=ProxyHealthManager(s,config={'PROXY_IP_CHECK_URL':'https://old.example/'})
    assert legacy.ip_urls[0]=='https://old.example/' and len(legacy.ip_urls)>1
    for value in ('http://bad.example/','https://ok.example/,https://u:secret@bad.example/',''):
        with pytest.raises(ValueError):ProxyHealthManager(s,config={'PROXY_IP_CHECK_URLS':value})


def test_ip_rotation_always_alerts_and_only_explicit_pause_blocks_new_work():
    async def run():
        s,p,clock=fixture()
        s.record_proxy_probe(p['id'],{'ok':True,'exit_ip':'203.0.113.77','latency_ms':1,'timezone':'UTC'})
        manager=ProxyHealthManager(s,clock=lambda:clock.now)
        result=await manager.run_once()
        assert any(a['code']=='proxy_exit_changed' and a['account_id']=='one' for a in result['alerts'])
        assert s.resolve_account_egress('one').proxy_id==p['id']
        s.update_proxy(p['id'],{'ip_change_policy':'pause'});result=await manager.run_once()
        assert any(a['code']=='proxy_exit_changed' for a in result['alerts'])
        with pytest.raises(PlatformError) as error:s.resolve_account_egress('one')
        assert error.value.code=='proxy_exit_changed'
        await manager.close()
    asyncio.run(run())
