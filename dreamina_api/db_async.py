"""Offload synchronous storage without sharing an in-memory SQLite connection.

SQLite :memory: uses SQLAlchemy StaticPool: every thread receives the same DBAPI
connection. Serialize its reads as well as writes with the store's reentrant
transaction lock. Normal file SQLite/PostgreSQL pools keep their concurrency.
"""
import asyncio
from sqlalchemy.pool import StaticPool


def _run(callback,args,kwargs,store):
    owner=store if store is not None else getattr(callback,'__self__',None)
    if owner is not None and not hasattr(owner,'engine'):owner=getattr(owner,'store',None)
    engine=getattr(owner,'engine',None)
    if isinstance(getattr(engine,'pool',None),StaticPool):
        with owner._lock:return callback(*args,**kwargs)
    return callback(*args,**kwargs)


async def db_call(callback,*args,_store=None,**kwargs):
    return await asyncio.to_thread(_run,callback,args,kwargs,_store)
