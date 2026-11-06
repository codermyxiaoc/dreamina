import asyncio
from types import SimpleNamespace

import pytest

import dreamina_api.platform_runtime as module
from dreamina_api.client import DreaminaError


class Store:
    def __init__(self):
        self.task={'id':'video_fixture','account_id':'one','submit_id':'original-upstream-id','status':'completed','output_path':None,'result':{}}
        self.events=[]
    def get_task(self,_):return dict(self.task)
    def settings_for_account(self,_,*,existing_task=False):
        assert existing_task is True
        return SimpleNamespace(api_origin='')
    def update_task(self,_,**kw):self.task.update(kw);return dict(self.task)
    def append_event(self,_,stage,detail):self.events.append((stage,detail))


def test_concurrent_downloads_share_one_transfer_and_atomic_destination(tmp_path,monkeypatch):
    async def run():
        store=Store();started=asyncio.Event();release=asyncio.Event();calls=[]
        class Client:
            def __init__(self,*_):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def download(self,sid,path):
                calls.append(sid);started.set();await release.wait()
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic-video');return path
        monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'DreaminaClient',Client)
        runtime=module.PlatformRuntime(store,None)
        tasks=[asyncio.create_task(runtime.download_video(store.get_task('x'))) for _ in range(5)]
        try:await asyncio.wait_for(started.wait(),5)
        finally:release.set()
        paths=await asyncio.gather(*tasks)
        assert calls==['original-upstream-id']
        assert len(set(paths))==1 and paths[0].read_bytes()==b'synthetic-video'
        assert store.task['status']=='completed'
    asyncio.run(run())


def test_download_network_failure_retries_same_task_not_generation(tmp_path,monkeypatch):
    async def run():
        store=Store();calls=[]
        class Client:
            def __init__(self,*_):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def download(self,sid,path):
                calls.append(sid)
                if len(calls)<3:raise DreaminaError('Synthetic CDN expiry',code='download_failed')
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture');return path
        async def fast_sleep(*_):pass
        monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'DreaminaClient',Client);monkeypatch.setattr(module.asyncio,'sleep',fast_sleep)
        path=await module.PlatformRuntime(store,None).download_video(store.get_task('x'))
        assert path.is_file() and calls==['original-upstream-id']*3
        assert len(store.events)==2 and all(e[0]=='video_download_retry' for e in store.events)
        assert store.task['status']=='completed'
    asyncio.run(run())


def test_download_known_not_ready_is_not_blindly_retried(tmp_path,monkeypatch):
    async def run():
        store=Store();calls=[]
        class Client:
            def __init__(self,*_):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def download(self,sid,path):calls.append(sid);raise DreaminaError('Not ready',code='not_ready',status=409)
        monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'DreaminaClient',Client)
        with pytest.raises(DreaminaError):await module.PlatformRuntime(store,None).download_video(store.get_task('x'))
        assert len(calls)==1 and store.task['output_path'] is None and store.task['status']=='completed'
    asyncio.run(run())
