"""Owned-key Images API and historical Videos-protocol compatibility.

This is a self-hosted Dreamina adapter, not an OpenAI service. The router never
generates media itself: video dispatch uses the paired Chrome queue and image
generation requires an explicitly supplied provider. Paid dispatch is claimed
once in the platform store; retries with the same idempotency key do not submit
again. Every task lookup and download is scoped to its originating API key.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import ROOT
from .db_async import db_call
from .request_uploads import UploadInputError,parse_request,stage_for,cleanup_uploads,run_file_io,persist_upload,finish_upload_input

log=logging.getLogger(__name__)

MAX_BODY_BYTES = 96 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
BASE_VIDEO_MODEL = 'seedance-2.0-mini'
BASE_IMAGE_MODEL = 'seedream-5.0-lite'


class OpenAIError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = 'invalid_request',
                 param: str | None = None, error_type: str = 'invalid_request_error',
                 task_id: str | None = None):
        super().__init__(message)
        self.status, self.code, self.param = status, code, param
        self.error_type, self.task_id = error_type, task_id


def _error_response(error: Exception, *, allocated_task_id: str | None = None) -> JSONResponse:
    if isinstance(error, OpenAIError):
        status, code, message = error.status, error.code, str(error)
        kind, param, task_id = error.error_type, error.param, error.task_id
    elif isinstance(error, RequestValidationError):
        status, code, message, kind = 400, 'invalid_request', 'Invalid request parameter type.', 'invalid_request_error'
        param, task_id = None, None
    elif isinstance(error, StarletteHTTPException):
        status, code, message, kind = error.status_code, 'invalid_request', str(error.detail), 'invalid_request_error'
        param, task_id = None, None
    elif hasattr(error, 'code') and hasattr(error, 'status'):
        status, code, message = int(error.status), str(error.code), str(error)
        kind = 'authentication_error' if status == 401 else 'permission_error' if status == 403 else 'rate_limit_error' if status == 429 else 'invalid_request_error' if status < 500 else 'server_error'
        param, task_id = None, None
    else:
        status, code = 502, 'provider_error'
        message, kind, param, task_id = 'The provider could not complete this request.', 'server_error', None, None
    if allocated_task_id:
        # This POST already reserved a durable generation task. Do not invite a
        # generic SDK's automatic 5xx retry to create another paid task when no
        # client idempotency key was supplied. The original ID remains resumable.
        task_id = task_id or allocated_task_id
        if status >= 500:
            status = 409
    result = {'error': {'message': message, 'type': kind, 'param': param, 'code': code}}
    headers = {}
    if task_id:
        result['task_id'] = task_id
        headers['x-request-id'] = task_id
    return JSONResponse(result, status_code=status, headers=headers)


async def _call(fn: Callable, *args, **kwargs):
    value = fn(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


async def _parse(request: Request, *, multipart: bool = False) -> tuple[dict, list[dict]]:
    """Bounded streamed multipart parsing; files remain request-owned on disk."""
    try:return await parse_request(request,allow_multipart=multipart,max_body_bytes=MAX_BODY_BYTES,max_file_bytes=MAX_UPLOAD_BYTES)
    except UploadInputError as error:
        raise OpenAIError(str(error),status=error.status,code=error.code,param=error.param) from None


def _principal(request: Request, store) -> dict:
    header = request.headers.get('authorization', '')
    scheme, sep, token = header.partition(' ')
    if scheme.lower() != 'bearer' or not sep or not token or token.strip() != token or any(x.isspace() for x in token):
        raise OpenAIError('Provide your gateway API key as Authorization: Bearer <key>.',
                          status=401, code='invalid_api_key', error_type='authentication_error')
    principal = store.authenticate_key(token)
    if not principal:
        raise OpenAIError('Invalid API key.', status=401, code='invalid_api_key', error_type='authentication_error')
    return principal


def _idempotency(request: Request) -> str | None:
    value = request.headers.get('idempotency-key')
    if value is not None and (not 1 <= len(value) <= 200 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)):
        raise OpenAIError('Idempotency-Key must contain 1–200 printable non-space ASCII characters.', param='Idempotency-Key')
    return value


def _unknown(data: dict, allowed: set[str]):
    extra = set(data) - allowed
    if extra:
        name = sorted(extra)[0]
        raise OpenAIError(f'Unsupported request parameter: {name}.', param=name, code='unsupported_parameter')


def _prompt(data: dict, limit: int = 32000) -> str:
    value = data.get('prompt')
    if not isinstance(value, str):
        raise OpenAIError('prompt must be a nonempty string.', param='prompt')
    value = value.strip()
    try:
        length = len(value.encode('utf-16-le')) // 2
    except UnicodeEncodeError as exc:
        raise OpenAIError('prompt contains an invalid Unicode surrogate.', param='prompt') from exc
    if not value or length > limit:
        raise OpenAIError(f'prompt must contain 1–{limit} UTF-16 characters.', param='prompt')
    return value


def _integer(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r'\d+', str(value)):
        raise OpenAIError(f'{name} must be an integer.', param=name)
    number = int(value)
    if not low <= number <= high:
        raise OpenAIError(f'{name} must be between {low} and {high}.', param=name)
    return number


def _timestamp(value) -> int | None:
    if value is None:
        return None
    if hasattr(value, 'timestamp'):
        return int(value.timestamp())
    return int(float(value))


def video_object(task: dict) -> dict:
    raw_status=task['status']
    status='completed' if raw_status=='completed' else 'failed' if raw_status in ('failed','cancelled','uncertain') else 'queued' if raw_status=='queued' else 'in_progress'
    req=task.get('request') or {};actual=task.get('result') or {}
    result={'id':task['id'],'object':'video','model':task['model'],'status':status,'created_at':_timestamp(task['created_at'])}
    if task.get('completed_at') is not None:result['completed_at']=_timestamp(task['completed_at'])
    progress=100 if status=='completed' else task.get('progress') if task.get('progress_details') else 0 if status=='queued' else None
    if progress is not None:result['progress']=progress
    if task.get('progress_details'):result['progress_details']=task['progress_details']
    duration=req.get('duration',req.get('seconds'))
    if duration is not None:result.update(duration=int(duration),seconds=str(duration))
    if req.get('size'):result['size']=req['size']
    # The requested resolution is a model tier, not necessarily the output's
    # short edge. Report the dimensions of the same first origin we download.
    videos=actual.get('videos') if isinstance(actual,dict) else None
    if status=='completed' and isinstance(videos,list) and videos and isinstance(videos[0],dict):
        width,height=videos[0].get('width'),videos[0].get('height')
        if type(width) is int and type(height) is int and width>0 and height>0:
            result['size']=f'{width}x{height}'
    resolution=req.get('resolution')
    if resolution:result['resolution']=resolution
    if task.get('billing_status') is not None:result['billing_status']=task['billing_status']
    if task.get('usage') is not None:result['usage']=task['usage']
    if status=='failed':
        error=task.get('error') or ({'code':'submission_unconfirmed','message':'Submission outcome is unknown. Inspect this original task; do not resubmit.'} if raw_status=='uncertain' else {'code':'generation_failed','message':'Video generation failed.'})
        if isinstance(error,str):error={'code':'generation_failed','message':error}
        result['error']={'code':str(error.get('code') or 'generation_failed'),'message':str(error.get('message') or 'Video generation failed.')}
    if raw_status=='uncertain':
        result['metadata']={'verification_required':True,'submission_state':'uncertain','message':'Do not submit again; reconcile the original task.'}
        if task.get('submit_id'):result['metadata']['original_task_pending']=True
    if task.get('conversation_id'):result.setdefault('metadata',{})['conversation_id']=task['conversation_id']
    retry=(task.get('request_detail') or {}).get('submission_retry')
    if isinstance(retry,dict):
        result.setdefault('metadata',{})['submission_retry']={k:retry[k] for k in
            ('attempt','max_attempts','stage','next_retry_at','reason') if k in retry}
    return result


def _safe_provider_error(exc: Exception) -> dict:
    # Provider messages can contain captured URLs or credentials; use only a code.
    code = str(getattr(exc, 'code', 'provider_error'))[:80]
    return {'code': code, 'message': 'The upstream provider could not complete the request. See the admin task detail.'}


def create_openai_router(store, bridge_store, *, sync_task: Callable | None = None,
                         image_generate: Callable | None = None, download_video: Callable | None = None,
                         image_download: Callable | None = None, model_catalog=None,
                         image_timeout: float | Callable = 600, media_directory: Path | None = None) -> APIRouter:
    """Build public routes with injectable providers and persistent ownership.

    sync_task(task) -> updated platform task, potentially after querying upstream.
    image_generate(spec, account_settings, task=task) -> {images:[{url,b64_json,...}]}.
    download_video(task) -> Path or bytes; image_download(image, settings) -> bytes.
    model_catalog is a list or callable returning {id,kind,enabled,capabilities}.
    """
    media_directory = Path(media_directory or ROOT / 'outputs' / 'platform-inputs')

    class CompatibilityRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def handler(request: Request):
                try:
                    return await original(request)
                except Exception as error:
                    return _error_response(error, allocated_task_id=getattr(request.state, 'allocated_task_id', None))
                finally:
                    try:await cleanup_uploads(request)
                    except Exception:log.warning('Private upload staging cleanup requires maintenance')
            return handler

    router = APIRouter(prefix='/v1', tags=['OpenAI compatible'], route_class=CompatibilityRoute)

    def catalog() -> list[dict]:
        if model_catalog is None:
            result = [{'id': BASE_VIDEO_MODEL, 'kind': 'video', 'enabled': True,
                       'capabilities': {'sizes': ['1280x720'], 'seconds': ['4'], 'input_reference': False}}]
            if image_generate is not None:
                result.append({'id': BASE_IMAGE_MODEL, 'kind': 'image', 'enabled': True,
                               'capabilities': {'sizes': ['2048x2048'], 'default_size': '2048x2048', 'n': [1], 'edits': False}})
            return result
        value = model_catalog() if callable(model_catalog) else model_catalog
        return list(value)

    def require_model(model: Any, kind: str) -> dict:
        if not isinstance(model, str):
            raise OpenAIError('model must be a string.', param='model')
        item = next((x for x in catalog() if x['id'] == model and x['kind'] == kind), None)
        if item is None:
            raise OpenAIError('Unknown model for this operation.', status=404, code='model_not_found', param='model')
        if not item.get('enabled', True):
            raise OpenAIError('This model is not enabled on this gateway.', status=503, code='capability_unavailable', param='model')
        return item

    def owned(task_id: str, principal: dict, kind: str) -> dict:
        task = store.get_task(task_id, principal=principal)
        if not task or task.get('kind') != kind:
            raise OpenAIError('Task not found.', status=404, code='not_found')
        return task

    async def refreshed(task: dict) -> dict:
        if sync_task is not None and task['status'] not in ('completed', 'failed'):
            return await _call(sync_task, task)
        return task

    def mark_uncertain(task_id: str, error: dict) -> None:
        # A database outage must not remove the already allocated task ID from
        # the response. dispatch_claimed remains the durable no-resubmit guard.
        try:
            store.update_task(task_id, status='uncertain', error=error)
        except Exception:
            pass

    async def image_response(task: dict, response_format: str | None = None) -> dict:
        if task['status'] != 'completed':
            raise OpenAIError('Image generation is not completed. Resume this task rather than submitting again.',
                              status=409, code='generation_in_progress' if task['status'] != 'failed' else 'generation_failed',
                              task_id=task['id'])
        def unavailable(message: str):
            # Generation has already completed. A 5xx here can cause an SDK to
            # retry POST /images/generations and create a second paid image.
            return OpenAIError(message + ' Retrieve the original image task result; do not generate again.',
                               status=409, code='result_unavailable', task_id=task['id'])
        result = task.get('result') or {}
        images = result.get('images', result.get('data', []))
        if not isinstance(images, list) or not images:
            raise unavailable('Provider completion has no usable image result.')
        fmt = response_format or task['request'].get('response_format', 'url')
        output = []
        for index, entry in enumerate(images):
            if not isinstance(entry, dict):
                raise unavailable('Provider completion has an invalid image result.')
            item = {}
            if fmt == 'url':
                url = entry.get('url')
                if not isinstance(url, str) or urlsplit(url).scheme not in ('http', 'https'):
                    raise unavailable('Provider did not supply an image URL.')
                item['url'] = url
            else:
                encoded = entry.get('b64_json')
                if encoded is None and entry.get('_local_image'):
                    local = (media_directory / task['id'] / entry['_local_image']).resolve()
                    expected_parent = (media_directory / task['id']).resolve()
                    if local.parent != expected_parent or not local.is_file():
                        raise unavailable('Cached image is unavailable.')
                    try:
                        encoded = base64.b64encode(local.read_bytes()).decode('ascii')
                    except OSError as exc:
                        raise unavailable('Cached image could not be read.') from exc
                if encoded is None and image_download is not None:
                    try:
                        raw = await _call(image_download, entry, (await db_call(store.settings_for_account, task['account_id'], existing_task=True, _store=store)))
                    except Exception as exc:
                        raise unavailable('Image was generated, but its media could not be downloaded.') from exc
                    if not isinstance(raw, bytes) or not raw or len(raw) > 50 * 1024 * 1024:
                        raise unavailable('Downloaded image data is invalid or too large.')
                    # Cache accepted media privately. The receipt contains only
                    # a basename; neither the blob nor base64 enters SQL JSON.
                    folder = media_directory / task['id']
                    filename = f'generated-{index}.bin'
                    temporary = folder / f'.{filename}.{secrets.token_hex(8)}.part'
                    try:
                        folder.mkdir(parents=True, exist_ok=True)
                        temporary.write_bytes(raw)
                        temporary.replace(folder / filename)
                        cached_images = [dict(image) for image in images]
                        cached_images[index]['_local_image'] = filename
                        result = dict(result, images=cached_images)
                        result.pop('data', None)
                        (await db_call(store.update_task, task['id'], result=result, _store=store))
                        images = cached_images
                    except Exception as exc:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise unavailable('Image was generated, but its private result cache could not be saved.') from exc
                    encoded = base64.b64encode(raw).decode('ascii')
                if not isinstance(encoded, str) or not encoded:
                    raise unavailable('Provider did not supply base64 image data.')
                try:
                    base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise unavailable('Provider returned invalid base64 image data.') from exc
                item['b64_json'] = encoded
            if isinstance(entry.get('revised_prompt'), str):
                item['revised_prompt'] = entry['revised_prompt']
            output.append(item)
        if len(output) != task['request']['n']:
            raise unavailable('Provider image count does not match n.')
        response = {'created': _timestamp(task.get('completed_at') or task['created_at']), 'data': output}
        if result.get('output_format') in ('png', 'jpeg', 'webp'):
            response['output_format'] = result['output_format']
        return response

    def cache_image_result(task: dict, result: dict) -> dict:
        """Keep large base64 blobs out of relational task receipts."""
        images = result.get('images', result.get('data'))
        if not isinstance(images, list) or len(images) != task['request']['n']:
            raise OpenAIError('Provider image count does not match n.', status=502, code='invalid_provider_result', task_id=task['id'])
        cached = []
        for index, entry in enumerate(images):
            if not isinstance(entry, dict):
                raise OpenAIError('Invalid provider image result.', status=502, code='invalid_provider_result')
            item = {key: entry[key] for key in ('url', 'revised_prompt', 'content_type') if key in entry}
            if entry.get('b64_json') is not None:
                encoded = entry['b64_json']
                if not isinstance(encoded, str) or len(encoded) > 70 * 1024 * 1024:
                    raise OpenAIError('Invalid or oversized provider image.', status=502, code='invalid_provider_result')
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise OpenAIError('Provider returned invalid base64 image data.', status=502, code='invalid_provider_result') from exc
                if not raw or len(raw) > 50 * 1024 * 1024:
                    raise OpenAIError('Invalid or oversized provider image.', status=502, code='invalid_provider_result')
                folder = media_directory / task['id']
                folder.mkdir(parents=True, exist_ok=True)
                filename = f'generated-{index}.bin'
                (folder / filename).write_bytes(raw)
                item['_local_image'] = filename
            if not item.get('url') and not item.get('_local_image'):
                raise OpenAIError('Provider returned no image data.', status=502, code='invalid_provider_result')
            cached.append(item)
        summary = {'images': cached}
        if result.get('output_format') in ('png', 'jpeg', 'webp'):
            summary['output_format'] = result['output_format']
        return summary

    @router.get('/models')
    async def models(request: Request):
        principal = (await db_call(_principal, request, store, _store=store))
        allowed = principal.get('allowed_models') or []
        data = [{'id': item['id'], 'object': 'model', 'created': 0, 'owned_by': 'dreamina-gateway',
                 'capabilities': item.get('capabilities', {}), 'type': item['kind']}
                for item in catalog() if item.get('enabled', True) and (not allowed or item['id'] in allowed)]
        return {'object': 'list', 'data': data}

    @router.post('/videos')
    async def create_video(request: Request):
        principal = (await db_call(_principal, request, store, _store=store))
        data, files = await _parse(request, multipart=True)
        from .video_inputs import normalize_options,parse_references,validate_references,save_references
        _unknown(data, {'model','prompt','seconds','size','duration','resolution','ratio','mode','references','input_reference','user'})
        model=data.get('model',BASE_VIDEO_MODEL)
        require_model(model,'video')
        from .models import model_config
        data['prompt']=_prompt(data,model_config(model)['max_prompt_length'])
        try:
            spec=normalize_options(data)
            prepared=(await run_file_io(parse_references,data,files,stage=stage_for(request)))
            (await run_file_io(validate_references,spec,prepared))
        except (ValueError,TypeError,KeyError) as exc:
            raise OpenAIError(str(exc),code='invalid_video_parameters') from exc
        spec['references']=[item['metadata'] for item in prepared]
        task=(await db_call(store.reserve_task, principal,'video',spec['model'],spec,idempotency_key=_idempotency(request), _store=store))
        request.state.allocated_task_id=task['id']
        if (await db_call(store.claim_dispatch, task['id'], _store=store)):
            dispatch_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'dreamina-video-dispatch:'+task['id']))
            try:
                (await db_call(store.update_task, task['id'],bridge_job_id=dispatch_id, _store=store))
                (await run_file_io(save_references,media_directory,task['id'],prepared))
                if hasattr(store,'append_event'):
                    detail={'method':'POST','path':'/v1/videos','content_type':request.headers.get('content-type'),'key_id':principal['id'],
                            'parameters':spec,'reference_count':len(prepared),'new_session':True}
                    (await db_call(store.update_task, task['id'],request_detail=detail, _store=store))
                    (await db_call(store.append_event, task['id'],'request_validated',detail, _store=store))
                bridge_request={k:spec[k] for k in ('model','prompt','duration','ratio','resolution','mode','references')}
                bridge_request.update(asset_owner_task_id=task['id'],new_session=True)
                bridge=(await db_call(bridge_store.enqueue, bridge_request,account_id=task['account_id'],job_id=dispatch_id, _store=store))
                (await db_call(store.update_task, task['id'],bridge_job_id=bridge['id'], _store=store))
            except Exception as exc:
                try:
                    safe=(await db_call(bridge_store.fail_unactivated, dispatch_id, _store=store))
                    if safe:
                        closed=(await db_call(store.update_task, task['id'],expected_bridge_job_id=dispatch_id,status='failed',error={'code':'dispatch_failed','message':'Dispatch failed before generation; no upstream generation was activated.'}, _store=store))
                        safe=closed['status']=='failed' and closed.get('bridge_job_id')==dispatch_id
                except Exception:safe=False
                if safe:raise OpenAIError('Dispatch failed before generation. The task is closed and its concurrency slot released.',status=409,code='dispatch_failed',task_id=task['id']) from exc
                (await db_call(mark_uncertain, task['id'],_safe_provider_error(exc), _store=store))
                raise OpenAIError('Dispatch outcome is uncertain; inspect this task before retrying.',status=409,code='dispatch_uncertain',task_id=task['id']) from exc
        task = (await db_call(store.get_task, task['id'], principal=principal, _store=store))
        return JSONResponse(video_object(task), headers={'x-request-id': task['id']})

    @router.get('/videos')
    async def list_videos(request: Request, limit: int = 20, after: str | None = None, order: str = 'desc'):
        principal = (await db_call(_principal, request, store, _store=store))
        if not 1 <= limit <= 100 or order not in ('asc', 'desc'):
            raise OpenAIError('limit must be 1–100 and order must be asc or desc.')
        rows = (await db_call(store.list_tasks, principal=principal, kind='video', limit=100, _store=store))
        if order == 'asc':
            rows = list(reversed(rows))
        if after is not None:
            index = next((i for i, row in enumerate(rows) if row['id'] == after), None)
            if index is None:
                raise OpenAIError('Pagination cursor is not in this key’s available task window.', param='after')
            rows = rows[index + 1:]
        selected = rows[:limit]
        return {'object': 'list', 'data': [video_object(x) for x in selected],
                'first_id': selected[0]['id'] if selected else None, 'last_id': selected[-1]['id'] if selected else None,
                'has_more': len(rows) > limit}

    @router.get('/videos/{video_id}')
    async def retrieve_video(video_id: str, request: Request):
        principal = (await db_call(_principal, request, store, _store=store))
        task = await refreshed((await db_call(owned, video_id, principal, 'video', _store=store)))
        return JSONResponse(video_object(task), headers={'x-request-id': task['id']})

    @router.get('/videos/{video_id}/content')
    async def video_content(video_id: str, request: Request, variant: str = 'video'):
        principal = (await db_call(_principal, request, store, _store=store))
        task = await refreshed((await db_call(owned, video_id, principal, 'video', _store=store)))
        if variant != 'video':
            raise OpenAIError('Only variant=video is currently supported.', param='variant', code='unsupported_parameter')
        if task['status'] != 'completed':
            raise OpenAIError('Video is not completed.', status=409, code='video_not_ready', task_id=task['id'])
        if download_video is None:
            raise OpenAIError('Video download provider is not configured.', status=503, code='capability_unavailable')
        result = await _call(download_video, task)
        headers = {'x-request-id': task['id']}
        if isinstance(result, bytes):
            return Response(result, media_type='video/mp4', headers=headers)
        if isinstance(result, (Path, str)) and Path(result).is_file():
            return FileResponse(result, media_type='video/mp4', filename=f'{task["id"]}.mp4', headers=headers)
        raise OpenAIError('Video download provider returned no file.', status=502, code='invalid_provider_result')

    async def generate_image_request(request: Request, *, edit: bool = False):
        principal = (await db_call(_principal, request, store, _store=store))
        data, files = await _parse(request, multipart=edit)
        _unknown(data, {'model', 'prompt', 'n', 'size', 'response_format', 'quality', 'style', 'user', 'stream',
                        'background', 'output_format', 'output_compression', 'moderation', 'image', 'mask'})
        if data.get('stream') not in (None, False, 'false'):
            raise OpenAIError('Streaming image generation is not implemented.', param='stream', code='unsupported_parameter')
        model = data.get('model', BASE_IMAGE_MODEL)
        item = require_model(model, 'image')
        capabilities = item.get('capabilities') or {}
        if image_generate is None or edit and not capabilities.get('edits', False):
            raise OpenAIError('Image editing is not yet enabled.' if edit else 'Image generation provider is not configured.',
                              status=503, code='capability_unavailable')
        prompt = _prompt(data, min(2000, int(capabilities.get('max_prompt_length', 2000))))
        n = _integer(data.get('n', 1), 'n', 1, 10)
        if n not in capabilities.get('n', [1]):
            raise OpenAIError('This model currently supports n=1.', param='n', code='unsupported_parameter')
        size = data.get('size', capabilities.get('default_size', '2048x2048'))
        if size not in capabilities.get('sizes', ['2048x2048']):
            raise OpenAIError('Unsupported image size for this model.', param='size', code='unsupported_parameter')
        response_format = data.get('response_format', 'url')
        if response_format not in ('url', 'b64_json'):
            raise OpenAIError('response_format must be url or b64_json.', param='response_format')
        # Avoid silently dropping OpenAI options the Dreamina provider cannot honor.
        for field, accepted in [('quality', capabilities.get('quality', ['auto'])), ('style', capabilities.get('style', [])),
                                ('background', capabilities.get('background', ['auto'])), ('output_format', capabilities.get('output_format', [])),
                                ('moderation', ['auto'])]:
            if data.get(field) is not None and data[field] not in accepted:
                raise OpenAIError(f'{field} is not supported by this provider.', param=field, code='unsupported_parameter')
        if data.get('output_compression') is not None or data.get('mask') is not None or any(x['field'] == 'mask' for x in files):
            raise OpenAIError('Masks and output compression are not supported by this provider.', code='unsupported_parameter')
        if not edit and data.get('image') is not None:
            raise OpenAIError('Use /v1/images/edits for image inputs.', param='image')
        image_files = [x for x in files if x['field'] in ('image', 'image[]')]
        if edit and (not image_files or len(image_files) != len(files)):
            raise OpenAIError('Image edits require multipart image or image[] file parts.', param='image')
        if any(x['content_type'] not in ('image/png', 'image/jpeg', 'image/webp') or x.get('size_bytes',len(x.get('bytes',b'')))<=0 for x in image_files):
            raise OpenAIError('Input images must be nonempty PNG, JPEG or WebP files.', param='image')
        spec = {'model': model, 'prompt': prompt, 'n': n, 'size': size, 'response_format': response_format,
                'operation': 'edit' if edit else 'generate'}
        for field in ('quality', 'style', 'background', 'output_format', 'moderation'):
            if data.get(field) is not None:
                spec[field] = data[field]
        if image_files:
            spec['input_images'] = [{'sha256': x.get('sha256') or hashlib.sha256(x['bytes']).hexdigest(), 'content_type': x['content_type']}
                                    for x in image_files]
        task = (await db_call(store.reserve_task, principal, 'image', model, spec, idempotency_key=_idempotency(request), _store=store))
        request.state.allocated_task_id = task['id']
        if not (await db_call(store.claim_dispatch, task['id'], _store=store)):
            finish_upload_input(request)
            task = await refreshed((await db_call(store.get_task, task['id'], principal=principal, _store=store)))
            return JSONResponse(await image_response(task), headers={'x-request-id': task['id']})
        (await db_call(store.update_task, task['id'], status='in_progress', _store=store))
        provider_spec = dict(spec)
        if image_files:
            folder = media_directory / task['id']
            folder.mkdir(parents=True, exist_ok=True)
            provider_spec['input_images'] = []
            for i, upload in enumerate(image_files):
                ext = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[upload['content_type']]
                path = folder / f'{i}{ext}'
                await run_file_io(persist_upload,upload,path)
                provider_spec['input_images'].append(str(path))
        finish_upload_input(request)
        try:
            timeout_seconds = float(image_timeout() if callable(image_timeout) else image_timeout)
            result = await asyncio.wait_for(_call(image_generate, provider_spec, (await db_call(store.settings_for_account, task['account_id'], _store=store)), task=task), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            current = (await db_call(store.get_task, task['id'], principal=principal, _store=store))
            if current['status'] not in ('completed', 'failed', 'submitted'):
                (await db_call(mark_uncertain, task['id'], {'code': 'provider_timeout', 'message': 'Outcome needs reconciliation; do not submit again.'}, _store=store))
            raise OpenAIError('Image generation timed out. Resume the original task; do not submit again.', status=409,
                              code='generation_timeout', error_type='server_error', task_id=task['id']) from exc
        except asyncio.CancelledError:
            (await db_call(mark_uncertain, task['id'], {'code': 'request_interrupted', 'message': 'Submission outcome needs reconciliation.'}, _store=store))
            raise
        except Exception as exc:
            current = (await db_call(store.get_task, task['id'], principal=principal, _store=store))
            if current['status'] not in ('completed', 'failed', 'uncertain'):
                (await db_call(mark_uncertain, task['id'], _safe_provider_error(exc), _store=store))
            raise OpenAIError('Image provider did not return a completed result. Check the original task before retrying.',
                              status=409, code=str(getattr(exc, 'code', 'provider_error')), error_type='server_error', task_id=task['id']) from exc
        if not isinstance(result, dict) or not isinstance(result.get('images', result.get('data')), list) or not result.get('images', result.get('data')):
            (await db_call(mark_uncertain, task['id'], {'code': 'invalid_provider_result', 'message': 'No confirmed image result was returned.'}, _store=store))
            raise OpenAIError('Provider returned no confirmed image result.', status=502, code='invalid_provider_result', task_id=task['id'])
        try:
            result = (await db_call(cache_image_result, task, result, _store=store))
        except OpenAIError:
            current = (await db_call(store.get_task, task['id'], principal=principal, _store=store))
            if current['status'] not in ('completed', 'failed'):
                (await db_call(mark_uncertain, task['id'], {'code': 'invalid_provider_result', 'message': 'Provider result needs reconciliation.'}, _store=store))
            raise
        (await db_call(store.update_task, task['id'], status='completed', result=result, progress=100, completed_at=time.time(), error=None, _store=store))
        task = (await db_call(store.get_task, task['id'], principal=principal, _store=store))
        return JSONResponse(await image_response(task), headers={'x-request-id': task['id']})

    @router.post('/images/generations')
    async def create_image(request: Request):
        return await generate_image_request(request)

    @router.post('/images/edits')
    async def edit_image(request: Request):
        return await generate_image_request(request, edit=True)

    @router.get('/images/jobs/{task_id}')
    async def image_job(task_id: str, request: Request):
        """Gateway recovery extension; not part of the official Images API."""
        task = await refreshed((await db_call(owned, task_id, (await db_call(_principal, request, store, _store=store)), 'image', _store=store)))
        return {'id': task['id'], 'object': 'image_job', 'status': task['status'], 'model': task['model'],
                'created_at': _timestamp(task['created_at']), 'completed_at': _timestamp(task.get('completed_at')),
                'error': task.get('error')}

    @router.get('/images/jobs/{task_id}/result')
    async def image_job_result(task_id: str, request: Request):
        task = await refreshed((await db_call(owned, task_id, (await db_call(_principal, request, store, _store=store)), 'image', _store=store)))
        return JSONResponse(await image_response(task), headers={'x-request-id': task['id']})

    return router


def augment_openapi(schema: dict) -> dict:
    """Add documentation for manually parsed public requests; no runtime changes.

    Call this from the application's custom ``app.openapi`` before caching the
    result. Only already-registered gateway routes are annotated. The operation
    is idempotent and deliberately does not set a global security requirement.
    """
    components = schema.setdefault('components', {})
    components.setdefault('securitySchemes', {})['GatewayBearer'] = {
        'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'sk-dr-…',
        'description': ('在本平台管理后台创建的 API 密钥，格式为 sk-dr-…；不是 OpenAI 密钥、'
                        'Dreamina Cookie、管理员密码或浏览器配对密钥。只填写原始密钥，Swagger 自动添加 Bearer 前缀。'),
    }
    definitions = components.setdefault('schemas', {})
    image_props = {
        'model': {'type': 'string', 'enum': [BASE_IMAGE_MODEL], 'default': BASE_IMAGE_MODEL},
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 2000,
                   'description': '生成提示词。去除首尾空白后按 UTF-16 计数，最多 2000 个代码单元。'},
        'n': {'type': 'integer', 'enum': [1], 'default': 1},
        'size': {'type': 'string', 'enum': ['2048x2048'], 'default': '2048x2048'},
        'response_format': {'type': 'string', 'enum': ['url', 'b64_json'], 'default': 'url',
                            'description': 'url 返回媒体地址；b64_json 返回经过验证并缓存的图像数据。'},
        'quality': {'type': 'string', 'enum': ['auto'], 'default': 'auto'},
        'background': {'type': 'string', 'enum': ['auto']},
        'output_format': {'type': 'string', 'enum': ['jpeg'], 'description': '当前原生图片流程输出 JPEG。'},
        'moderation': {'type': 'string', 'enum': ['auto']},
        'stream': {'type': 'boolean', 'enum': [False], 'default': False,
                   'description': '当前不支持流式图片输出。'},
        'user': {'type': 'string', 'description': '兼容字段；不用于选择上游账号。'},
    }
    video_props = {
        'model': {'type': 'string', 'enum': ['seedance-2.5','seedance-2.0-mini','seedance-2.0-fast','seedance-2.0'], 'default': BASE_VIDEO_MODEL},
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 15000,
                   'description': ('2.5最多15000，其余4000个UTF-16代码单元。支持精准引用 @图片1、@图片2、@视频1：'
                                   '图片/视频分别按同类型素材顺序从1编号，转换为对应素材标签，不按文件名匹配；同一编号可重复。'
                                   '使用无前导零的正整数（半角数字）；缺失、无效或超出上传数量的编号在创建任务前返回400。'
                                   '普通提示词不需要@；这些引用语法是本网关扩展。')},
        'seconds': {'oneOf': [{'type': 'string', 'pattern':'^[0-9]+$'}, {'type': 'integer','minimum':4,'maximum':30}],
                    'default': '4', 'description': '2.5支持4–30秒；2.0系列4–15秒。'},
        'duration': {'type':'integer','minimum':4,'maximum':30,'description':'seconds的别名；两者同时提供必须一致。'},
        'size': {'type': 'string', 'example':'1280x720','description':'原生输出尺寸；与ratio/resolution同时提供必须一致。'},
        'resolution':{'type':'string','enum':['480p','720p','1080p'],'default':'720p'},
        'ratio':{'type':'string','enum':['21:9','16:9','4:3','1:1','3:4','9:16'],'default':'16:9'},
        'mode':{'type':'string','enum':['omni']},
        'references':{'type':'array','description':('网关扩展：多图/视频Base64 data URL；图片和视频分别按数组内同类型顺序对应@图片N/@视频N。'
                                                  'name仅作素材说明，不决定引用编号。推荐只选JSON或multipart一种传法；混用时依次为data URL形式的input_reference、references数组、multipart文件，再按类型编号。'
                                                  '也可使用multipart reference_images/reference_videos；不接受任意远程URL或服务器路径。'),
            'items':{'type':'object','required':['type','data_url'],'additionalProperties':False,
                     'properties':{'type':{'type':'string','enum':['image','video']},'data_url':{'type':'string'},'name':{'type':'string'}}}},
        'input_reference':{'description':'兼容单张图片data URL；此data URL在JSON references之前编号为@图片1，其余图片顺延。multipart文件形式则按文件接收顺序。不接受服务器本地路径或任意远程URL。'},
        'user': {'type': 'string', 'description': '兼容字段；不用于选择上游账号。'},
    }
    definitions.update({
        'GatewayImageGenerationRequest': {
            'type': 'object', 'required': ['prompt'], 'additionalProperties': False,
            'properties': image_props,
            'example': {'model': BASE_IMAGE_MODEL, 'prompt': 'A small blue ceramic vase on a wooden table.',
                        'n': 1, 'size': '2048x2048', 'response_format': 'b64_json'},
        },
        'GatewayVideoCreateRequest': {
            'type': 'object', 'required': ['prompt'], 'additionalProperties': False,
            'properties': video_props,
            'example': {'model': BASE_VIDEO_MODEL, 'prompt': 'Soft watercolor clouds drift above purple mountains.',
                        'seconds': '4', 'size': '1280x720'},
        },
        'GatewayVideoCreateForm': {
            'type': 'object', 'required': ['prompt'], 'additionalProperties': False,
            'properties': dict(video_props, seconds={'type': 'string', 'default': '4'},
                reference_images={'type':'array','description':'重复同名字段上传多张图片；接收顺序对应@图片1、@图片2等。单张图片提示词示例：让 @图片1 中的人物走起来。','items':{'type':'string','format':'binary'}},
                reference_videos={'type':'array','description':'重复同名字段上传多个视频；视频独立编号为@视频1等。混合示例：让 @图片1 中的人物在 @图片2 的场景中按照 @视频1 的步伐行走。','items':{'type':'string','format':'binary'}}),
        },
        'GatewayImageEditRequest': {
            'type': 'object', 'required': ['prompt', 'image'],
            'description': '兼容协议占位；本实例尚未启用图片编辑，正常请求返回 503 capability_unavailable。',
            'properties': dict(image_props, image={'type': 'string', 'format': 'binary',
                                                   'description': '图片文件；编辑能力尚未启用。'}),
        },
        'GatewayError': {
            'type': 'object', 'required': ['message', 'type', 'param', 'code'],
            'properties': {'message': {'type': 'string'}, 'type': {'type': 'string'},
                           'param': {'type': ['string', 'null']}, 'code': {'type': 'string'}},
        },
        'GatewayErrorResponse': {
            'type': 'object', 'required': ['error'],
            'properties': {'error': {'$ref': '#/components/schemas/GatewayError'},
                           'task_id': {'type': 'string', 'description': '已分配任务时返回；用于恢复，不要重新生成。'}},
        },
        'GatewayVideo': {
            'type': 'object', 'required': ['id', 'object', 'model', 'status', 'created_at'],
            'properties': {
                'id': {'type': 'string', 'description': '本平台视频 ID，格式为 video_…，不是浏览器 submit_id。'},
                'object': {'type': 'string', 'enum': ['video']}, 'model': {'type': 'string'},
                'status': {'type': 'string', 'enum': ['queued', 'in_progress', 'completed', 'failed']},
                'progress': {'type': 'integer', 'minimum': 0, 'maximum': 100,
                             'description': '仅在观察到真实百分比时返回；确认完成为100。未知时省略。'},
                'progress_details':{'type':'object','description':'进度来源、阶段和观测时间。'},
                'duration':{'type':'integer','description':'已知的请求时长，秒。'},
                'resolution':{'type':'string'},
                'billing_status':{'type':'string','description':'仅有真实上游结算证据时返回，任务完成不自动等于settled。'},
                'usage':{'type':'object','description':'上游确认的用量；不估算completion_tokens。'},
                'created_at': {'type': 'integer', 'format': 'int64'},
                'completed_at': {'type': ['integer', 'null'], 'format': 'int64'},
                'expires_at': {'type': ['integer', 'null'], 'format': 'int64'},
                'seconds': {'type': 'string'}, 'size': {'type': 'string'},
                'prompt': {'type': ['string', 'null']}, 'error': {'type': ['object', 'null']},
                'remixed_from_video_id': {'type': ['string', 'null']},
                'metadata': {'type': 'object', 'description': '网关扩展：submission_retry提供attempt/max_attempts/stage/next_retry_at/reason；明确未受理的临时故障最多新会话重试一次，video_id不变。-6、审核拒绝和结果未知不自动重发。verification_required=true表示需核对；original_task_pending=true表示原任务仍在后台查询。不代表上游已取消或无需扣费。'},
            },
        },
        'GatewayVideoList': {
            'type': 'object', 'required': ['object', 'data', 'has_more'],
            'properties': {'object': {'type': 'string', 'enum': ['list']},
                           'data': {'type': 'array', 'items': {'$ref': '#/components/schemas/GatewayVideo'}},
                           'first_id': {'type': ['string', 'null']}, 'last_id': {'type': ['string', 'null']},
                           'has_more': {'type': 'boolean'}},
        },
        'GatewayImage': {
            'type': 'object', 'oneOf': [{'required': ['url']}, {'required': ['b64_json']}],
            'properties': {'url': {'type': 'string', 'format': 'uri'},
                           'b64_json': {'type': 'string', 'contentEncoding': 'base64'},
                           'revised_prompt': {'type': 'string'}},
        },
        'GatewayImagesResponse': {
            'type': 'object', 'required': ['created', 'data'],
            'properties': {'created': {'type': 'integer', 'format': 'int64'},
                           'data': {'type': 'array', 'items': {'$ref': '#/components/schemas/GatewayImage'}},
                           'output_format': {'type': 'string', 'enum': ['png', 'jpeg', 'webp']}},
        },
        'GatewayImageJob': {
            'type': 'object', 'required': ['id', 'object', 'status', 'model', 'created_at'],
            'properties': {'id': {'type': 'string'}, 'object': {'type': 'string', 'enum': ['image_job']},
                           'status': {'type': 'string'}, 'model': {'type': 'string'},
                           'created_at': {'type': 'integer', 'format': 'int64'},
                           'completed_at': {'type': ['integer', 'null'], 'format': 'int64'},
                           'error': {'type': ['object', 'string', 'null']}},
        },
        'GatewayModelsResponse': {
            'type': 'object', 'required': ['object', 'data'],
            'properties': {'object': {'type': 'string', 'enum': ['list']}, 'data': {'type': 'array', 'items': {
                'type': 'object', 'required': ['id', 'object', 'owned_by'],
                'properties': {'id': {'type': 'string'}, 'object': {'type': 'string', 'enum': ['model']},
                               'created': {'type': 'integer'}, 'owned_by': {'type': 'string'},
                               'type': {'type': 'string', 'enum': ['image', 'video']},
                               'capabilities': {'type': 'object'}},
            }}},
        },
    })
    paths = schema.get('paths', {})
    operations = {
        ('/v1/models', 'get'): ('列出可调用模型', 'GatewayModelsResponse'),
        ('/v1/videos', 'post'): ('创建视频任务（历史 Videos 协议兼容）', 'GatewayVideo'),
        ('/v1/videos', 'get'): ('列出当前 API 密钥的视频任务', 'GatewayVideoList'),
        ('/v1/videos/{video_id}', 'get'): ('查询视频任务', 'GatewayVideo'),
        ('/v1/videos/{video_id}/content', 'get'): ('下载已完成视频', None),
        ('/v1/images/generations', 'post'): ('生成图片', 'GatewayImagesResponse'),
        ('/v1/images/edits', 'post'): ('编辑图片（尚未启用）', None),
        ('/v1/images/jobs/{task_id}', 'get'): ('恢复图片任务：查询状态（网关扩展）', 'GatewayImageJob'),
        ('/v1/images/jobs/{task_id}/result', 'get'): ('恢复图片任务：取得结果（网关扩展）', 'GatewayImagesResponse'),
    }
    error_descriptions = {'400': '参数错误或不支持的选项。', '401': 'API 密钥缺失、失效或已撤销。',
                          '403': '密钥无权使用此模型或账号池。', '404': '资源不存在，或不属于当前 API 密钥。',
                          '409': '已有任务待核对或结果暂不可用；使用 task_id 恢复，不要重新提交。',
                          '413': '请求超过网关大小限制。', '415': '不支持的 Content-Type。',
                          '429': '密钥额度、速率或并发限制。', '503': '分配任务前能力或可用账号不足。'}
    for (path, method), (summary, response_name) in operations.items():
        operation = paths.get(path, {}).get(method)
        if operation is None:
            continue
        operation['summary'] = summary
        operation['security'] = [{'GatewayBearer': []}]
        responses = operation.setdefault('responses', {})
        # The compatibility route converts FastAPI parameter-validation errors
        # into the gateway's 400 envelope rather than FastAPI's default 422.
        responses.pop('422', None)
        for status, description in error_descriptions.items():
            responses[status] = {'description': description,
                                 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/GatewayErrorResponse'}}}}
        if response_name:
            responses['200'] = {'description': '成功。', 'headers': {'x-request-id': {'schema': {'type': 'string'},
                                  'description': '已分配的任务 ID（任务相关操作）。'}},
                                'content': {'application/json': {'schema': {'$ref': f'#/components/schemas/{response_name}'}}}}
        if '/videos' in path:
            operation['description'] = ('自建 Dreamina 网关对历史 OpenAI Videos 协议的兼容实现；'
                '不调用 OpenAI/Sora 服务。每任务独立新会话，四模型原生参数、多图/视频参考。查到失败任务仍返回HTTP200及error；可用参数以/v1/models和实际账号为准。')
        if '/images/jobs/' in path:
            operation['description'] = '本平台的任务恢复扩展，不属于标准 OpenAI Images API。只允许任务所属 API 密钥查询。'
    for path, formats in {
        '/v1/videos': {'application/json': 'GatewayVideoCreateRequest', 'multipart/form-data': 'GatewayVideoCreateForm'},
        '/v1/images/generations': {'application/json': 'GatewayImageGenerationRequest'},
        '/v1/images/edits': {'multipart/form-data': 'GatewayImageEditRequest'},
    }.items():
        operation = paths.get(path, {}).get('post')
        if operation is None:
            continue
        operation['requestBody'] = {'required': True, 'content': {
            kind: {'schema': {'$ref': f'#/components/schemas/{name}'}} for kind, name in formats.items()}}
        existing = [p for p in operation.get('parameters', []) if p.get('name', '').lower() != 'idempotency-key']
        operation['parameters'] = existing + [{'name': 'Idempotency-Key', 'in': 'header', 'required': False,
            'description': '付费请求强烈建议提供。相同密钥与相同请求重放返回原任务，不能用于不同请求。',
            'schema': {'type': 'string', 'minLength': 1, 'maxLength': 200}}]
    edit = paths.get('/v1/images/edits', {}).get('post')
    if edit is not None:
        edit['x-capability-status'] = 'unavailable'
        edit['description'] = ('接口形状为兼容占位。当前实例未启用图片编辑；有效鉴权后的正常编辑请求返回 '
                               '503 capability_unavailable，不创建任务，不消耗生成额度。请勿将此声明视为已支持编辑。')
        edit['responses'].pop('200', None)
    content = paths.get('/v1/videos/{video_id}/content', {}).get('get')
    if content is not None:
        content['responses']['200'] = {'description': 'MP4 视频字节。', 'content': {
            'video/mp4': {'schema': {'type': 'string', 'format': 'binary'}}}}
    return schema
