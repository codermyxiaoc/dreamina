import argparse
import asyncio
import json
from pathlib import Path
import sys

from .client import DreaminaClient, DreaminaError, SubmissionUncertain
from .config import ROOT, Settings
from .models import get_models
from .payloads import build_generation
from .uploads import UploadError


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def run(args):
    config = Settings.load()
    if args.command == 'models':
        output(get_models())
        return
    if args.command in ('prepare', 'generate'):
        spec = json.loads(Path(args.request).read_text(encoding='utf-8-sig'))
        workspace = spec.pop('workspace_id', None) or config.workspace_id or None
        if args.command == 'prepare':
            output(build_generation(spec, workspace_id=workspace))
            return
    async with DreaminaClient(config) as client:
        if args.command == 'check':
            output({'authenticated': True, 'credits': await client.credits()})
        elif args.command == 'generate':
            from .service import create_video
            output(await create_video(client, spec, workspace_id=workspace,
                                      wait_seconds=args.wait, download=args.download))
        elif args.command == 'poll':
            output(await client.wait(args.id, timeout=args.wait) if args.wait else await client.poll(args.id))
        elif args.command == 'download':
            destination = Path(args.output) if args.output else ROOT / 'outputs' / f'{args.id}.mp4'
            output({'path': str(await client.download(args.id, destination))})
        elif args.command == 'upload':
            from .uploads import AssetUploader
            uploader = AssetUploader(client)
            output(await (uploader.upload_video(Path(args.path), experimental=True) if args.type == 'video' else uploader.upload_image(Path(args.path))))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Dreamina session-token local API and CLI')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('models')
    sub.add_parser('check')
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=8787)
    serve.add_argument('--host', default='127.0.0.1')
    for name in ('prepare', 'generate'):
        item = sub.add_parser(name)
        item.add_argument('request', help='JSON request file')
        if name == 'generate':
            item.add_argument('--wait', type=float, default=0, help='Wait timeout in seconds')
            item.add_argument('--download', action='store_true')
    poll = sub.add_parser('poll')
    poll.add_argument('id')
    poll.add_argument('--wait', type=float, default=0)
    download = sub.add_parser('download')
    download.add_argument('id')
    download.add_argument('--output')
    upload = sub.add_parser('upload')
    upload.add_argument('path')
    upload.add_argument('--type', choices=['image', 'video'], default='image')
    args = parser.parse_args()
    if args.command == 'serve':
        import uvicorn
        uvicorn.run('dreamina_api.server:app', host=args.host, port=args.port, log_level='warning', ws='none')
        return
    try:
        asyncio.run(run(args))
    except (DreaminaError, UploadError, ValueError, NotImplementedError, OSError) as error:
        result = {'error': {'code': getattr(error, 'code', type(error).__name__), 'message': str(error)}}
        if isinstance(error, SubmissionUncertain):
            result['submit_id'] = error.submit_id
        output(result)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
