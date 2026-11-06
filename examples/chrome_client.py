"""Submit once, then reconcile and download through the local Chrome bridge API."""
import argparse
import json
import os
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('request', nargs='?', default=str(Path(__file__).with_name('chrome-job.json')))
    parser.add_argument('--resume', help='Resume a local bridge job ID without submitting again')
    parser.add_argument('--timeout', type=float, default=1200)
    parser.add_argument('--output', default='outputs/chrome-result.mp4')
    args = parser.parse_args()
    headers = {}
    if os.environ.get('LOCAL_API_KEY'):
        headers['Authorization'] = 'Bearer ' + os.environ['LOCAL_API_KEY']
    base = 'http://127.0.0.1:8787'
    with httpx.Client(base_url=base, headers=headers, timeout=60) as client:
        if args.resume:
            job_id = args.resume
        else:
            response = client.post('/v1/chrome/jobs', json=json.loads(Path(args.request).read_text(encoding='utf-8-sig')))
            response.raise_for_status()
            job_id = response.json()['id']
        print(json.dumps({'local_job_id': job_id, 'resume': f'python examples/chrome_client.py --resume {job_id}'}), flush=True)
        deadline = time.monotonic() + args.timeout
        last_status = None
        while time.monotonic() < deadline:
            response = client.post(f'/v1/chrome/jobs/{job_id}/sync')
            response.raise_for_status()
            job = response.json()
            state = (job['status'], (job.get('result') or {}).get('upstream_status'))
            if state != last_status:
                print(json.dumps({'status': state[0], 'upstream_status': state[1]}, ensure_ascii=False), flush=True)
                last_status = state
            if job['status'] == 'completed':
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                with client.stream('GET', f'/v1/chrome/jobs/{job_id}/content') as stream:
                    stream.raise_for_status()
                    with path.open('wb') as file:
                        for chunk in stream.iter_bytes():
                            file.write(chunk)
                print(json.dumps({'downloaded': str(path.resolve())}, ensure_ascii=False))
                return
            if job['status'] in ('failed', 'uncertain'):
                print(json.dumps({'job_id': job_id, 'status': job['status'], 'error': job.get('error'),
                                  'failure': (job.get('result') or {}).get('failure')}, ensure_ascii=False))
                raise SystemExit(1)
            time.sleep(5)
        print(json.dumps({'job_id': job_id, 'status': 'wait_timeout', 'message': 'Resume this ID; do not submit again.'}))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
