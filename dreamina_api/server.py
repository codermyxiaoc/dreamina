"""Run with python -m dreamina_api serve. Defaults to loopback only."""
from __future__ import annotations

import hmac
import re
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .client import DreaminaClient, DreaminaError, SubmissionUncertain, HISTORY_PATH, normalize_record
from .config import ROOT, Settings, load_env
from .models import get_models
from .payloads import build_generation
from .service import create_video
from .chrome_bridge import BridgeError, create_chrome_bridge_router
from .chrome_verify import verify_record
from .public_cors import PublicAPICORSMiddleware
from .task_limits import TaskLimits
from .maintenance import ManagedMaintenance
from .monitoring import monitoring_snapshot
from .egress_clients import close_account_client_pool
from .proxy_health import ProxyHealthManager
from .proxy_service import ProxyBindingService
from .proxy_api import create_proxy_router
from .monitoring import ProcessSampler
from .db_async import db_call

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(platform_runtime.run_reconciler()) if load_env().get('ENABLE_RECONCILER','1') == '1' else None
    credit_refresh=asyncio.create_task(platform_runtime.run_credit_refresh()) if task else None
    login_enabled=load_env().get('DISABLE_LOGIN_MANAGER_LIFECYCLE')!='1'
    supervisor_task=None
    maintenance_task=None
    proxy_task=None;proxy_recovery_task=None;performance_task=asyncio.create_task(process_sampler.run())
    if login_enabled:
        await account_login_manager.startup()
        async def scan_managed_workers():
            while True:
                try:
                    nodes=await managed_workers.scan()
                    for node in nodes:
                        if node.get('worker_error_code')=='proxy_error':await platform_runtime.proxy_failure(node['account_id'])
                except Exception:pass
                try:
                    for policy in await asyncio.to_thread(platform_store.worker_registry_policies):
                        try:
                            if policy['deleted']:await asyncio.to_thread(chrome_store.retire_account,policy['account_id'])
                            else:await asyncio.to_thread(chrome_store.prune_workers,policy['account_id'],keep_slots=policy['keep_slots'])
                        except Exception:pass  # Preserve evidence and retry this account next scan.
                except Exception:pass
                await asyncio.sleep(5)
        supervisor_task=asyncio.create_task(scan_managed_workers())
        maintenance_task=asyncio.create_task(managed_maintenance.run())
        proxy_recovery_task=asyncio.create_task(proxy_service.recover())
        proxy_task=asyncio.create_task(proxy_health.run())
    try:yield
    finally:
        if login_enabled:await account_login_manager.shutdown()
        for running in (task,supervisor_task,credit_refresh,maintenance_task,proxy_task,proxy_recovery_task,performance_task):
            if running:running.cancel()
        await asyncio.gather(*(r for r in (task,supervisor_task,credit_refresh,maintenance_task,proxy_task,proxy_recovery_task,performance_task) if r),return_exceptions=True)
        await proxy_service.close();await proxy_health.close();await close_account_client_pool()


app = FastAPI(title='Dreamina Console API', version='0.13.2',
              description='Self-hosted Dreamina gateway. Images API and historical Videos-protocol compatibility; not an OpenAI service.',
              lifespan=lifespan)
app.add_middleware(PublicAPICORSMiddleware,origins=load_env().get('PUBLIC_API_CORS_ORIGINS'))


def authorize(request: Request) -> Settings:
    config = Settings.load()
    # Browsers must not drive a token-bearing local service from an unrelated origin.
    if request.headers.get('origin'):
        raise HTTPException(403, 'Use a local HTTP client; cross-origin browser requests are disabled.')
    expected = config.api_key or load_env().get('LOCAL_API_KEY','')
    actual = request.headers.get('authorization', '')
    if not expected or not hmac.compare_digest(actual, 'Bearer ' + expected):
        raise HTTPException(401, 'This internal route requires the private operator key. Use /v1 with a gateway key.')
    if request.url.path.startswith('/internal/'):
        account_id=request.headers.get('x-dreamina-account') or request.query_params.get('account_id')
        if not account_id:raise HTTPException(400,'Internal upstream requests require an account_id or X-Dreamina-Account; global direct credentials are not used.')
        if request.headers.get('x-dreamina-session'):raise HTTPException(400,'Session overrides are not permitted for managed account egress.')
        return platform_store.settings_for_account(account_id)
    supplied = request.headers.get('x-dreamina-session')
    if supplied:
        config.session_id, config.cookie = supplied, ''
        config.workspace_id = ''
    return config


def checked_id(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9-]{1,128}', value):
        raise HTTPException(422, 'Invalid submit_id.')
    return value


@app.exception_handler(DreaminaError)
async def upstream_error(_, error: DreaminaError):
    body = {'error': {'code': error.code, 'message': str(error)}}
    if isinstance(error, SubmissionUncertain):
        body['submit_id'] = error.submit_id
    return JSONResponse(body, status_code=error.status)


@app.exception_handler(ValueError)
async def invalid_request(_, error: ValueError):
    return JSONResponse({'error': {'code': 'invalid_request', 'message': str(error)}}, status_code=422)


@app.exception_handler(NotImplementedError)
async def unsupported(_, error: NotImplementedError):
    return JSONResponse({'error': {'code': 'not_implemented', 'message': str(error)}}, status_code=501)


from .uploads import UploadError


@app.exception_handler(UploadError)
async def upload_error(_, error: UploadError):
    return JSONResponse({'error': {'code': 'upload_failed', 'stage': error.stage, 'message': str(error)}}, status_code=502)


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'dreamina-local-api'}


@app.get('/internal/models', include_in_schema=False)
def models(_: Settings = Depends(authorize)):
    return {'object': 'list', 'data': get_models()}


@app.post('/internal/auth/check', include_in_schema=False)
async def auth_check(config: Settings = Depends(authorize)):
    async with DreaminaClient(config) as client:
        credit = await client.credits()
        return {'authenticated': True, 'region': config.region, 'credits': credit}


@app.post('/internal/videos/prepare', include_in_schema=False)
async def prepare(body: dict, config: Settings = Depends(authorize)):
    spec = dict(body)
    workspace = spec.pop('workspace_id', None) or config.workspace_id or None
    return build_generation(spec, workspace_id=workspace)


@app.post('/internal/videos/generations', status_code=202, include_in_schema=False)
async def generate(body: dict, config: Settings = Depends(authorize)):
    spec = dict(body)
    workspace = spec.pop('workspace_id', None)
    wait_seconds = float(spec.pop('wait_seconds', 0))
    if not 0 <= wait_seconds <= 1800:
        raise ValueError('wait_seconds must be between 0 and 1800.')
    download = spec.pop('download', False)
    if not isinstance(download, bool):
        raise ValueError('download must be a boolean.')
    async with DreaminaClient(config) as client:
        return await create_video(client, spec, workspace_id=workspace, wait_seconds=wait_seconds, download=download)


@app.get('/internal/videos/{submit_id}', include_in_schema=False)
async def get_video(submit_id: str, config: Settings = Depends(authorize)):
    async with DreaminaClient(config) as client:
        return await client.poll(checked_id(submit_id))


@app.get('/internal/videos/{submit_id}/content', include_in_schema=False)
async def content(submit_id: str, config: Settings = Depends(authorize)):
    submit_id = checked_id(submit_id)
    async with DreaminaClient(config) as client:
        path = await client.download(submit_id, ROOT / 'outputs' / f'{submit_id}.mp4')
    return FileResponse(path, media_type='video/mp4', filename=path.name)


@app.post('/internal/assets', include_in_schema=False)
async def upload(body: dict, config: Settings = Depends(authorize)):
    from .uploads import AssetUploader
    if not isinstance(body.get('path'), str) or not body['path']:
        raise ValueError('path must be a local image or video file path.')
    async with DreaminaClient(config) as client:
        uploader = AssetUploader(client)
        if body.get('type', 'image') == 'image':
            resource = await uploader.upload_image(Path(body['path']))
        elif body['type'] == 'video':
            resource = await uploader.upload_video(Path(body['path']), experimental=True)
        else:
            raise ValueError('type must be image or video.')
        return {'object': 'asset', 'resource': resource}


@app.post('/internal/assets/audit', include_in_schema=False)
async def audit(body: dict, config: Settings = Depends(authorize)):
    if not isinstance(body.get('references'), list) or not all(isinstance(x, dict) for x in body['references']):
        raise ValueError('references must be a list of uploaded resource objects.')
    async with DreaminaClient(config) as client:
        return await client.audit_resources(body.get('model', 'seedance-2.0-mini'), body['references'])


def record_browser_observation(job,values):
    platform_store.record_bridge_observation(job['id'],values)


async def verify_credit_before_generation(job,worker_id,claim_token,quote):
    return await platform_runtime.credit_preflight(job,worker_id,claim_token,quote)


async def activate_native_generation(job,worker_id,claim_token):
    return await platform_runtime.activate_task(job,worker_id,claim_token)

async def prepare_native_generation(job,worker_id,claim_token):
    return await platform_runtime.prepare_task(job,worker_id,claim_token)

async def reserve_native_submission_turn(job,worker_id,claim_token):
    return await platform_runtime.submission_turn(job,worker_id,claim_token)

async def native_submission_reported(job):
    return await platform_runtime.reported_submission(job)


def native_account_limits(account_id):
    try:return platform_store.get_account_limits(account_id)
    except Exception:return {'enabled':False,'concurrency_limit':0}


chrome_router = create_chrome_bridge_router(authorize,telemetry_sink=record_browser_observation,credit_checker=verify_credit_before_generation,
                                           account_limits_loader=native_account_limits,activation_checker=activate_native_generation,
                                           preparation_checker=prepare_native_generation,submission_turn_checker=reserve_native_submission_turn,report_sink=native_submission_reported)
chrome_store = chrome_router.bridge_store
app.include_router(chrome_router)


@app.post('/v1/chrome/jobs/{job_id}/sync', tags=['Chrome bridge'])
async def sync_chrome_job(job_id: str, config: Settings = Depends(authorize)):
    try:
        job = await db_call(chrome_store.get,job_id)
        config = await db_call(platform_store.settings_for_account,job['account_id'],existing_task=True)
        if job.get('api_origin'): config.api_origin=job['api_origin']
        if job['status'] not in ('submitted', 'uncertain') or not job.get('submit_id'):
            return job
        async with DreaminaClient(config) as client:
            data = await client.request(HISTORY_PATH, {'submit_ids': [job['submit_id']]})
        if not isinstance(data,dict):raise HTTPException(502,'Upstream history returned an invalid response.')
        record = data.get(job['submit_id'])
        if not record:
            return dict(job, verification_pending=True)
        try:
            verify_record(job, record)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        from .image_models import normalize_image_record
        result = (normalize_image_record if job['request'].get('kind')=='image' else normalize_record)(record, job['submit_id'])
        return await db_call(chrome_store.reconcile_submitted,job_id,result)
    except BridgeError as error:
        raise HTTPException(error.status, str(error)) from error


@app.get('/v1/chrome/jobs/{job_id}/content', tags=['Chrome bridge'])
async def chrome_job_content(job_id: str, config: Settings = Depends(authorize)):
    job = await sync_chrome_job(job_id, config)
    if job['status'] != 'completed':
        raise HTTPException(409, f"Chrome job is {job['status']}; video is not ready.")
    config = await db_call(platform_store.settings_for_account,job['account_id'],existing_task=True)
    if job.get('api_origin'): config.api_origin=job['api_origin']
    async with DreaminaClient(config) as client:
        path = await client.download(job['submit_id'], ROOT / 'outputs' / f"chrome-{job['id']}.mp4")
    return FileResponse(path, media_type='video/mp4', filename=path.name)


from .platform_store import PlatformStore
from .admin_api import create_admin_router
from .openai_api import create_openai_router, augment_openapi
from .platform_runtime import PlatformRuntime, model_catalog
from .managed_workers import ManagedWorkers
from .account_login import AccountLoginManager,create_account_login_router

environment = load_env()
proxy_required=environment.get('ACCOUNT_PROXY_REQUIRED','true').lower()
if proxy_required not in ('true','false'):raise ValueError('ACCOUNT_PROXY_REQUIRED must be true or false.')
platform_store = PlatformStore(database_url=environment.get('DATABASE_URL') or None,
                               encryption_key=environment.get('PLATFORM_ENCRYPTION_KEY') or None,
                               worker_status=chrome_store.accounts,account_capacity_limit=int(environment.get('NATIVE_MAX_ACCOUNT_SLOTS','4')),
                               account_inflight_limit=int(environment.get('NATIVE_MAX_ACCOUNT_INFLIGHT','100')),
                               account_proxy_required=proxy_required=='true')
if environment.get('ADMIN_PASSWORD'):
    platform_store.bootstrap_admin(environment.get('ADMIN_USERNAME','admin'), environment['ADMIN_PASSWORD'],
                                   minimum_length=int(environment.get('ADMIN_PASSWORD_MIN_LENGTH','12')))
platform_store.import_default(Settings.load())
platform_store._migrate_legacy_proxies()
platform_runtime = PlatformRuntime(platform_store, chrome_store,limits=TaskLimits.from_env(environment),
                                   submission_attempts=int(environment.get('NATIVE_SUBMISSION_ATTEMPTS','2')))
managed_workers=ManagedWorkers(platform_store,chrome_store,environment)
managed_maintenance=ManagedMaintenance(managed_workers,config=environment)
proxy_health=ProxyHealthManager(platform_store,config=environment)
proxy_service=ProxyBindingService(platform_store,managed_workers,proxy_health,platform_runtime.check_account)
proxy_health.binder=proxy_service.bind
platform_runtime.proxy_health=proxy_health
process_sampler=ProcessSampler()
account_login_manager=AccountLoginManager(platform_store,chrome_store,managed_workers,config=environment,
                                         account_check=platform_runtime.check_account)


def admin_models():
    accounts = platform_store.list_accounts()
    blocked={(b['account_id'],b['model']) for b in platform_store.account_model_blocks()}
    result = []
    for item in model_catalog():
        result.append(dict(item, available=any(a['enabled'] and a.get('ready') and not a.get('scheduling_paused') and item['id'] in a['capabilities'] and (a['id'],item['id']) not in blocked for a in accounts)))
    exposed = {item['id'] for item in result}
    for item in get_models():
        if item['id'] not in exposed:
            result.append(dict(item, kind='video', enabled=False, available=False,
                               verification_status='configuration-only',
                               description='已收集网页参数；该模型尚未接入并实测浏览器执行器。'))
    return result


async def load_monitoring():
    return await asyncio.to_thread(monitoring_snapshot,platform_store,chrome_store,managed_workers,managed_maintenance,proxy_health,process_sampler)

admin_router=create_admin_router(platform_store, chrome_store,account_check=platform_runtime.check_account,
                                 models_loader=admin_models,managed_workers=managed_workers,
                                 task_reconciler=platform_runtime.manual_reconcile,monitor_loader=load_monitoring,gate_releaser=platform_runtime.release_gate)
app.include_router(admin_router)
app.include_router(create_proxy_router(platform_store,proxy_service,admin_router))
app.include_router(create_account_login_router(account_login_manager,admin_router.admin_dependency))
app.include_router(create_openai_router(platform_store, chrome_store,
                                       sync_task=platform_runtime.sync_task,
                                       image_generate=platform_runtime.generate_image,
                                       image_download=platform_runtime.download_image,
                                       download_video=platform_runtime.download_video,
                                       image_timeout=lambda: platform_store.get_settings()['default_image_timeout'],
                                       model_catalog=model_catalog))


@app.get('/', include_in_schema=False)
def home():
    return RedirectResponse('/admin/')


@app.get('/admin', include_in_schema=False)
def admin_redirect():
    return RedirectResponse('/admin/')


app.mount('/admin', StaticFiles(directory=ROOT/'web', html=True), name='admin')


def gateway_openapi():
    if app.openapi_schema is None:
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(title=app.title, version=app.version,
                             description=app.description, routes=app.routes)
        app.openapi_schema = augment_openapi(schema)
    return app.openapi_schema


app.openapi = gateway_openapi
