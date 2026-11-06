"""The first image capability verified against the live Dreamina UI."""
IMAGE_MODEL = 'seedream-5.0-lite'
IMAGE_MODEL_NAME = 'Seedream 5.0 Lite'
IMAGE_UPSTREAM_MODEL = 'high_aes_general_v50'


def validate_image_spec(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError('Image generation request must be an object.')
    allowed = {'kind', 'model', 'prompt', 'size', 'n', 'ratio', 'resolution'}
    if set(value) - allowed:
        raise ValueError('Image bridge supports text-to-image only; references and masks are not enabled.')
    result = {'kind': 'image', 'model': IMAGE_MODEL, 'size': '2048x2048', 'n': 1,
              'ratio': '1:1', 'resolution': '2K', **value}
    if (result['kind'] != 'image' or result['model'] != IMAGE_MODEL or result['size'] != '2048x2048'
            or type(result['n']) is not int or result['n'] != 1
            or result['ratio'] != '1:1' or str(result['resolution']).lower() != '2k'):
        raise ValueError('Image bridge currently supports Seedream 5.0 Lite, 2048x2048, n=1.')
    prompt = result.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-16-le')) // 2 > 2000:
        raise ValueError('Image prompt must contain 1–2000 UTF-16 characters.')
    result['prompt'] = prompt.strip()
    return result


def normalize_image_record(record: dict, submit_id: str) -> dict:
    status = record.get('status', (record.get('task') or {}).get('status'))
    images = []
    for item in record.get('item_list') or []:
        for image in (item.get('image') or {}).get('large_images') or []:
            if image.get('image_url'):
                images.append({'url': image['image_url'], 'width': image.get('width'),
                               'height': image.get('height'), 'format': image.get('format')})
    state = 'completed' if status == 50 and images else 'failed' if status in (10, 30, 40, 100) else 'processing'
    message = record.get('fail_starling_message')
    if message in (None, '', '[none]'):
        message = record.get('fail_starling_key') or 'Image generation failed.'
    return {'id': submit_id, 'object': 'image', 'status': state, 'upstream_status': status,
            'images': images, 'failure': {'code': str(record.get('fail_code') or status), 'message': message}
            if state == 'failed' else None}
