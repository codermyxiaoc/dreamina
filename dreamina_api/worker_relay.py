"""Narrow loopback bridge for an existing Chrome profile on a remote worker.

The extension keeps its fixed localhost endpoint. This process relays only its
three queue protocol operations to the central HTTPS API. It has no Dreamina
Cookie access and never performs a browser action or retries a task claim.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .chrome_bridge import HeartbeatInput, ReportInput, WorkerInput, TelemetryInput, ClaimIdentity, CreditPreflightInput, ClaimInput

PREFIX = '/v1/chrome'
EXTENSION_ID = re.compile(r'^[a-p]{32}$')
ACCOUNT_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
SETTINGS_NAMES = {'REMOTE_API_URL', 'LOCAL_BRIDGE_KEY', 'REMOTE_BRIDGE_KEY',
                  'CHROME_BRIDGE_EXTENSION_ID', 'ALLOWED_ACCOUNT_IDS'}


@dataclass(repr=False)
class RelayConfig:
    remote_api_url: str
    local_bridge_key: str
    remote_bridge_key: str
    extension_id: str
    allowed_account_ids: tuple[str, ...] = ('default',)

    def __post_init__(self):
        self.remote_api_url = self.remote_api_url.rstrip('/')
        parsed = urlsplit(self.remote_api_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('REMOTE_API_URL must be an absolute API base URL without credentials, query or fragment.')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('REMOTE_API_URL requires HTTPS; HTTP is allowed only for a loopback development server.')
        if parsed.hostname in ('localhost', '127.0.0.1', '::1') and (parsed.port or 80) == 8787:
            raise ValueError('The remote API cannot use this relay\'s loopback port 8787.')
        if any(part == '..' for part in parsed.path.split('/')) or '%' in parsed.path:
            raise ValueError('REMOTE_API_URL must not contain encoded or parent-directory path segments.')
        for name, value in (('LOCAL_BRIDGE_KEY', self.local_bridge_key), ('REMOTE_BRIDGE_KEY', self.remote_bridge_key)):
            if not isinstance(value, str) or len(value) < 24 or any(c.isspace() for c in value):
                raise ValueError(f'{name} must be an independent random key of at least 24 characters without whitespace.')
        if hmac.compare_digest(self.local_bridge_key, self.remote_bridge_key):
            raise ValueError('LOCAL_BRIDGE_KEY and REMOTE_BRIDGE_KEY must be different.')
        if not EXTENSION_ID.fullmatch(self.extension_id):
            raise ValueError('CHROME_BRIDGE_EXTENSION_ID must be the exact installed extension ID.')
        if not self.allowed_account_ids or any(not ACCOUNT_ID.fullmatch(x) for x in self.allowed_account_ids):
            raise ValueError('ALLOWED_ACCOUNT_IDS must contain one or more account IDs.')

    @classmethod
    def load(cls, path: str | Path | None = None):
        values = {}
        if path:
            # Use a dedicated worker configuration; this module does not load .env.
            for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
                key, separator, value = line.strip().partition('=')
                if separator and key in SETTINGS_NAMES:
                    values[key] = value.strip().strip('"').strip("'")
        values.update({key: os.environ[key] for key in SETTINGS_NAMES if key in os.environ})
        return cls(remote_api_url=values.get('REMOTE_API_URL', ''),
                   local_bridge_key=values.get('LOCAL_BRIDGE_KEY', ''),
                   remote_bridge_key=values.get('REMOTE_BRIDGE_KEY', ''),
                   extension_id=values.get('CHROME_BRIDGE_EXTENSION_ID', ''),
                   allowed_account_ids=tuple(x.strip() for x in values.get('ALLOWED_ACCOUNT_IDS', 'default').split(',') if x.strip()))


def create_relay_app(config: RelayConfig, *, http: httpx.AsyncClient | None = None) -> FastAPI:
    owned_http = http is None
    remote = http or httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
                                     verify=True, trust_env=False)

    @asynccontextmanager
    async def lifespan(_):
        yield
        if owned_http:
            await remote.aclose()

    app = FastAPI(title='Dreamina Chrome worker relay', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    expected_origin = 'chrome-extension://' + config.extension_id

    def error(code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status)

    @app.middleware('http')
    async def authorize(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and origin != expected_origin:
            return error('origin_denied', 'Only the configured Chrome extension may access this relay.', 403)
        if request.method == 'OPTIONS':
            requested = {part.strip().lower() for part in request.headers.get('access-control-request-headers', '').split(',') if part.strip()}
            if origin != expected_origin or request.headers.get('access-control-request-method', '').upper() not in ('POST','GET') or requested - {'content-type','x-chrome-bridge-key','x-chrome-claim-token','x-chrome-worker-id'}:
                return error('preflight_denied', 'Unsupported relay preflight.', 403)
            response = Response(status_code=204)
            response.headers['Access-Control-Allow-Methods'] = 'POST, GET'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Chrome-Bridge-Key, X-Chrome-Claim-Token, X-Chrome-Worker-Id'
            response.headers['Access-Control-Max-Age'] = '600'
        elif request.url.path == '/health' and request.method == 'GET':
            response = await call_next(request)
        elif not hmac.compare_digest(request.headers.get('x-chrome-bridge-key', ''), config.local_bridge_key):
            response = error('invalid_pairing_key', 'Invalid local bridge pairing key.', 401)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if origin == expected_origin:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Vary'] = 'Origin'
        return response

    def scrub(value):
        if isinstance(value, str):
            return value.replace(config.local_bridge_key, '[redacted]').replace(config.remote_bridge_key, '[redacted]')
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    async def forward(request: Request, path: str, schema):
        raw = await request.body()
        if len(raw) > 16_384:
            return error('request_too_large', 'Bridge protocol request exceeds 16 KiB.', 413)
        try:
            data = schema.model_validate_json(raw).model_dump(exclude_none=True)
        except (ValidationError, ValueError):
            # Validation details can contain arbitrary caller values; do not echo them.
            return error('invalid_request', 'Invalid bridge protocol request.', 422)
        if 'account_id' in data and data['account_id'] not in config.allowed_account_ids:
            return error('account_not_allowed', 'This worker is not configured for that account ID.', 403)
        if path == '/heartbeat':
            parsed = urlsplit(data['page_url'])
            if parsed.scheme != 'https' or parsed.hostname != 'dreamina.capcut.com' or parsed.username or parsed.password or not parsed.path.startswith('/ai-tool/'):
                return error('invalid_page', 'Only an official Dreamina generation page is accepted.', 422)
            data['page_url'] = 'https://dreamina.capcut.com' + parsed.path
        try:
            response = await remote.post(config.remote_api_url + PREFIX + path,
                        headers={'Content-Type': 'application/json', 'X-Chrome-Bridge-Key': config.remote_bridge_key},
                        content=json.dumps(data, separators=(',', ':')).encode(), follow_redirects=False)
        except httpx.TimeoutException:
            if path == '/claim':
                return error('claim_outcome_unknown', 'The central claim may have been accepted. Pause and inspect that worker; do not retry the claim automatically.', 504)
            return error('remote_timeout', 'Central bridge request timed out.', 504)
        except httpx.RequestError:
            if path == '/claim':
                return error('claim_outcome_unknown', 'Claim outcome is unknown. Do not retry automatically.', 502)
            return error('remote_unavailable', 'The central bridge is unavailable.', 502)
        if response.is_redirect:
            return error('remote_redirect_rejected', 'Central bridge redirects are not followed.', 502)
        if len(response.content) > 2_000_000:
            return error('remote_response_too_large', 'Central bridge response exceeded the relay limit.', 502)
        try:
            content = response.json()
        except ValueError:
            return error('invalid_remote_response', 'Central bridge did not return JSON.', 502)
        return JSONResponse(scrub(content), status_code=response.status_code)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'service': 'dreamina-chrome-worker-relay'}

    @app.post(PREFIX + '/heartbeat')
    async def heartbeat(request: Request):
        return await forward(request, '/heartbeat', HeartbeatInput)

    @app.post(PREFIX + '/claim')
    async def claim(request: Request):
        return await forward(request, '/claim', ClaimInput)

    @app.post(PREFIX + '/jobs/{job_id}/report')
    async def report(job_id: str, request: Request):
        import uuid
        try:
            normalized = str(uuid.UUID(job_id))
        except ValueError:
            return error('invalid_job_id', 'A UUID bridge job ID is required.', 422)
        return await forward(request, '/jobs/' + normalized + '/report', ReportInput)

    @app.post(PREFIX+'/jobs/{job_id}/telemetry')
    async def telemetry(job_id:str,request:Request):
        import uuid
        try:job_id=str(uuid.UUID(job_id))
        except ValueError:return error('invalid_job_id','A UUID is required.',422)
        return await forward(request,'/jobs/'+job_id+'/telemetry',TelemetryInput)

    @app.post(PREFIX+'/jobs/{job_id}/status')
    async def status(job_id:str,request:Request):
        import uuid
        try:job_id=str(uuid.UUID(job_id))
        except ValueError:return error('invalid_job_id','A UUID is required.',422)
        return await forward(request,'/jobs/'+job_id+'/status',ClaimIdentity)

    @app.post(PREFIX+'/jobs/{job_id}/preflight')
    async def credit_preflight(job_id:str,request:Request):
        if not re.fullmatch(r'[0-9a-fA-F-]{36}',job_id):return error('invalid_job','Invalid job ID.',400)
        return await forward(request,'/jobs/'+job_id+'/preflight',CreditPreflightInput)

    @app.post(PREFIX+'/jobs/{job_id}/activate')
    async def activate(job_id:str,request:Request):
        if not re.fullmatch(r'[0-9a-fA-F-]{36}',job_id):return error('invalid_job','Invalid job ID.',400)
        return await forward(request,'/jobs/'+job_id+'/activate',ClaimIdentity)

    @app.post(PREFIX+'/jobs/{job_id}/prepare')
    async def prepare(job_id:str,request:Request):
        if not re.fullmatch(r'[0-9a-fA-F-]{36}',job_id):return error('invalid_job','Invalid job ID.',400)
        return await forward(request,'/jobs/'+job_id+'/prepare',ClaimIdentity)

    @app.post(PREFIX+'/jobs/{job_id}/submission-turn')
    async def submission_turn(job_id:str,request:Request):
        if not re.fullmatch(r'[0-9a-fA-F-]{36}',job_id):return error('invalid_job','Invalid job ID.',400)
        return await forward(request,'/jobs/'+job_id+'/submission-turn',ClaimIdentity)

    @app.post(PREFIX+'/slots/recover')
    async def recover_slot(request:Request):
        return await forward(request,'/slots/recover',ClaimInput)

    @app.get(PREFIX+'/jobs/{job_id}/assets/{asset_id}')
    async def asset(job_id:str,asset_id:str,request:Request):
        import uuid,re
        try:job_id=str(uuid.UUID(job_id))
        except ValueError:return error('invalid_job_id','A UUID is required.',422)
        if not re.fullmatch(r'asset_\d{1,2}_[0-9a-f]{24}',asset_id):return error('invalid_asset','Invalid asset.',422)
        headers={'X-Chrome-Bridge-Key':config.remote_bridge_key,
                 'X-Chrome-Claim-Token':request.headers.get('x-chrome-claim-token',''),
                 'X-Chrome-Worker-Id':request.headers.get('x-chrome-worker-id','')}
        try:
            async with remote.stream('GET',config.remote_api_url+PREFIX+f'/jobs/{job_id}/assets/{asset_id}',headers=headers,follow_redirects=False) as response:
                if response.status_code!=200:return error('asset_unavailable','Central asset unavailable.',response.status_code if response.status_code>=400 else 502)
                content=bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>50*1024*1024:return error('asset_too_large','Asset exceeds relay limit.',413)
                return Response(bytes(content),media_type=response.headers.get('content-type','application/octet-stream'))
        except httpx.RequestError:return error('remote_unavailable','Central asset unavailable.',502)

    return app


def main():
    parser = argparse.ArgumentParser(description='Relay an existing Chrome extension to a central Dreamina HTTPS API.')
    parser.add_argument('--config', type=Path, help='Dedicated worker.env file; account cookies are not used.')
    args = parser.parse_args()
    try:
        config = RelayConfig.load(args.config)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    import uvicorn
    uvicorn.run(create_relay_app(config), host='127.0.0.1', port=8787, log_level='warning', access_log=False, ws='none')


if __name__ == '__main__':
    main()
