"""HTTP transport with optional offline SDK signing; no automatic POST retries."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .egress import direct_egress,parse_proxy_url,Egress
from .egress_clients import account_client_pool

GENERATE_PATH = '/mweb/v1/aigc_draft/generate'
HISTORY_PATH = '/mweb/v1/get_history_by_ids'
API_ORIGINS = {'https://dreamina-api.us.capcut.com', 'https://dreamina-api-us-ttp2.us.capcut.com', 'https://mweb-api-sg.capcut.com'}
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36')


class DreaminaError(Exception):
    def __init__(self, message: str, *, code: str = 'upstream_error', status: int = 502):
        super().__init__(message)
        self.code, self.status = code, status


class SubmissionUncertain(DreaminaError):
    def __init__(self, submit_id: str):
        super().__init__('Submission outcome is unknown. Query this submit_id before submitting again.',
                         code='submission_uncertain', status=504)
        self.submit_id = submit_id


def make_sign(path: str, timestamp: int | str, *, appvr: str = '8.4.0', pf: str = '7') -> str:
    return hashlib.md5(f'9e2c|{path[-7:]}|{pf}|{appvr}|{timestamp}||11ac'.encode()).hexdigest()


def _cookie(settings: Settings) -> str:
    if settings.cookie:
        value = settings.cookie
    else:
        sid = settings.session_id
        if not sid:
            raise DreaminaError('Configure DREAMINA_SESSION_ID in the local .env file.',
                               code='missing_session', status=401)
        if any(c in sid for c in ';\r\n'):
            raise ValueError('DREAMINA_SESSION_ID must be one cookie value, not a Cookie header.')
        idc = 'useast5' if settings.region == 'US' else 'sg1'
        value = f'sessionid={sid}; sessionid_ss={sid}; sid_tt={sid}; store-idc={idc}; cc-target-idc={idc}'
    if '\r' in value or '\n' in value:
        raise ValueError('Cookie must not contain newlines.')
    return value


def normalize_record(record: dict, submit_id: str) -> dict:
    status = record.get('status')
    task = record.get('task') or {}
    if status is None:
        status = task.get('status')
    videos = []
    for item in record.get('item_list') or []:
        video = item.get('video') or {}
        origin = (video.get('transcoded_video') or {}).get('origin') or {}
        url = origin.get('video_url')
        if url:
            videos.append({'url': url, 'width': origin.get('width'), 'height': origin.get('height'),
                           'fps': origin.get('fps'), 'duration_ms': video.get('duration_ms'),
                           'size': origin.get('size'), 'format': origin.get('format'),
                           'watermark': origin.get('logo_type')})
    if status == 50 and videos:
        state = 'completed'
    elif status in (10, 30, 40):
        state = 'failed'
    elif status == 100:
        state = 'deleted'
    elif status in (20, 42, 45, 50):
        state = 'processing'
    else:
        state = 'unknown'
    failure_message = record.get('fail_starling_message')
    if failure_message in (None, '', '[none]'):
        failure_message = record.get('fail_starling_key') or 'Video generation failed.'
    return {'id': submit_id, 'object': 'video', 'status': state, 'upstream_status': status,
            'history_record_id': record.get('history_record_id'), 'videos': videos,
            'failure': {'code': record.get('fail_code') or (task.get('resp_ret') or {}).get('ret'),
                        'message': failure_message, 'reason_key': record.get('fail_starling_key')}
                       if state == 'failed' else None}


class DreaminaClient:
    def __init__(self, settings: Settings | None = None, *, http: httpx.AsyncClient | None = None):
        self.settings = settings or Settings.load()
        if self.settings.region not in ('US', 'SG', 'JP', 'HK'):
            raise ValueError('Supported routing regions: US, SG, JP, HK. Only US has captured evidence.')
        self.region = self.settings.region
        self.api_base = 'https://dreamina-api.us.capcut.com' if self.region == 'US' else 'https://mweb-api-sg.capcut.com'
        if self.settings.api_origin:
            if self.settings.api_origin not in API_ORIGINS:
                raise ValueError('DREAMINA_API_ORIGIN must be an observed Dreamina API origin.')
            self.api_base = self.settings.api_origin
        self.commerce_base = 'https://commerce.us.capcut.com' if self.region == 'US' else 'https://commerce-api-sg.capcut.com'
        # Session credentials are deliberately NOT default client headers: upload/download hosts differ.
        self.egress=getattr(self.settings,'egress',None)
        if self.egress is None:
            if self.settings.proxy:
                spec=parse_proxy_url(self.settings.proxy)
                # Legacy standalone HTTP clients retain explicit proxy support.
                self.egress=Egress(None,spec.canonical_url,None,'legacy-explicit')
            else:self.egress=direct_egress()
        self._pooled=http is None and bool(getattr(self.settings,'account_id',''))
        self._pool=None;self._pool_key=None
        self.http = http or (None if self._pooled else httpx.AsyncClient(timeout=httpx.Timeout(60, connect=25),
                                            proxy=self.egress.httpx_proxy,trust_env=False,follow_redirects=False))
        self._owns_http = http is None

    async def __aenter__(self):
        if self.http is None:
            self._pool=account_client_pool();self.http,self._pool_key=await self._pool.acquire(self.settings,self.egress)
        return self

    async def __aexit__(self, *_):
        if self._pool:
            await self._pool.release(self._pool_key,self.http);self.http=None;self._pool=None
        elif self._owns_http:
            await self.http.aclose()

    def headers(self, path: str) -> dict[str, str]:
        now = str(int(time.time()))
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                'Accept-Language':'en-US',
                'User-Agent': USER_AGENT, 'Origin': 'https://dreamina.capcut.com',
                'Referer': 'https://dreamina.capcut.com/', 'Appid': '513641',
                'Appvr': '8.4.0', 'Pf': '7', 'App-Sdk-Version': '48.0.0',
                'Device-Time': now, 'Sign': make_sign(path, now), 'Sign-Ver': '1',
                'Lan': 'en', 'Loc': self.region, 'Store-Country-Code': self.region.lower(),
                'Store-Country-Code-Src': 'uid', 'Cookie': _cookie(self.settings)}
        if self.settings.device_id:
            if not self.settings.device_id.isdigit():
                raise ValueError('DREAMINA_DEVICE_ID must contain digits only.')
            headers['Did'] = self.settings.device_id
            headers['Tdid'] = ''
        return headers

    async def request(self, path: str, body: dict, query: dict | None = None) -> dict:
        if self.http is None:await self.__aenter__()
        if path==GENERATE_PATH and getattr(self.settings,'account_id',''):
            raise DreaminaError('Managed accounts generate only through the native browser queue. Use /v1/videos or /v1/images/generations.',code='native_generation_required',status=409)
        if path==GENERATE_PATH and self.settings.signing=='browser' and self.egress.httpx_proxy:
            raise DreaminaError('The legacy browser transport cannot enforce this proxy. Use the managed native browser gateway.',code='legacy_proxy_transport_disabled',status=409)
        if not path.startswith(('/mweb/', '/commerce/')) or '?' in path or '#' in path:
            raise ValueError('Only a Dreamina API path is accepted.')
        commerce = path.startswith('/commerce/')
        params = {} if commerce else {'aid': '513641', 'device_platform': 'web',
                  'region': self.region, 'da_version': '3.3.32', 'web_version': '7.5.0',
                  'aigc_features': 'app_lip_sync'}
        if query:
            params.update(query)
        if not commerce:
            params['region'] = self.region
        body_text = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        url = str(httpx.URL((self.commerce_base if commerce else self.api_base) + path, params=params))
        if path == GENERATE_PATH and self.settings.signing == 'offline':
            try:
                from signer.runtime import sign_request
                url = await sign_request(url, body_text, ms_token=self.settings.ms_token)
            except Exception as error:
                raise DreaminaError('Offline SDK signing failed before submission; check the signer runtime and SDK hash.',
                                   code='signer_failed', status=503) from error
        elif path == GENERATE_PATH and self.settings.signing not in ('none', 'browser'):
            raise ValueError('DREAMINA_SIGNING must be browser, offline or none.')
        if path == GENERATE_PATH and self.settings.signing == 'browser':
            from signer.browser_transport import request_generation
            from signer.runtime import SignerError
            sid = self.settings.session_id
            if not sid and self.settings.cookie:
                from http.cookies import SimpleCookie
                parsed_cookies = SimpleCookie(self.settings.cookie)
                sid = parsed_cookies['sessionid'].value if 'sessionid' in parsed_cookies else ''
            try:
                captured = await request_generation(url, body_text, self.headers(path), sid,
                                                    region=self.region, allow_submit=True, cookie_header=self.settings.cookie)
            except SignerError as error:
                if 'transport ended' in str(error):
                    raise SubmissionUncertain(body['submit_id']) from error
                raise DreaminaError(str(error), code='browser_not_ready', status=503) from error
            if captured.get('api_origin') in API_ORIGINS:
                self.api_base = captured['api_origin']
            response = httpx.Response(captured['status'], json=captured['body'])
        else:
            try:
                response = await self.http.post(url, content=body_text.encode(), headers=self.headers(path))
            except httpx.RequestError as error:
                if path == GENERATE_PATH:
                    raise SubmissionUncertain(body['submit_id']) from error
                raise DreaminaError('The configured proxy connection failed; no direct fallback was used.' if self.egress.httpx_proxy else 'Could not reach Dreamina; check network settings.',
                    code='proxy_error' if self.egress.httpx_proxy else 'network_error') from None
        if response.status_code==407:raise DreaminaError('The configured proxy rejected authentication; no direct fallback was used.',code='proxy_error',status=503)
        if response.status_code >= 500 and path == GENERATE_PATH:
            raise SubmissionUncertain(body['submit_id'])
        try:
            result = response.json()
        except ValueError as error:
            if path == GENERATE_PATH and response.is_success:
                raise SubmissionUncertain(body['submit_id']) from error
            raise DreaminaError(f'Dreamina returned HTTP {response.status_code} without JSON; a challenge or routing issue may be present.',
                               code='non_json_response') from error
        if not isinstance(result, dict):
            if path == GENERATE_PATH:
                raise SubmissionUncertain(body['submit_id'])
            raise DreaminaError('Unexpected Dreamina response shape.', code='invalid_response')
        ret = str(result.get('ret', ''))
        if response.is_error or ret not in ('0', ''):
            message = str(result.get('errmsg') or result.get('message') or 'Request rejected')[:250]
            # Never include captured URLs, request headers, or credential values in errors.
            for secret in (self.settings.session_id, self.settings.cookie):
                if secret:
                    message = message.replace(secret, '[redacted]')
            raise DreaminaError(f'Dreamina HTTP {response.status_code}, ret={ret}: {message}',
                               code=f'upstream_{ret or response.status_code}',status=response.status_code if response.status_code>=400 else 502)
        return result.get('data', result)

    async def credits(self) -> dict:
        return await self.request('/commerce/v1/benefits/user_credit', {})

    async def audit_resources(self, model: str, references: list[dict]) -> dict:
        from .models import model_config
        key = model_config(model)['model_req_key']
        materials = []
        for resource in references:
            if resource.get('type') == 'video' and resource.get('vid'):
                materials.append({'material_type': 2, 'vid': resource['vid']})
            elif (resource.get('image_uri') or resource.get('uri')) and resource.get('type', 'image') == 'image':
                materials.append({'material_type': 1, 'uri': resource.get('image_uri') or resource['uri']})
            else:
                raise ValueError('Audit expects uploaded image/video resource objects.')
        if not materials:
            raise ValueError('At least one uploaded reference is required for an audit.')
        return await self.request('/mweb/v1/execute_generate_audit',
                                  {'scene': 2, 'model_key': key, 'material_list': materials})

    async def submit(self, prepared: dict) -> dict:
        data = await self.request(GENERATE_PATH, prepared['body'], prepared.get('query'))
        record = data.get('aigc_data') if isinstance(data, dict) else None
        if not isinstance(record, dict) or not record:
            raise SubmissionUncertain(prepared['submit_id'])
        result = normalize_record(record, prepared['submit_id'])
        result['api_origin'] = self.api_base
        result['warnings'] = prepared.get('warnings', [])
        return result

    async def poll(self, submit_id: str) -> dict:
        if not submit_id or len(submit_id) > 128:
            raise ValueError('Invalid submit_id.')
        data = await self.request(HISTORY_PATH, {'submit_ids': [submit_id]})
        record = data.get(submit_id)
        if not record:
            return {'id': submit_id, 'object': 'video', 'status': 'not_found', 'videos': []}
        return normalize_record(record, submit_id)

    async def wait(self, submit_id: str, *, timeout: float = 900, interval: float = 5) -> dict:
        import asyncio
        deadline = time.monotonic() + timeout
        while True:
            result = await self.poll(submit_id)
            if result['status'] in ('completed', 'failed', 'deleted'):
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(result, wait_timed_out=True)
            await asyncio.sleep(min(max(interval, 1), remaining))

    async def download(self, submit_id: str, destination: Path) -> Path:
        result = await self.poll(submit_id)
        if result['status'] != 'completed':
            raise DreaminaError(f"Video is {result['status']}; it is not ready to download.", code='not_ready', status=409)
        url = result['videos'][0]['url']
        if urlsplit(url).scheme != 'https':
            raise DreaminaError('Unexpected non-HTTPS video URL.', code='invalid_media_url')
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + '.part')
        # No Cookie / Authorization is attached to the media CDN.
        try:
            async with self.http.stream('GET', url, follow_redirects=True, headers={'User-Agent': USER_AGENT}) as response:
                response.raise_for_status()
                with partial.open('wb') as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
            partial.replace(destination)
        except httpx.HTTPStatusError:
            raise DreaminaError('Video download failed; re-query the original media through the configured egress.',code='download_failed') from None
        except (httpx.ProxyError,httpx.ConnectError,httpx.ConnectTimeout):
            raise DreaminaError('The configured connection failed while downloading the original media.',code='proxy_error' if self.egress.httpx_proxy else 'download_failed') from None
        except httpx.HTTPError:
            raise DreaminaError('Video download failed; retry the original media through the configured egress.',code='download_failed') from None
        return destination
