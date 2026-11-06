"""Bounded proxy health checks. Every probe uses the selected proxy explicitly."""
from __future__ import annotations

import asyncio
import ipaddress
import time
from urllib.parse import urlsplit

import httpx

from .db_async import db_call
from .egress import EgressError,ProxySpec


def _https_endpoint(value,label):
    if not isinstance(value,str) or len(value)>2000:raise ValueError(label+' is invalid.')
    try:p=urlsplit(value)
    except ValueError:raise ValueError(label+' is invalid.') from None
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError(label+' must be an HTTPS URL without credentials or fragment.')
    return value


DEFAULT_IP_URLS=('https://api64.ipify.org?format=json','https://api.ipify.org?format=json','https://ipwho.is/')


def _ip_endpoints(value=None,first=None):
    if value is None:values=([first] if first else [])+list(DEFAULT_IP_URLS)
    elif isinstance(value,str):values=[entry.strip() for entry in value.split(',') if entry.strip()]
    elif isinstance(value,(tuple,list)):values=list(value)
    else:raise ValueError('PROXY_IP_CHECK_URLS must contain HTTPS URLs.')
    if not 1<=len(values)<=8:raise ValueError('Configure 1 to 8 proxy IP probes.')
    return tuple(dict.fromkeys(_https_endpoint(v,'Proxy IP probe') for v in values))


def _auth_error(error):
    # SOCKS stacks use several concrete authentication error descriptions.
    # Inspect locally, but never publish exception text or credentials.
    message=str(error).lower()
    return any(value in message for value in ('407','username/password','username password','authentication failed','authentication rejected','invalid username','invalid password'))


async def _response_json(response):
    import json
    body=bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body)>16384:raise ValueError('Probe response too large')
    value=json.loads(body)
    if not isinstance(value,dict):raise ValueError('Probe response must be an object')
    return value


async def probe_proxy(spec:ProxySpec,*,ip_url=None,ip_urls=None,target_url='https://dreamina-api.us.capcut.com/',
                      geo_url=None,transport=None,timeout=15):
    """Probe target TLS and independent IP echo services through one proxy.

    Failed IP echo services are a monitoring warning, not evidence that the
    proxy cannot reach Dreamina. Unknown IP never authorizes a new binding.
    """
    endpoints=_ip_endpoints(ip_urls,ip_url);target_url=_https_endpoint(target_url,'Proxy target probe')
    if geo_url:geo_url=_https_endpoint(geo_url,'Proxy geography probe')
    start=time.monotonic();ip=None
    options={'proxy':spec.canonical_url,'trust_env':False,'follow_redirects':False,'timeout':httpx.Timeout(timeout,connect=min(10,timeout)),
             'headers':{'User-Agent':'DreaminaGatewayProxyProbe/0.13','Accept':'application/json','Accept-Language':'en-US'}}
    if transport is not None:options.update(transport=transport,mounts={'all://':transport})
    try:
        async with httpx.AsyncClient(**options) as client:
            async with client.stream('GET',target_url) as response:
                if response.status_code==407:return {'ok':False,'error_code':'proxy_auth_failed'}
                # Even a target application 5xx proves the configured proxy
                # established TLS. It is not an account/model permission test.
                if not 200<=response.status_code<600:return {'ok':False,'error_code':'proxy_target_unavailable'}
            for endpoint in endpoints:
                try:
                    async with asyncio.timeout(min(5,timeout)):
                        async with client.stream('GET',endpoint) as response:
                            if response.status_code==407:return {'ok':False,'error_code':'proxy_auth_failed'}
                            if response.status_code!=200:continue
                            payload=await _response_json(response)
                            ip=str(ipaddress.ip_address(payload.get('ip',payload.get('query'))))
                            break
                except (httpx.HTTPError,OSError,ValueError,TypeError,KeyError,TimeoutError) as error:
                    if _auth_error(error):return {'ok':False,'error_code':'proxy_auth_failed'}
            result={'ok':True,'exit_ip':ip,'ip_known':ip is not None,'latency_ms':round((time.monotonic()-start)*1000,3)}
            if ip is None:result['warning_code']='proxy_ip_unavailable'
            if geo_url and ip:
                try:
                    async with asyncio.timeout(min(5,timeout)):
                        async with client.stream('GET',geo_url) as response:
                            if response.status_code==200:
                                geo=await _response_json(response)
                                stated=geo.get('ip',geo.get('query'))
                                successful=geo.get('success') is True or geo.get('status')=='success'
                                if successful and str(ipaddress.ip_address(stated))==ip:
                                    country=geo.get('countryCode',geo.get('country_code'))
                                    if isinstance(country,str) and len(country)==2 and country.isascii() and country.isalpha():result['country']=country.upper()
                                    for field in ('region','city','timezone'):
                                        v=geo.get(field)
                                        if field=='timezone' and isinstance(v,dict):v=v.get('id')
                                        if isinstance(v,str) and 0<len(v)<=120:result[field]=v
                except (httpx.HTTPError,OSError,ValueError,TypeError,KeyError,TimeoutError):pass
            return result
    except ImportError:return {'ok':False,'error_code':'proxy_dependency_missing'}
    except (httpx.HTTPError,OSError,TimeoutError) as error:
        return {'ok':False,'error_code':'proxy_auth_failed' if _auth_error(error) else 'proxy_connect_failed'}


class ProxyHealthManager:
    def __init__(self,store,*,config=None,probe=None,binder=None,clock=time.time):
        self.store=store;self.clock=clock;config=dict(config or {});self.binder=binder
        self.ip_urls=_ip_endpoints(config.get('PROXY_IP_CHECK_URLS'),config.get('PROXY_IP_CHECK_URL'))
        self.ip_url=self.ip_urls[0]
        self.target_url=_https_endpoint(config.get('PROXY_TARGET_CHECK_URL','https://dreamina-api.us.capcut.com/'),'PROXY_TARGET_CHECK_URL')
        self.geo_url=config.get('PROXY_GEO_CHECK_URL','https://ipwho.is/') or None
        if self.geo_url:self.geo_url=_https_endpoint(self.geo_url,'PROXY_GEO_CHECK_URL')
        interval=config.get('PROXY_HEALTH_INTERVAL_SECONDS',600)
        if isinstance(interval,bool) or not str(interval).isdigit() or not 60<=int(interval)<=86400:raise ValueError('PROXY_HEALTH_INTERVAL_SECONDS must be 60–86400.')
        self.interval=int(interval);self.probe=probe;self._slots=asyncio.Semaphore(4);self._inflight={};self._fallback_due={}
        self.latest={'state':'not_run','checked':0,'fallbacks':0,'alerts':[]}

    async def test(self,proxy_id):
        # One pending probe per endpoint keeps manual clicks from overwhelming it.
        existing=self._inflight.get(proxy_id)
        if existing is not None and not existing.done():return await asyncio.shield(existing)
        async def operation():
            async with self._slots:
                spec,revision=await db_call(self.store.proxy_probe_context,proxy_id)
                result=await self.probe(spec) if self.probe else await probe_proxy(spec,ip_urls=self.ip_urls,target_url=self.target_url,geo_url=self.geo_url)
                return await db_call(self.store.record_proxy_probe,proxy_id,result,expected_revision=revision)
        task=asyncio.create_task(operation());self._inflight[proxy_id]=task
        def done(_):
            if self._inflight.get(proxy_id) is task:self._inflight.pop(proxy_id,None)
            # A disconnected caller must not leave an unobserved exception
            # containing transport internals in the event-loop error log.
            if not task.cancelled():task.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def run_once(self):
        now=self.clock();proxies=await db_call(self.store.list_proxies);alerts=[];checked=0;fallbacks=0
        due=[p for p in proxies if p['bound_accounts_count'] and p['status']!='disabled' and (p['last_checked_at'] is None or now-p['last_checked_at']>=self.interval)]
        async def check(p):
            try:await self.test(p['id']);return True
            except Exception:return False
        if due:checked=sum(await asyncio.gather(*(check(p) for p in due[:100])))
        proxies=await db_call(self.store.list_proxies)
        for proxy in proxies:
            if proxy.get('warning_code')=='proxy_ip_unavailable':alerts.append({'code':'proxy_ip_unavailable','severity':'warning','proxy_id':proxy['id'],'message':'Dreamina 可达，但出口 IP 探针暂不可用；保留已核验出口，禁止新的账号绑定。'})
            if proxy['bound_accounts_count'] and proxy['exit_ip']:
                for account in await db_call(self.store.proxy_accounts,proxy['id']):
                    if account.get('proxy_exit_ip_at_bind') and account['proxy_exit_ip_at_bind']!=proxy['exit_ip']:
                        paused=proxy['ip_change_policy']=='pause'
                        alerts.append({'code':'proxy_exit_changed','severity':'warning','proxy_id':proxy['id'],'account_id':account['id'],
                            'message':'代理出口 IP 已变化；账号已暂停新调度，等待确认。' if paused else '代理出口 IP 已变化；按当前策略仅告警，继续使用该代理。'})
            if proxy['status']=='failing':alerts.append({'code':'proxy_unhealthy','severity':'warning','proxy_id':proxy['id'],'message':'代理连续检测失败，关联账号已暂停新调度。'})
            if proxy['expires_soon']:alerts.append({'code':'proxy_expiring','severity':'warning','proxy_id':proxy['id'],'message':'代理即将到期。'})
            if proxy['expired']:
                alerts.append({'code':'proxy_expired','severity':'warning','proxy_id':proxy['id'],'message':'代理已到期，不会退回直连。'})
                if proxy['fallback_mode']!='proxy' or not proxy['bound_accounts_count'] or not self.binder:continue
                backup=await db_call(self.store.proxy_fallback_candidate,proxy['id'])
                if not backup:continue
                for account in await db_call(self.store.proxy_accounts,proxy['id']):
                    if now<self._fallback_due.get(account['id'],0):continue
                    self._fallback_due[account['id']]=now+600
                    try:
                        await self.binder(account['id'],backup,confirm_country_change=False,confirm_capacity_override=False)
                        fallbacks+=1
                    except Exception:alerts.append({'code':'proxy_fallback_pending','severity':'warning','proxy_id':proxy['id'],'account_id':account['id'],'message':'备用代理切换等待账号空闲或管理员确认。'})
        self.latest={'state':'completed','checked_at':now,'checked':checked,'fallbacks':fallbacks,'alerts':alerts}
        return dict(self.latest)

    async def run(self):
        while True:
            try:await self.run_once()
            except asyncio.CancelledError:raise
            except Exception:self.latest={'state':'error','checked_at':self.clock(),'checked':0,'fallbacks':0,'alerts':[{'code':'proxy_monitor_error','severity':'warning','message':'代理检测暂不可用；不会退回直连。'}]}
            await asyncio.sleep(60)

    async def close(self):
        pending=list(self._inflight.values())
        for task in pending:task.cancel()
        if pending:await asyncio.gather(*pending,return_exceptions=True)
        self._inflight.clear()
