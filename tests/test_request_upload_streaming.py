"""Streamed ingress boundaries and cleanup; no external service or generation."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import struct
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from dreamina_api import request_uploads as uploads
from dreamina_api.openai_api import create_openai_router
from dreamina_api.platform_store import PlatformStore
from dreamina_api.video_inputs import parse_references,save_references,file_reference_metadata

BOUNDARY=b'synthetic-boundary-123'
PNG=b'\x89PNG\r\n\x1a\n'+b'\0\0\0\rIHDR'+struct.pack('>II',512,512)+b'\x08\x02\0\0\0'

def multipart(parts,*,closed=True):
    out=[]
    for name,payload,filename in parts:
        header=b'Content-Disposition: form-data; name="'+name.encode()+b'"'
        if filename is not None:header+=b'; filename="'+filename.encode()+b'"\r\nContent-Type: image/png'
        out += [b'--'+BOUNDARY+b'\r\n'+header+b'\r\n\r\n'+payload+b'\r\n']
    if closed:out.append(b'--'+BOUNDARY+b'--\r\n')
    return b''.join(out)

class Request:
    def __init__(self,raw,*,chunk=16384,ctype=None,declared=None,on_chunk=None):
        self.raw=raw;self.chunk=chunk;self.state=SimpleNamespace();self.on_chunk=on_chunk
        self.headers={'content-type':ctype or 'multipart/form-data; boundary='+BOUNDARY.decode()}
        if declared is not None:self.headers['content-length']=str(declared)
    async def body(self):raise AssertionError('Multipart ingress must never request the full body')
    async def stream(self):
        for i in range(0,len(self.raw),self.chunk):
            if self.on_chunk:await self.on_chunk(i)
            yield self.raw[i:i+self.chunk]
            await asyncio.sleep(0)

@pytest.fixture(autouse=True)
def staging(tmp_path,monkeypatch):
    path=tmp_path/'staging';monkeypatch.setattr(uploads,'UPLOAD_ROOT',path)
    return path

def test_file_is_streamed_before_body_finishes_and_persisted_without_read_bytes(staging,tmp_path,monkeypatch):
    async def run():
        raw=PNG+b'x'*(2*1024*1024);wire=multipart([('prompt',b'Line1\r\n\r\nLine2',None),('reference_images',raw,'../../outside.png')])
        observations=[]
        async def observe(position):
            if position>65536:
                stage=req.state.upload_stage
                observations.append(any(p.stat().st_size>0 for p in stage.paths))
        req=Request(wire,on_chunk=observe)
        data,files=await uploads.parse_request(req,allow_multipart=True)
        assert observations and any(observations[:len(observations)//2]),'File data must reach disk well before the body is complete'
        assert data['prompt']=='Line1\r\n\r\nLine2' and len(files)==1
        source=files[0];assert 'bytes' not in source and source['size_bytes']==len(raw)
        assert source['sha256']==hashlib.sha256(raw).hexdigest() and source['path'].parent==req.state.upload_stage.path
        monkeypatch.setattr(Path,'read_bytes',lambda *_:(_ for _ in ()).throw(AssertionError('Whole-file buffering is forbidden')))
        prepared=await uploads.run_file_io(parse_references,data,files,stage=req.state.upload_stage)
        assert prepared[0]['metadata']['width']==512 and 'bytes' not in prepared[0]
        await uploads.run_file_io(save_references,tmp_path/'media','video_'+'a'*32,prepared)
        path=tmp_path/'media'/('video_'+'a'*32)/'references'/(prepared[0]['metadata']['asset_id']+'.bin')
        with path.open('rb') as f:assert f.read()==raw
        await uploads.cleanup_uploads(req)
        assert not list(staging.iterdir())
    asyncio.run(run())

@pytest.mark.parametrize('chunk',[1,3,17,65536])
def test_arbitrary_chunk_boundaries_preserve_file_and_field_bytes(chunk):
    async def run():
        req=Request(multipart([('prompt','多行\r\n内容'.encode(),None),('reference_images',PNG,'图.png')]),chunk=chunk)
        fields,files=await uploads.parse_request(req,allow_multipart=True)
        assert fields['prompt']=='多行\r\n内容';assert files[0]['path'].read_bytes()==PNG
        await uploads.cleanup_uploads(req)
    asyncio.run(run())

@pytest.mark.parametrize('case',['missing_end','duplicate','file_limit','field_limit','parts','body_limit','declared_limit','header_limit','nested','invalid_utf8'])
def test_bad_inputs_fail_bounded_and_remove_every_staging_file(case,staging):
    async def run():
        parts=[('reference_images',PNG,'image.png')];options={}
        if case=='duplicate':parts += [('prompt',b'a',None),('prompt',b'b',None)]
        elif case=='file_limit':options['max_file_bytes']=10
        elif case=='field_limit':parts += [('prompt',b'a'*200001,None)]
        elif case=='parts':parts += [(f'field{i}',b'x',None) for i in range(70)]
        elif case=='invalid_utf8':parts += [('prompt',b'\xff',None)]
        wire=multipart(parts,closed=case!='missing_end')
        if case=='header_limit':wire=wire.replace(b'Content-Type: image/png',b'X-Long: '+b'a'*9000+b'\r\nContent-Type: image/png')
        elif case=='nested':wire=wire.replace(b'Content-Type: image/png',b'Content-Type: multipart/mixed')
        elif case=='body_limit':options['max_body_bytes']=100
        req=Request(wire,declared=1000 if case=='declared_limit' else None)
        if case=='declared_limit':options['max_body_bytes']=100
        with pytest.raises(uploads.UploadInputError):await uploads.parse_request(req,allow_multipart=True,**options)
        assert not staging.exists() or not list(staging.iterdir())
    asyncio.run(run())

def test_four_ingress_requests_hold_capacity_until_persisted_or_cleaned():
    async def run():
        entered=[];release=asyncio.Event()
        class Slow(Request):
            async def stream(self):
                entered.append(self);await release.wait();yield self.raw
        requests=[Slow(b'{}',ctype='application/json') for _ in range(5)]
        jobs=[asyncio.create_task(uploads.parse_request(r)) for r in requests]
        for _ in range(50):
            if len(entered)==4:break
            await asyncio.sleep(.005)
        assert len(entered)==4
        release.set();await asyncio.gather(*jobs[:4]);await asyncio.sleep(.01)
        assert len(entered)==4 and not jobs[4].done(), 'The fifth body is not consumed before earlier input persistence'
        uploads.finish_upload_input(requests[0]);await jobs[4];assert len(entered)==5
        for r in requests:await uploads.cleanup_uploads(r)
    asyncio.run(run())

def test_cancel_waits_for_threaded_writer_then_cleans_and_releases_capacity(staging,monkeypatch):
    entered=threading.Event();release=threading.Event();original=uploads.MultipartSink.part_data
    def slow(self,*args):
        original(self,*args)
        if self.handle is not None and not entered.is_set():entered.set();release.wait(5)
    monkeypatch.setattr(uploads.MultipartSink,'part_data',slow)
    async def run():
        req=Request(multipart([('reference_images',PNG+b'x'*100000,'image.png')]))
        operation=asyncio.create_task(uploads.parse_request(req,allow_multipart=True))
        try:
            assert await asyncio.to_thread(entered.wait,3)
            operation.cancel();await asyncio.sleep(.02);operation.cancel();await asyncio.sleep(.02)
            assert not operation.done() and any(staging.iterdir())
            release.set()
            with pytest.raises(asyncio.CancelledError):await operation
            assert not list(staging.iterdir())
            assert req.state.upload_stage.parser_slot is None
        finally:
            release.set()
            if not operation.done():operation.cancel();await asyncio.gather(operation,return_exceptions=True)
    asyncio.run(run())

def test_json_reference_decodes_to_private_file_in_chunks_and_rejects_embedded_padding(tmp_path):
    async def run():
        raw=PNG+b'x'*200000;data={'references':[{'type':'image','data_url':'data:image/png;base64,'+base64.b64encode(raw).decode()}]}
        req=Request(json.dumps(data).encode(),ctype='application/json')
        body,files=await uploads.parse_request(req)
        refs=await uploads.run_file_io(parse_references,body,files,stage=req.state.upload_stage)
        assert 'path' in refs[0] and 'bytes' not in refs[0] and refs[0]['metadata']['size_bytes']==len(raw)
        assert refs[0]['path'].read_bytes()==raw
        bad={'references':[{'type':'image','data_url':'data:image/png;base64,'+('YQ=='+'A'*65532)+'Yg=='}]}
        with pytest.raises(ValueError):await uploads.run_file_io(parse_references,bad,[],stage=req.state.upload_stage)
        await uploads.cleanup_uploads(req)
    asyncio.run(run())

def test_invalid_mapped_video_releases_memoryviews_without_buffer_error(tmp_path):
    path=tmp_path/'bad.mp4';path.write_bytes(b'\0\0\0\x14ftypisom\0\0\0\0isom')
    with pytest.raises(ValueError,match='moov'):file_reference_metadata(path,'video')
    path.unlink();assert not path.exists()

def test_uploaded_file_path_must_belong_to_request(tmp_path):
    path=tmp_path/'foreign.png';path.write_bytes(PNG);stage=uploads.UploadStage(tmp_path/'private')
    with pytest.raises(ValueError,match='belong'):
        parse_references({},[{'field':'reference_images','filename':'x','content_type':'image/png','path':path,'size_bytes':len(PNG),'sha256':hashlib.sha256(PNG).hexdigest()}],stage=stage)

def test_compatibility_route_cleans_staging_after_success_and_parameter_rejection(tmp_path,staging):
    store=PlatformStore('sqlite:///'+str(tmp_path/'db.sqlite'),encryption_key=Fernet.generate_key())
    store.create_account({'id':'one','name':'one','session_id':'synthetic','capabilities':['seedance-2.0-mini']})
    key=store.create_key({'name':'synthetic','request_limit':1})
    class Bridge:
        def enqueue(self,request,*,account_id,job_id=None):return {'id':job_id,'status':'queued'}
    app=FastAPI();media=tmp_path/'media';app.include_router(create_openai_router(store,Bridge(),media_directory=media))
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+key['key']}
        r=client.post('/v1/videos',headers=headers,data={'prompt':'@图片1 turns','seconds':'4'},files=[('reference_images',('ref.png',PNG,'image/png'))]);assert r.status_code==200,r.text
        assert not list(staging.iterdir()) and list(media.rglob('*.bin'))
        r=client.post('/v1/videos',headers=headers,data={'prompt':'bad','unknown':'field'},files=[('reference_images',('ref.png',PNG,'image/png'))]);assert r.status_code==400
        assert not list(staging.iterdir())
