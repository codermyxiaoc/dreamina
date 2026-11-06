"""Bounded, loop-local HTTP connection pools isolated by account and egress."""
import asyncio
import hashlib
import time
import weakref
import httpx

_pools=weakref.WeakKeyDictionary()

class AccountClientPool:
    def __init__(self,*,factory=httpx.AsyncClient,maximum=64,idle_seconds=300):
        self.factory=factory;self.maximum=maximum;self.idle_seconds=idle_seconds;self.entries={};self.lock=asyncio.Lock()

    async def acquire(self,settings,egress):
        fingerprint=hashlib.sha256((egress.httpx_proxy or '').encode()).hexdigest()
        auth=hashlib.sha256((settings.session_id+'\0'+settings.cookie).encode()).hexdigest()
        key=(settings.account_id,egress.proxy_id,egress.revision,fingerprint,auth)
        async with self.lock:
            now=time.monotonic()
            for old,entry in list(self.entries.items()):
                if not entry['refs'] and (now-entry['used']>self.idle_seconds or old[0]==settings.account_id and old!=key):
                    self.entries.pop(old);await entry['client'].aclose()
            if key in self.entries:
                entry=self.entries[key];entry['refs']+=1;entry['used']=now;return entry['client'],key
            if len(self.entries)>=self.maximum:
                idle=[(k,e) for k,e in self.entries.items() if not e['refs']]
                if idle:
                    old,entry=min(idle,key=lambda pair:pair[1]['used']);self.entries.pop(old);await entry['client'].aclose()
            client=self.factory(proxy=egress.httpx_proxy,trust_env=False,follow_redirects=False,
                timeout=httpx.Timeout(60,connect=25),limits=httpx.Limits(max_connections=8,max_keepalive_connections=4,keepalive_expiry=60))
            if len(self.entries)>=self.maximum:return client,None
            self.entries[key]={'client':client,'refs':1,'used':now};return client,key

    async def release(self,key,client):
        if key is None:await client.aclose();return
        async with self.lock:
            entry=self.entries.get(key)
            if entry and entry['client'] is client:entry['refs']=max(0,entry['refs']-1);entry['used']=time.monotonic()

    async def invalidate(self,account_id):
        async with self.lock:
            for key,entry in list(self.entries.items()):
                if key[0]==account_id and not entry['refs']:
                    self.entries.pop(key);await entry['client'].aclose()

    async def close(self):
        async with self.lock:
            entries=list(self.entries.values());self.entries.clear()
        await asyncio.gather(*(e['client'].aclose() for e in entries),return_exceptions=True)

def account_client_pool():
    loop=asyncio.get_running_loop()
    if loop not in _pools:_pools[loop]=AccountClientPool()
    return _pools[loop]

async def close_account_client_pool():
    pool=_pools.pop(asyncio.get_running_loop(),None)
    if pool:await pool.close()
