"""Validated video options and private, content-addressed reference attachments."""
from __future__ import annotations
import base64
import hashlib
import json
import mmap
from pathlib import Path
import re

from .models import model_config, validate_request
from .uploads import image_dimensions, mp4_metadata
from .prompt_mentions import parse_prompt_mentions

MAX_REFERENCE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 70 * 1024 * 1024
RESOLUTIONS = {'480p':480, '720p':720, '1080p':1080}


def size_options(model):
    out = {}
    for resolution in model['resolutions']:
        short = RESOLUTIONS.get(resolution.lower())
        if not short: continue  # 2K/4K post-upscaling is a separate unverified path.
        for ratio in model['ratios']:
            a,b = map(int, ratio.split(':'))
            w,h = (round(short*a/b),short) if a>=b else (short,round(short*b/a))
            out[f'{w}x{h}'] = {'ratio':ratio,'resolution':resolution.lower()}
    return out


def normalize_options(data):
    model = model_config(data.get('model','seedance-2.0-mini'))
    seconds = data.get('seconds', data.get('duration',4))
    if isinstance(seconds,bool) or not re.fullmatch(r'\d+',str(seconds)):
        raise ValueError('seconds/duration must be an integer number of seconds.')
    seconds=int(seconds)
    if 'seconds' in data and 'duration' in data and str(data['seconds'])!=str(data['duration']):
        raise ValueError('seconds and duration disagree.')
    sizes=size_options(model)
    size=data.get('size')
    if size is not None:
        if size not in sizes: raise ValueError('size is not an available native resolution/ratio for this model.')
        selected=sizes[size]
        for key in ('resolution','ratio'):
            if key in data and str(data[key]).lower()!=selected[key].lower(): raise ValueError(f'{key} conflicts with size.')
    else:
        selected={'resolution':str(data.get('resolution','720p')).lower(),'ratio':data.get('ratio','16:9')}
        size=next((s for s,v in sizes.items() if v==selected),None)
        if not size: raise ValueError('Unsupported resolution/ratio; native 480p/720p/1080p only where listed.')
    if data.get('mode','omni')!='omni': raise ValueError('This reference endpoint currently uses omni mode.')
    checked=validate_request({'model':model['id'],'prompt':data.get('prompt'),'duration':seconds,**selected,'mode':'omni'})
    return {'model':model['id'],'prompt':checked['prompt'],'seconds':str(seconds),'duration':seconds,'size':size,**selected,'mode':'omni','new_session':True}


class _MappedImage:
    def __init__(self,value):self.value=value
    def startswith(self,prefix):return self.value[:len(prefix)]==prefix
    def __len__(self):return len(self.value)
    def __getitem__(self,index):return self.value[index]


def _metadata(kind,raw,size):
    if kind=='image':
        if size>30*1024*1024:raise ValueError('Image reference exceeds 30 MiB.')
        w,h,fmt=image_dimensions(raw)
        if fmt not in ('png','jpeg','jpg','webp'):raise ValueError('Gateway currently accepts PNG, JPEG and WebP images.')
        fmt='jpeg' if fmt=='jpg' else fmt
        return {'width':w,'height':h,'format':fmt},'image/'+fmt,'jpg' if fmt=='jpeg' else fmt
    if size<12 or raw[4:8]!=b'ftyp':raise ValueError('Reference video must be an MP4/MOV container.')
    media=mp4_metadata(raw);media['duration']=media.pop('duration_ms');media['format']='mp4'
    return media,'video/mp4','mp4'


def file_reference_metadata(path,kind):
    """Inspect mapped headers without loading the full media into Python memory."""
    size=Path(path).stat().st_size
    if not 0<size<=MAX_REFERENCE_BYTES:raise ValueError('Invalid reference type or size.')
    error=None;result=None
    with Path(path).open('rb') as source:
        with mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ) as mapped:
            try:result=_metadata(kind,_MappedImage(mapped) if kind=='image' else mapped,size)
            except (ValueError,TypeError,IndexError,KeyError) as exc:
                # Clear exported-memoryview traceback frames before closing mmap.
                error=str(exc);exc.__traceback__=None
    if error is not None:raise ValueError(error)
    return result


def _digest_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(64*1024),b''):digest.update(block)
    return digest.hexdigest()


def parse_references(data, files, *, stage=None):
    refs=data.get('references',[])
    if isinstance(refs,str):
        try: refs=json.loads(refs)
        except ValueError as exc: raise ValueError('references must be a JSON array.') from exc
    if not isinstance(refs,list): raise ValueError('references must be an array.')
    refs=list(refs)
    if data.get('input_reference') is not None:
        reference=data['input_reference']
        if isinstance(reference,str): reference={'data_url':reference}
        if not isinstance(reference,dict): raise ValueError('input_reference must be a data URL or image object.')
        refs.insert(0,{'type':'image','data_url':reference.get('data_url',reference.get('image_url')),'name':reference.get('name')})
    buffers=[]
    for ref in refs:
        if not isinstance(ref,dict) or set(ref)-{'type','kind','data_url','name'}: raise ValueError('references accepts only type, data_url and name; server paths and remote URLs are not accepted.')
        kind=ref.get('type',ref.get('kind'))
        value=ref.get('data_url','')
        if not isinstance(value,str) or len(value)>MAX_REFERENCE_BYTES*4//3+1024: raise ValueError('Invalid or oversized reference data URL.')
        match=re.fullmatch(r'data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)',value)
        if not match: raise ValueError('Use a base64 data URL or multipart upload; remote URLs are not fetched.')
        if stage is None:
            try: raw=base64.b64decode(match[2],validate=True)
            except ValueError as exc: raise ValueError('Invalid reference base64.') from exc
            buffers.append((kind,ref.get('name'),match[1],{'bytes':raw,'size_bytes':len(raw)}))
        else:
            path,out=stage.new_file();digest=hashlib.sha256();size=0;encoded=match[2]
            try:
                padding=encoded.find('=')
                if '\r' in encoded or '\n' in encoded or len(encoded)%4 or padding>=0 and encoded[padding:] not in ('=','=='):
                    raise ValueError('Invalid base64 padding.')
                for start in range(0,len(encoded),65536):
                    chunk=base64.b64decode(encoded[start:start+65536],validate=True);size+=len(chunk)
                    if size>MAX_REFERENCE_BYTES:raise ValueError('Reference data URL exceeds the upload limit.')
                    out.write(chunk);digest.update(chunk)
            except ValueError as exc:raise ValueError('Invalid or oversized reference base64.') from exc
            finally:out.close()
            buffers.append((kind,ref.get('name'),match[1],{'path':path,'size_bytes':size,'sha256':digest.hexdigest()}))
    for upload in files:
        field=upload['field'].removesuffix('[]')
        kind='image' if field in ('reference_images','input_reference') else 'video' if field=='reference_videos' else None
        if not kind: raise ValueError('File field must be reference_images, reference_videos or input_reference.')
        if 'path' in upload:
            if stage is None or not stage.owns(upload['path']):raise ValueError('Upload path does not belong to this request.')
            body={k:upload[k] for k in ('path','size_bytes','sha256')}
        else:body={'bytes':upload['bytes'],'size_bytes':len(upload['bytes'])}
        buffers.append((kind,upload['filename'],upload['content_type'],body))
    if len(buffers)>50 or sum(x[3]['size_bytes'] for x in buffers)>MAX_TOTAL_BYTES: raise ValueError('Reference count/total size exceeds gateway limits.')
    prepared=[]
    for index,(kind,name,mime,body) in enumerate(buffers):
        size=body['size_bytes']
        if kind not in ('image','video') or not 0<size<=MAX_REFERENCE_BYTES: raise ValueError('Invalid reference type or size.')
        if 'path' in body:
            if Path(body['path']).stat().st_size!=size:raise ValueError('Reference upload changed while reading.')
            media,mime,ext=file_reference_metadata(body['path'],kind);digest=_digest_file(body['path'])
            if digest!=body['sha256']:raise ValueError('Reference upload integrity mismatch.')
        else:media,mime,ext=_metadata(kind,body['bytes'],size);digest=hashlib.sha256(body['bytes']).hexdigest()
        safe_name=f'reference-{index+1}.{ext}'  # User filenames never become paths or runtime instructions.
        prepared.append({'metadata':{'asset_id':f'asset_{index}_{digest[:24]}','kind':kind,'name':safe_name,'mime_type':mime,
                                    'size_bytes':size,'sha256':digest,**media},**body})
    return prepared


def validate_references(spec, prepared):
    # Validate before task reservation: invalid @ references must not consume a
    # client request allowance or dispatch a paid browser operation.
    parse_prompt_mentions(spec['prompt'], [item['metadata'] for item in prepared])
    resources=[]
    for item in prepared:
        m=item['metadata'];r={'type':m['kind'],'width':m['width'],'height':m['height'],'format':m['format'],'file_size_bytes':m['size_bytes']}
        if m['kind']=='image':r['image_uri']='pending-'+m['asset_id']
        else:r.update(vid='pending-'+m['asset_id'],duration=m['duration'],fps=m['fps'])
        resources.append(r)
    validate_request({k:spec[k] for k in ('model','prompt','duration','ratio','resolution','mode')}|{'references':resources})


def asset_path_for_task(directory, task_id, asset):
    if not re.fullmatch(r'(video|img)_[a-f0-9]{32}',task_id):raise ValueError('Invalid asset task identity.')
    if not re.fullmatch(r'asset_\d{1,2}_[a-f0-9]{24}',asset['asset_id']):raise ValueError('Invalid asset ID.')
    return Path(directory)/task_id/'references'/(asset['asset_id']+'.bin')


def save_references(directory, task_id, prepared):
    for item in prepared:
        path=asset_path_for_task(directory,task_id,item['metadata']);path.parent.mkdir(parents=True,exist_ok=True)
        created=False
        try:
            with path.open('xb') as file:
                created=True;digest=hashlib.sha256();size=0
                if 'path' in item:
                    with Path(item['path']).open('rb') as source:
                        for block in iter(lambda:source.read(64*1024),b''):file.write(block);digest.update(block);size+=len(block)
                else:file.write(item['bytes']);digest.update(item['bytes']);size=len(item['bytes'])
                if size!=item['metadata']['size_bytes'] or digest.hexdigest()!=item['metadata']['sha256']:raise ValueError('Reference changed before private persistence.')
        except BaseException:
            if created:path.unlink(missing_ok=True)
            raise
