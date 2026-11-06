import asyncio
from dataclasses import replace
from types import SimpleNamespace
import httpx
import pytest

from dreamina_api.config import Settings
from dreamina_api.egress import Egress,direct_egress,parse_proxy_url
from dreamina_api.egress_clients import AccountClientPool,close_account_client_pool
from dreamina_api.client import DreaminaClient,DreaminaError,GENERATE_PATH


class FakeHTTP:
    def __init__(self,**kw):self.options=kw;self.closed=False;self.cookies={}
    async def aclose(self):self.closed=True


def test_pool_separates_account_auth_and_proxy_revision():
    async def run():
        created=[]
        def factory(**kw):x=FakeHTTP(**kw);created.append(x);return x
        pool=AccountClientPool(factory=factory)
        a=Settings(account_id='a',session_id='session-a');b=Settings(account_id='b',session_id='session-b')
        e=direct_egress();one,k1=await pool.acquire(a,e);two,k2=await pool.acquire(b,e)
        assert one is not two;one.cookies['example']='a';assert not two.cookies
        again,ka=await pool.acquire(a,e);assert again is one
        await pool.release(k1,one);await pool.release(ka,again);await pool.release(k2,two)
        a.session_id='renewed';new,kn=await pool.acquire(a,e);assert new is not one and one.closed and not two.closed
        await pool.release(kn,new)
        spec=parse_proxy_url('http://u:password@proxy.example:80');proxied=Egress('proxy_'+'a'*32,spec.canonical_url,spec.browser_proxy(),'proxy-proxy_'+'a'*32,'UTC',revision='1')
        first,kp=await pool.acquire(a,proxied);assert first.options['proxy']==spec.canonical_url and first.options['trust_env'] is False
        changed,kc=await pool.acquire(a,replace(proxied,revision='2'));assert changed is not first and not first.closed
        await pool.release(kp,first);await pool.release(kc,changed)
        same,ks=await pool.acquire(a,replace(proxied,revision='2'));assert same is changed and first.closed
        assert all(x.options['trust_env'] is False and x.options['follow_redirects'] is False for x in created)
        await pool.release(ks,same);await pool.close();assert all(x.closed for x in created)
    asyncio.run(run())


def test_full_pool_uses_ephemeral_client_and_does_not_close_busy_client():
    async def run():
        pool=AccountClientPool(factory=FakeHTTP,maximum=1)
        one,k1=await pool.acquire(Settings(account_id='a'),direct_egress())
        two,k2=await pool.acquire(Settings(account_id='b'),direct_egress());assert k2 is None and not one.closed
        await pool.release(k2,two);assert two.closed and not one.closed
        await pool.invalidate('a');assert not one.closed
        await pool.release(k1,one);await pool.invalidate('a');assert one.closed
    asyncio.run(run())


def test_cookie_headers_stay_account_scoped_and_not_default_client_headers():
    async def run():
        a=DreaminaClient(Settings(account_id='a',session_id='private-a',egress=direct_egress()))
        b=DreaminaClient(Settings(account_id='b',session_id='private-b',egress=direct_egress()))
        async with a, b:
            assert a.http is not b.http
            assert 'cookie' not in a.http.headers and 'authorization' not in a.http.headers
            assert 'private-a' in a.headers('/mweb/v1/example')['Cookie'] and 'private-b' not in a.headers('/mweb/v1/example')['Cookie']
            assert 'private-b' in b.headers('/mweb/v1/example')['Cookie']
        await close_account_client_pool()
    asyncio.run(run())


def test_http_proxy_errors_are_sanitized_and_never_retried_direct():
    async def run():
        calls=[];spec=parse_proxy_url('http://u:secret@proxy.example:80')
        e=Egress('proxy_'+'a'*32,spec.canonical_url,spec.browser_proxy(),'proxy-proxy_'+'a'*32,'UTC')
        async def reply(request):calls.append(request);raise httpx.ProxyError('407 u:secret@proxy.example')
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply),trust_env=False) as h:
            async with DreaminaClient(Settings(session_id='hidden',account_id='a',egress=e),http=h) as client:
                with pytest.raises(DreaminaError) as error:await client.credits()
                assert error.value.code=='proxy_error' and 'secret' not in str(error.value)
                with pytest.raises(DreaminaError) as error:await client.request(GENERATE_PATH,{})
                assert error.value.code=='native_generation_required'
        assert len(calls)==1
    asyncio.run(run())
