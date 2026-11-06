"""Official OpenAI SDK pointed at the self-hosted Dreamina gateway."""
import argparse
import base64
import json
import os
from pathlib import Path
import time
import uuid

from openai import OpenAI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['models', 'image', 'video', 'resume'])
    parser.add_argument('--prompt', default='A turquoise ceramic bowl on an ivory surface, soft light, no text.')
    parser.add_argument('--id', help='Existing video ID for resume')
    parser.add_argument('--request-id', help='Unique per new paid request; reuse on retry')
    parser.add_argument('--output', default='outputs/sdk-result')
    args = parser.parse_args()
    key = os.environ.get('DREAMINA_API_KEY')
    if not key:
        parser.error('Set DREAMINA_API_KEY to a key created in your own management console.')
    client = OpenAI(api_key=key, base_url=os.environ.get('DREAMINA_BASE_URL', 'http://127.0.0.1:8787/v1'),
                    max_retries=0, timeout=240)
    if args.operation == 'models':
        print([item.id for item in client.models.list().data]); return
    request_id = args.request_id or uuid.uuid4().hex
    print(json.dumps({'idempotency_key': request_id}), flush=True)
    headers = {'Idempotency-Key': request_id}
    if args.operation == 'image':
        result = client.images.generate(model='seedream-5.0-lite', prompt=args.prompt, n=1,
                    size='2048x2048', response_format='b64_json', extra_headers=headers)
        path = Path(args.output).with_suffix('.jpeg'); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result.data[0].b64_json)); print(str(path.resolve())); return
    if args.operation == 'resume':
        if not args.id: parser.error('--id is required for resume')
        video = client.videos.retrieve(args.id)
    else:
        video = client.videos.create(model='seedance-2.0-mini', prompt=args.prompt,
                    seconds='4', size='1280x720', extra_headers=headers)
    print(json.dumps({'video_id': video.id}), flush=True)
    deadline = time.monotonic() + 1800
    while video.status not in ('completed','failed') and time.monotonic() < deadline:
        if (video.model_dump().get('metadata') or {}).get('verification_required'):
            print(video.model_dump_json())
            raise SystemExit(2)
        time.sleep(5); video = client.videos.retrieve(video.id)
    if video.status == 'completed':
        path = Path(args.output).with_suffix('.mp4'); path.parent.mkdir(parents=True, exist_ok=True)
        client.videos.download_content(video.id).write_to_file(path); print(str(path.resolve()))
    else:
        print(video.model_dump_json()); raise SystemExit(1)


if __name__ == '__main__':
    main()
