"""Shared API/CLI orchestration with a durable submission receipt."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .client import DreaminaClient, DreaminaError, SubmissionUncertain
from .config import ROOT
from .payloads import build_generation
from .models import validate_request


def save_receipt(submit_id: str, value: dict) -> None:
    folder = ROOT / 'outputs' / 'tasks'
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = ''.join(c for c in submit_id if c.isalnum() or c == '-')
    path = folder / f'{safe_name}.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


async def resolve_assets(client: DreaminaClient, spec: dict) -> dict:
    from .uploads import AssetUploader
    result = deepcopy(spec)
    uploader = None

    async def one(value, default_type='image'):
        nonlocal uploader
        if isinstance(value, str) or isinstance(value, dict) and 'path' in value:
            if uploader is None:
                uploader = AssetUploader(client)
        if isinstance(value, str):
            return await uploader.upload_image(Path(value))
        if isinstance(value, dict) and 'path' in value:
            if value.get('type', default_type) == 'video':
                return await uploader.upload_video(Path(value['path']), experimental=True)
            return await uploader.upload_image(Path(value['path']))
        return value

    for key in ('first_frame', 'end_frame'):
        if result.get(key) is not None:
            result[key] = await one(result[key])
    if 'references' in result:
        result['references'] = [await one(item) for item in result['references']]
    return result


def preflight(spec: dict) -> None:
    """Check parameters/counts before any upload; real metadata is checked afterwards."""
    sample = deepcopy(spec)

    def placeholder(value):
        if isinstance(value, str):
            value = {'path': value, 'type': 'image'}
        if not isinstance(value, dict) or 'path' not in value:
            return value
        kind = value.get('type', 'image')
        if kind not in ('image', 'video'):
            raise ValueError('Local assets must have type=image or type=video.')
        if not isinstance(value['path'], str) or not Path(value['path']).is_file():
            raise ValueError('A local asset path does not exist or is not a file.')
        if kind == 'image':
            return {'type': 'image', 'image_uri': 'pending-local-upload', 'width': 1024, 'height': 1024}
        return {'type': 'video', 'vid': 'pending-local-upload', 'width': 1024,
                'height': 1024, 'duration': 2000, 'fps': 24}

    for key in ('first_frame', 'end_frame'):
        if sample.get(key) is not None:
            sample[key] = placeholder(sample[key])
    if isinstance(sample.get('references'), list):
        sample['references'] = [placeholder(item) for item in sample['references']]
    validate_request(sample)


async def create_video(client: DreaminaClient, spec: dict, *, workspace_id: str | None = None,
                       wait_seconds: float = 0, download: bool = False) -> dict:
    preflight(spec)
    spec = await resolve_assets(client, spec)
    prepared = build_generation(spec, workspace_id=workspace_id or client.settings.workspace_id or None)
    submit_id = prepared['submit_id']
    save_receipt(submit_id, {'id': submit_id, 'status': 'submitting', 'request': spec,
                             'model_req_key': prepared['model_req_key']})
    try:
        result = await client.submit(prepared)
    except DreaminaError as error:
        save_receipt(submit_id, {'id': submit_id, 'status': 'uncertain' if isinstance(error, SubmissionUncertain) else 'rejected',
                                'error': error.code, 'message': str(error)})
        raise
    save_receipt(submit_id, result)
    if wait_seconds:
        try:
            result = await client.wait(submit_id, timeout=wait_seconds)
        except DreaminaError as error:
            result['poll_error'] = {'code': error.code, 'message': str(error)}
            result['resume_with_submit_id'] = submit_id
        save_receipt(submit_id, result)
    if download and result['status'] == 'completed':
        path = await client.download(submit_id, ROOT / 'outputs' / f'{submit_id}.mp4')
        result['local_path'] = str(path)
        save_receipt(submit_id, result)
    return result
