"""Official-page fallback in an isolated browser; no user Chrome attachment.

Preparation always blocks paid generation endpoints. A caller must explicitly
set allow_submit=True when invoking request_generation to send one request.
No stealth patches, CAPTCHA solving, token fabrication or automatic retries.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.async_api import async_playwright

from .runtime import GENERATE_PATH, ALLOWED_HOSTS, SignerError, _chromium_executable

PAGE_URL = 'https://dreamina.capcut.com/ai-tool/ai-video'
BROWSER_API_HOSTS = ALLOWED_HOSTS | {'dreamina-api-us-ttp2.us.capcut.com'}
PAID_PATHS = {f'/mweb/v1/{x}' for x in ('aigc_draft/generate','generate_video','batch_generate_video',
              'submit_async_task','creation_agent/v2/conversation','generate','painting','mixblend')}
OBSERVED_HEADER_NAMES = {'appid','appvr','pf','app-sdk-version','loc','lan','did','tdid',
                         'store-country-code','store-country-code-src'}


class DreaminaBrowserSession:
    def __init__(self, session_id: str, *, region: str = 'US', page_url: str = PAGE_URL, cookie_header: str = ''):
        if not session_id or any(x in session_id for x in '\r\n;'):
            raise ValueError('A single nonempty sessionid value is required.')
        if urlsplit(page_url).hostname != 'dreamina.capcut.com':
            raise ValueError('Only the official Dreamina page may be initialized.')
        self._session_id = session_id
        if '\r' in cookie_header or '\n' in cookie_header:
            raise ValueError('Cookie header must not contain newlines.')
        self._cookie_header = cookie_header
        self.region = region
        self.page_url = page_url
        self._playwright = self.browser = self.context = self.page = None
        self._expected_body: str | None = None
        self._allow_submit = False
        self._submission_count = 0
        self._api_origin = None
        self._request_headers = {}
        self.blocked_generation_count = 0
        self.network = Counter()
        self.sdk_network = []
        self.auth_observations = []
        self.page_error_count = 0
        self.sdk_signature_seen = False
        self.ms_token_seen = False
        self._response_tasks = set()

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        try:
            self.browser = await self._playwright.chromium.launch(
                executable_path=_chromium_executable(self._playwright.chromium.executable_path),
                headless=True,
                args=['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],
            )
            # Use the runtime's actual User-Agent; do not hide webdriver.
            self.context = await self.browser.new_context(service_workers='block',locale='en-US',viewport={'width':1440,'height':900})
            values = {name:self._session_id for name in ('sessionid','sessionid_ss','sid_tt')}
            for item in self._cookie_header.split(';'):
                name, separator, value = item.strip().partition('=')
                if separator and re.fullmatch(r'[A-Za-z0-9_!#$%&\x27*+.^`|~-]+', name):
                    values[name] = value
            await self.context.add_cookies([
                {'name':name,'value':value,'domain':'.capcut.com','path':'/',
                 'secure':True,'httpOnly':name.startswith(('sessionid','sid_','uid_')),'sameSite':'None'}
                for name,value in values.items()
            ])
            self._session_id = ''
            self._cookie_header = ''
            await self.context.route('**/*', self._route)
            self.page = await self.context.new_page()
            self.page.on('pageerror',lambda error: setattr(self,'page_error_count',self.page_error_count+1))
            self.page.on('request',self._on_request)
            self.page.on('response',self._on_response)
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self,*args):
        await self.close()

    async def close(self):
        for task in list(self._response_tasks):
            if not task.done(): task.cancel()
        if self._response_tasks:
            await asyncio.gather(*list(self._response_tasks),return_exceptions=True)
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _route(self, route):
        req=route.request; parsed=urlsplit(req.url)
        paid = req.method.upper() == 'POST' and parsed.path in PAID_PATHS
        if paid:
            permitted = (self._allow_submit and self._submission_count == 0
                         and parsed.hostname in BROWSER_API_HOSTS and parsed.path == GENERATE_PATH
                         and req.post_data == self._expected_body)
            if not permitted:
                self.blocked_generation_count += 1
                await route.abort('blockedbyclient')
                return
            self._submission_count += 1
        await route.continue_()

    def _on_request(self, req):
        parsed=urlsplit(req.url)
        if parsed.hostname in BROWSER_API_HOSTS and parsed.path.startswith('/mweb/'):
            # Account-specific values stay in memory and are never reported.
            self._request_headers.update({k:v for k,v in req.headers.items() if k.lower() in OBSERVED_HEADER_NAMES})
            if parsed.path == '/mweb/v1/video_generate/get_common_config' or self._api_origin is None:
                self._api_origin = f'{parsed.scheme}://{parsed.netloc}'
        query=dict(parse_qsl(parsed.query,keep_blank_values=True))
        self.sdk_signature_seen |= bool(query.get('X-Bogus') and query.get('X-Gnarly'))
        self.ms_token_seen |= bool(query.get('msToken'))
        if 'mssdk' in (parsed.hostname or '') and parsed.path in ('/web/resource','/web/report','/web/common'):
            self.sdk_network.append({'kind':'request','host':parsed.hostname,'path':parsed.path,
                                     'has_ms_token':bool(query.get('msToken')),
                                     'has_x_bogus':bool(query.get('X-Bogus')),
                                     'has_x_gnarly':bool(query.get('X-Gnarly'))})

    def _on_response(self, response):
        task=asyncio.create_task(self._inspect_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _inspect_response(self, response):
        try:
            parsed=urlsplit(response.url)
            if parsed.path.startswith(('/mweb/','/passport/','/commerce/')):
                self.network[(parsed.hostname,parsed.path,response.status)] += 1
            if 'mssdk' in (parsed.hostname or '') and parsed.path in ('/web/resource','/web/report','/web/common'):
                headers=await response.all_headers()
                self.ms_token_seen |= bool(headers.get('x-ms-token'))
                self.sdk_network.append({'kind':'response','host':parsed.hostname,'path':parsed.path,
                                         'http_status':response.status,'has_ms_token_header':bool(headers.get('x-ms-token'))})
            # Observe normal page account responses; never return profile data.
            if '/passport/' in parsed.path and any(x in parsed.path for x in ('info','login','account')):
                data=await response.json()
                payload=data.get('data') or {}
                if isinstance(payload,dict):
                    self.auth_observations.append({'path':parsed.path,'http_status':response.status,
                      'business_code':data.get('ret',data.get('code',data.get('status_code'))),
                      'has_user_identity':bool(payload.get('uid') or payload.get('user_id') or payload.get('user_id_str')),
                      'data_keys':[k for k in payload if k in ('uid','user_id','user_id_str','is_login','name','screen_name')]})
        except Exception:
            pass

    async def prepare(self, *, settle_seconds: float = 12) -> dict:
        assert self.page is not None
        navigation_error = None
        try:
            await self.page.goto(self.page_url,wait_until='domcontentloaded',timeout=45000)
        except Exception as exc:
            # Exclude URL/query detail from error output.
            navigation_error = type(exc).__name__
        deadline=time.monotonic()+settle_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(1, max(0,deadline-time.monotonic())))
        if self._response_tasks:
            await asyncio.wait(list(self._response_tasks),timeout=2)
        state=await self.page.evaluate('''() => {
          const sdk=window.byted_acrawler;
          const options=window._mssdk?.cacheOpts?.['513641'];
          const body=(document.body?.innerText||'').slice(0,25000);
          const texts=[...document.querySelectorAll('button,[role="button"]')].map(x=>x.textContent?.trim()).filter(Boolean);
          return {sdk_available:!!sdk,sdk_is_webmssdk:!!sdk?.isWebmssdk,
            sdk_initialized:!!options,sdk_config:options?{aid:options.aid,mode:options.mode,isSDK:options.isSDK,
              region:options.region,apiHost:options.apiHost,dfp:options.dfp,
              slardarConfig:options.slardarConfig,
              generation_path_enabled:(options.enablePathList||[]).includes('/mweb/v1/aigc_draft/generate')}:null,
            page_has_login_button:texts.some(x=>/^(sign in|log in|login|登录)$/i.test(x)),
            page_has_logout_text:/\b(log out|sign out)\b/i.test(body),
            challenge_text_detected:/verify (you are|you're) human|complete the captcha|security verification|drag the slider|验证码|安全验证|拖动滑块/i.test(body),
            challenge_frame_detected:[...document.querySelectorAll('iframe')].some(f=>/captcha|challenge|verify/i.test(f.src)),
            title:document.title,document_ready_state:document.readyState,
            origin:location.origin,path:location.pathname,webdriver:navigator.webdriver,
            user_agent:navigator.userAgent};
        }''')
        logged_in=any(x['has_user_identity'] for x in self.auth_observations)
        challenge = state['challenge_text_detected'] or state['challenge_frame_detected']
        return {'logged_in_observed':logged_in,'login_evidence':self.auth_observations,
          'page':state,'navigation_error':navigation_error,'sdk_network':self.sdk_network,
          'api_origin':self._api_origin,
          'ready_for_submission':bool(logged_in and state['sdk_initialized'] and self.ms_token_seen and not challenge and self._api_origin),
          'sdk_signature_seen':self.sdk_signature_seen,'ms_token_seen':self.ms_token_seen,
          'network_summary':[{'host':h,'path':p,'http_status':s,'count':n} for (h,p,s),n in self.network.items()],
          'page_error_count':self.page_error_count,'generation_requests_sent':self._submission_count,
          'generation_requests_blocked':self.blocked_generation_count,
          'challenge_detected':challenge,
          'browser_type':'isolated headless Chromium; actual UA; webdriver unmodified'}


async def inspect_authenticated_browser(session_id: str, *, region: str='US', settle_seconds: float=12) -> dict:
    async with DreaminaBrowserSession(session_id,region=region) as session:
        return await session.prepare(settle_seconds=settle_seconds)


async def request_generation(url: str, body: str, headers: dict[str,str], session_id: str,
                             *, region: str='US', allow_submit: bool=False, cookie_header: str='') -> dict:
    """Prepare the official page, then optionally send ONE explicitly allowed POST.

    With the default allow_submit=False this only returns preparation diagnostics.
    Transport timeout is an uncertain outcome; never retry automatically.
    """
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname not in BROWSER_API_HOSTS or parsed.path!=GENERATE_PATH:
        raise ValueError('Only the observed Dreamina generation endpoint is accepted.')
    json.loads(body)
    if any(k.lower() in ('x-bogus','x-gnarly','mstoken') for k,v in parse_qsl(parsed.query)):
        raise ValueError('Use an unsigned URL; the official page SDK supplies signatures and token.')
    forbidden={'cookie','host','origin','referer','user-agent','content-length'}
    safe_headers={k:v for k,v in headers.items() if k.lower() not in forbidden and not k.lower().startswith(('sec-','proxy-',':'))}
    async with DreaminaBrowserSession(session_id,region=region,cookie_header=cookie_header) as session:
        readiness=await session.prepare()
        if not allow_submit:
            return {'submitted':False,'readiness':readiness}
        if readiness['challenge_detected']:
            raise SignerError('Official page requires a challenge; no generation submitted.')
        if not readiness['ready_for_submission']:
            raise SignerError('Authenticated official SDK readiness is not established; no generation submitted.')
        # Follow the authenticated page's actual routing (currently US TTP2),
        # without assuming the older HAR hostname remains authoritative.
        origin = urlsplit(readiness['api_origin'])
        url = urlunsplit((origin.scheme,origin.netloc,parsed.path,parsed.query,''))
        current_headers = {k.lower():v for k,v in safe_headers.items()}
        current_headers.update(session._request_headers)
        now = str(int(time.time()))
        appvr = current_headers.get('appvr','8.4.0')
        pf = current_headers.get('pf','7')
        current_headers.update({'device-time':now,'sign-ver':'1',
          'sign':hashlib.md5(f'9e2c|{parsed.path[-7:]}|{pf}|{appvr}|{now}||11ac'.encode()).hexdigest()})
        session._allow_submit=True
        session._expected_body=body
        try:
            response=await asyncio.wait_for(session.page.evaluate('''async ({url,body,headers}) => {
              const response=await fetch(url,{method:'POST',credentials:'include',headers,body});
              const text=await response.text();
              return {status:response.status,body:JSON.parse(text)};
            }''',{'url':url,'body':body,'headers':current_headers}),timeout=60)
        except Exception:
            raise SignerError('Generation transport ended without a confirmed response; query the original submit_id before retrying.') from None
        return {'submitted':True,'status':response['status'],'body':response['body'],
                'api_origin':readiness['api_origin']}
