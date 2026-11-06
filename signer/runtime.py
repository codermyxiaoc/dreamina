"""Run the downloaded SDK in a disposable offline Chromium context.

This module never loads the user's Chrome profile and never sends the signed
request. It does not read .env, HAR files, sessionid, or the user's cookies.
The caller remains responsible for transmitting exactly the same body bytes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import async_playwright

SDK_FILE = Path(__file__).resolve().parent / 'sdk' / 'webmssdk.js'
SDK_SHA256 = 'e180fa46bf919c17dff94e5770b1033fa32590239c958945302faf28dfe77baf'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36')
ALLOWED_HOSTS = {'dreamina-api.us.capcut.com', 'mweb-api-sg.capcut.com'}
GENERATE_PATH = '/mweb/v1/aigc_draft/generate'


class SignerError(RuntimeError):
    pass


def _chromium_executable(default: str) -> str:
    configured = os.environ.get('SIGNER_CHROMIUM')
    if configured:
        if not Path(configured).is_file():
            raise SignerError('SIGNER_CHROMIUM does not point to an executable.')
        return configured
    if Path(default).is_file():
        return default
    # Reuse an existing dedicated Playwright runtime; never a user profile.
    base = Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright'
    candidates = list(base.glob('chromium-*/chrome-win64/chrome.exe'))
    candidates += list(base.glob('chromium-*/chrome-win/chrome.exe'))
    if candidates:
        return str(max(candidates, key=lambda p: int(p.parents[1].name.split('-')[-1])))
    raise SignerError('No Playwright Chromium runtime found; install Chromium or set SIGNER_CHROMIUM.')


async def sign_request(url: str, body: str, *, ms_token: str = '') -> str:
    """Return an SDK-signed URL for an exact JSON body, without network access.

    Uses the observed XHR hook. No sessionid is accepted or needed here.
    ms_token is optional caller-supplied SDK state; an empty value remains empty.
    A nonempty signature is not evidence that the remote service accepts it.
    """
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.path != GENERATE_PATH:
        raise ValueError('Only Dreamina video-generation HTTPS endpoints are supported.')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('URL must not contain credentials or a fragment.')
    if not isinstance(body, str):
        raise TypeError('body must be the exact JSON string that will be sent.')
    json.loads(body)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if any(k.lower() in {'x-bogus', 'x-gnarly'} for k in query):
        raise ValueError('Supply an unsigned URL, without captured SDK signatures.')
    if query.get('aid') != '513641':
        raise ValueError('The observed international Dreamina aid is 513641.')
    if any(c in ms_token for c in '\r\n;'):
        raise ValueError('ms_token must be a single cookie value.')
    source = SDK_FILE.read_bytes()
    if hashlib.sha256(source).hexdigest() != SDK_SHA256:
        raise SignerError('SDK hash mismatch; inspect a changed SDK before executing it.')
    source_text = source.decode('utf-8')
    if ms_token:
        # Exact-version instrumentation: expose only the existing SDK token slot.
        # This does not generate a token, patch the signature algorithm, or alter
        # fingerprint flags. It supplies caller-owned state to an offline SDK.
        anchor = 'o.frontierSign=q,o.getReferer='
        if source_text.count(anchor) != 1:
            raise SignerError('Offline token adapter anchor is ambiguous.')
        source_text = source_text.replace(anchor,
            'o.__setOfflineMsToken=function(value){i.o[833].v.msToken=value;},' + anchor)
    return await asyncio.wait_for(_sign(url, body, ms_token, source_text), timeout=25)


async def _sign(url: str, body: str, ms_token: str, source: str) -> str:
    browser = None
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                executable_path=_chromium_executable(playwright.chromium.executable_path),
                headless=True,
                args=['--disable-background-networking', '--disable-component-update',
                      '--disable-sync', '--no-first-run'],
            )
            context = await browser.new_context(
                offline=True, service_workers='block', user_agent=USER_AGENT,
                locale='en-US', viewport={'width': 1280, 'height': 720},
            )
            captured = asyncio.get_running_loop().create_future()
            expected_host = urlsplit(url).hostname

            async def intercept(route):
                req = route.request
                parsed = urlsplit(req.url)
                if req.resource_type == 'document' and req.url == 'https://dreamina.capcut.com/offline-signer':
                    await route.fulfill(status=200, content_type='text/html', body='<!doctype html><title>Offline SDK signer</title>')
                    return
                if parsed.hostname == expected_host and parsed.path == GENERATE_PATH and req.method == 'POST':
                    if not captured.done():
                        if req.post_data != body:
                            captured.set_exception(SignerError('SDK changed body bytes; signed request discarded.'))
                        else:
                            captured.set_result(req.url)
                # Every non-document request, including telemetry, is aborted.
                # No route ever calls continue_(), fetch(), or a network client.
                await route.abort('blockedbyclient')

            await context.route('**/*', intercept)
            page = await context.new_page()
            await page.goto('https://dreamina.capcut.com/offline-signer')
            await page.add_script_tag(content=source)
            sdk_region = 'va-capcut' if expected_host == 'dreamina-api.us.capcut.com' else 'sg-capcut'
            await page.evaluate('''region => {
                window.byted_acrawler.init({aid:513641,isSDK:false,region,
                  boe:false,enablePathList:['/mweb/v1/aigc_draft/generate'],mode:514,
                  slardarConfig:{enableSlardar:false,enableLazyload:false,settingLocation:0,
                    initConfigOverrides:{slardarDomain:'',slardarPluginPrefixPath:''}}});
            }''', sdk_region)
            if ms_token:
                await page.evaluate('token => window.byted_acrawler.__setOfflineMsToken(token)', ms_token)
            await page.evaluate('''({url,body}) => new Promise(resolve => {
                const xhr = new XMLHttpRequest();
                xhr.open('POST',url,true);
                xhr.setRequestHeader('Content-Type','application/json');
                xhr.timeout=6000;
                xhr.onload=xhr.onerror=xhr.ontimeout=()=>resolve();
                xhr.send(body);
            })''', {'url':url,'body':body})
            signed = await asyncio.wait_for(captured, timeout=3)
            params = dict(parse_qsl(urlsplit(signed).query, keep_blank_values=True))
            if not params.get('X-Bogus') or not params.get('X-Gnarly'):
                raise SignerError('SDK did not produce both X-Bogus and X-Gnarly.')
            if ms_token and params.get('msToken') != ms_token:
                raise SignerError('SDK did not preserve supplied ms_token.')
            return signed
        finally:
            if browser is not None:
                await browser.close()


async def _probe() -> None:
    url = 'https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate?aid=513641&device_platform=web&region=US'
    results = []
    for token in ('', 'offline-probe-token-not-a-real-credential'):
        signed = await sign_request(url, '{"offline_probe":true}', ms_token=token)
        query = dict(parse_qsl(urlsplit(signed).query, keep_blank_values=True))
        results.append({'supplied_ms_token': bool(token), 'ms_token_preserved':query.get('msToken') == token,
                        'x_bogus_length':len(query['X-Bogus']),'x_gnarly_length':len(query['X-Gnarly'])})
    print(json.dumps({'offline_probe':results,'sdk_sha256':SDK_SHA256,'live_submission_performed':False},indent=2))


if __name__ == '__main__':
    asyncio.run(_probe())
