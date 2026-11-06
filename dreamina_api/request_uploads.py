"""Bounded streamed multipart ingress and request-owned private staging files."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import weakref

from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from .config import ROOT

UPLOAD_ROOT=ROOT/'outputs'/'request-uploads'
CHUNK_BYTES=64*1024
_LIMITERS=weakref.WeakKeyDictionary()


class UploadInputError(Exception):
    def __init__(self,message,*,status=400,code='invalid_request',param=None):
        super().__init__(message);self.status=status;self.code=code;self.param=param


async def run_file_io(function,*args,**kwargs):
    """Do not remove a file while a cancelled to_thread writer is still using it."""
    work=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:await asyncio.shield(work)
            except asyncio.CancelledError:continue
            except Exception:break
        try:work.result()
        except BaseException:pass
        raise


@asynccontextmanager
async def parsing_slot():
    semaphore=_limiter()
    async with semaphore:yield


def _limiter():
    loop=asyncio.get_running_loop();semaphore=_LIMITERS.get(loop)
    if semaphore is None:semaphore=asyncio.Semaphore(4);_LIMITERS[loop]=semaphore
    return semaphore


class UploadStage:
    """Only generated filenames are ever written, independent of user filenames."""
    def __init__(self,root=None):
        self.root=Path(root or UPLOAD_ROOT);self.path=self.root/('request-'+secrets.token_hex(16))
        self.paths=[];self.handles=[];self.created=False;self.parser_slot=None

    def new_file(self):
        if not self.created:
            self.root.mkdir(parents=True,exist_ok=True)
            if self.root.is_symlink():raise ValueError('Upload staging directory must not be a symlink.')
            self.path.mkdir(mode=0o700);self.created=True
        path=self.path/(secrets.token_hex(16)+'.part')
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
        handle=os.fdopen(descriptor,'wb');self.paths.append(path);self.handles.append(handle)
        return path,handle

    def owns(self,path):
        value=Path(path)
        return value in self.paths and value.parent==self.path and not value.is_symlink()

    def cleanup(self):
        for handle in self.handles:
            if not handle.closed:handle.close()
        for path in self.paths:
            if path.parent!=self.path:continue
            path.unlink(missing_ok=True)
        if self.created:
            try:self.path.rmdir()
            except FileNotFoundError:pass


def stage_for(request):
    stage=getattr(request.state,'upload_stage',None)
    if stage is None:stage=UploadStage();request.state.upload_stage=stage
    return stage


async def cleanup_uploads(request):
    stage=getattr(request.state,'upload_stage',None)
    if stage is not None:
        try:await run_file_io(stage.cleanup)
        finally:finish_upload_input(request)


def finish_upload_input(request):
    """Release ingress capacity after media persistence, before generation wait."""
    stage=getattr(request.state,'upload_stage',None)
    if stage is not None and stage.parser_slot is not None:
        stage.parser_slot.release();stage.parser_slot=None


def persist_upload(upload,destination):
    """Copy an admitted private upload without buffering the full input again."""
    path=Path(destination);path.parent.mkdir(parents=True,exist_ok=True);created=False
    try:
        with path.open('xb') as out:
            created=True;digest=hashlib.sha256();size=0
            if 'path' in upload:
                with Path(upload['path']).open('rb') as source:
                    for block in iter(lambda:source.read(CHUNK_BYTES),b''):out.write(block);digest.update(block);size+=len(block)
            else:
                raw=upload['bytes'];out.write(raw);digest.update(raw);size=len(raw)
            if upload.get('size_bytes',size)!=size or upload.get('sha256',digest.hexdigest())!=digest.hexdigest():raise ValueError('Upload integrity changed before persistence.')
    except BaseException:
        if created:path.unlink(missing_ok=True)
        raise


def check_length(request,maximum):
    raw=request.headers.get('content-length')
    if raw is not None:
        try:value=int(raw)
        except ValueError:raise UploadInputError('Invalid Content-Length.') from None
        if value<0 or value>maximum:raise UploadInputError('Request body exceeds the gateway limit.',status=413,code='request_too_large')


async def read_json(request,maximum):
    check_length(request,maximum);body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>maximum:raise UploadInputError('Request body exceeds the gateway limit.',status=413,code='request_too_large')
        body.extend(chunk)
    try:data=await run_file_io(json.loads,body)
    except (ValueError,UnicodeError):raise UploadInputError('Body must be valid JSON.',code='invalid_json') from None
    if not isinstance(data,dict):raise UploadInputError('The request body must be a JSON object.')
    return data


class MultipartSink:
    def __init__(self,stage,*,max_file_bytes):
        self.stage=stage;self.max_file_bytes=max_file_bytes;self.fields={};self.files=[]
        self.parts=0;self.ended=False;self.header_name=bytearray();self.header_value=bytearray();self.header_bytes=0
        self.headers={};self.handle=None;self.file_path=None;self.field=None;self.filename=None;self.size=0;self.buffer=bytearray();self.digest=None

    def part_begin(self):
        self.parts+=1
        if self.parts>70:raise UploadInputError('Invalid or excessive multipart parts.')
        self.headers={};self.header_name=bytearray();self.header_value=bytearray();self.header_bytes=0
        self.handle=None;self.file_path=None;self.field=None;self.filename=None;self.size=0;self.buffer=bytearray();self.digest=None

    def _header(self,target,data,start,end):
        self.header_bytes+=end-start
        if self.header_bytes>8192:raise UploadInputError('Multipart headers are too large.')
        target.extend(data[start:end])

    def header_field(self,data,start,end):self._header(self.header_name,data,start,end)
    def header_value_data(self,data,start,end):self._header(self.header_value,data,start,end)

    def header_end(self):
        key=bytes(self.header_name).lower();value=bytes(self.header_value)
        if not key or key in self.headers or len(self.headers)>=16:raise UploadInputError('Invalid duplicate or excessive multipart headers.')
        self.headers[key]=value;self.header_name=bytearray();self.header_value=bytearray()

    def headers_finished(self):
        disposition,options=parse_options_header(self.headers.get(b'content-disposition'))
        name=options.get(b'name')
        if disposition!=b'form-data' or not name:raise UploadInputError('Every multipart part needs a form field name.')
        try:self.field=name.decode('utf-8')
        except UnicodeError:raise UploadInputError('Multipart field names must be UTF-8.') from None
        if not re.fullmatch(r'[A-Za-z0-9_\[\]-]{1,80}',self.field):raise UploadInputError('Invalid multipart field name.')
        content_type=self.headers.get(b'content-type',b'application/octet-stream').decode('latin-1').split(';',1)[0].strip().lower()
        if content_type.startswith('multipart/') or self.headers.get(b'content-transfer-encoding') not in (None,b'binary',b'8bit'):
            raise UploadInputError('Nested or encoded multipart parts are unsupported.')
        self.content_type=content_type
        if b'filename' in options:
            if len(self.files)>=50:raise UploadInputError('At most 50 file parts are accepted.',param=self.field)
            raw=options[b'filename']
            if len(raw)>4096:raise UploadInputError('Multipart filename is too long.')
            try:self.filename=raw.decode('utf-8')
            except UnicodeError:self.filename=raw.decode('latin-1')
            self.file_path,self.handle=self.stage.new_file();self.digest=hashlib.sha256()
        elif self.field in self.fields:raise UploadInputError('Duplicate field: '+self.field+'.',param=self.field)

    def part_data(self,data,start,end):
        if self.field is None:raise UploadInputError('Multipart data has no field headers.')
        self.size+=end-start
        if self.handle is not None:
            if self.size>self.max_file_bytes:raise UploadInputError('Input asset exceeds 50 MiB.',status=413,code='file_too_large',param=self.field)
            chunk=memoryview(data)[start:end];self.handle.write(chunk);self.digest.update(chunk)
        else:
            if self.size>200_000:raise UploadInputError('A form field is too large.',status=413,param=self.field)
            self.buffer.extend(data[start:end])

    def part_end(self):
        if self.field is None:raise UploadInputError('Multipart part has no field headers.')
        if self.handle is not None:
            self.handle.close()
            self.files.append({'field':self.field,'filename':self.filename,'content_type':self.content_type,
                'path':self.file_path,'size_bytes':self.size,'sha256':self.digest.hexdigest()})
        else:
            try:self.fields[self.field]=self.buffer.decode('utf-8')
            except UnicodeError:raise UploadInputError('Text form fields must use UTF-8.',param=self.field) from None

    def end(self):self.ended=True

    def callbacks(self):
        return {'on_part_begin':self.part_begin,'on_header_field':self.header_field,'on_header_value':self.header_value_data,
            'on_header_end':self.header_end,'on_headers_finished':self.headers_finished,'on_part_data':self.part_data,
            'on_part_end':self.part_end,'on_end':self.end}


async def parse_request(request,*,allow_multipart=False,max_body_bytes=96*1024*1024,max_file_bytes=50*1024*1024):
    content_type=request.headers.get('content-type','').strip()
    try:
        stage=stage_for(request)
        if stage.parser_slot is None:
            semaphore=_limiter();await semaphore.acquire();stage.parser_slot=semaphore
        check_length(request,max_body_bytes)
        if content_type.split(';',1)[0].lower()=='application/json':return await read_json(request,max_body_bytes),[]
        if not allow_multipart or not content_type.lower().startswith('multipart/form-data;'):
            raise UploadInputError('Use application/json'+(' or multipart/form-data.' if allow_multipart else '.'),status=415,code='unsupported_media_type')
        if '\r' in content_type or '\n' in content_type or len(content_type)>300:raise UploadInputError('Invalid multipart Content-Type.')
        try:
            _,options=parse_options_header(content_type);boundary=options.get(b'boundary')
            if not boundary or len(boundary)>200 or any(b<32 or b>126 for b in boundary):raise ValueError()
            sink=MultipartSink(stage,max_file_bytes=max_file_bytes);parser=MultipartParser(boundary,sink.callbacks())
        except (ValueError,TypeError):raise UploadInputError('Malformed multipart body or missing boundary.') from None
        size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>max_body_bytes:raise UploadInputError('Request body exceeds the gateway limit.',status=413,code='request_too_large')
            for start in range(0,len(chunk),CHUNK_BYTES):
                try:await run_file_io(parser.write,chunk[start:start+CHUNK_BYTES])
                except UploadInputError:raise
                except (ValueError,TypeError):raise UploadInputError('Malformed multipart body.') from None
        await run_file_io(parser.finalize)
        if not sink.ended:raise UploadInputError('Malformed multipart body or missing closing boundary.')
        return sink.fields,sink.files
    except BaseException:
        await cleanup_uploads(request)
        raise
