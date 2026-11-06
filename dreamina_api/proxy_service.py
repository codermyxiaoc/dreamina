"""Frozen, verified account egress changes, resumable after interruptions."""
import asyncio
import weakref
import re
from .db_async import db_call
from .platform_store import PlatformError
from .egress_clients import account_client_pool

class ProxyBindingService:
    def __init__(self,store,workers,health,account_check):
        self.store=store;self.workers=workers;self.health=health;self.account_check=account_check
        self._locks=weakref.WeakValueDictionary();self._operations={};self._probes=set();self._retries={};self._closed=False

    def probe_later(self,proxy_id):
        async def check():
            try:await self.health.test(proxy_id)
            except Exception:pass
        task=asyncio.create_task(check());self._probes.add(task);task.add_done_callback(self._probes.discard)

    async def bind(self,account_id,proxy_id,*,confirm_country_change=False,confirm_capacity_override=False):
        if proxy_id is not None and (not isinstance(proxy_id,str) or not re.fullmatch(r'proxy_[0-9a-f]{32}',proxy_id)):
            raise PlatformError('A valid managed proxy_id is required.',code='invalid_proxy')
        if type(confirm_country_change) is not bool or type(confirm_capacity_override) is not bool:
            raise PlatformError('Proxy confirmations must be booleans.',code='invalid_proxy')
        signature=(proxy_id,confirm_country_change,confirm_capacity_override);previous=self._operations.get(account_id)
        if previous:
            if previous[0]!=signature:raise PlatformError('Another proxy change is in progress.',code='proxy_change_in_progress',status=409)
            return await asyncio.shield(previous[1])
        async def operation():
            lock=self._locks.setdefault(account_id,asyncio.Lock())
            async with lock:
                pending=await db_call(self.store.pending_proxy_change,account_id)
                if pending and pending['proxy_id']!=proxy_id:raise PlatformError('Resume the existing proxy change first.',code='proxy_change_in_progress',status=409)
                if pending and pending['state']=='restarting':
                    await self._restore_worker(pending)
                    return {'account':await db_call(self.store.get_account,account_id),'proxy_changed':True}
                if pending and pending['state']=='checking' and (pending.get('retry_at') or 0)>self.store.clock():
                    self._schedule_retry(pending)
                    return await self._pending_result(pending)
                if not pending:
                    account=await db_call(self.store.get_account,account_id)
                    if account.get('active_tasks',0) or account.get('blocking_tasks',0) or account.get('status') in ('login_pending','proxy_changing','deleting','deleted'):
                        raise PlatformError('Finish or reconcile this account work before changing its proxy.',code='account_busy',status=409)
                if proxy_id is not None and (not pending or pending['state']=='pending'):await self.health.test(proxy_id)
                if not pending:
                    status=await db_call(self.workers.status,account_id)
                    pending=await db_call(self.store.begin_proxy_change,account_id,proxy_id,
                        confirm_country_change=confirm_country_change,confirm_capacity_override=confirm_capacity_override,
                        previous_running=status.get('state') in ('running','starting'))
                cid=pending['id']
                if pending.get('state','pending')=='pending':
                    await self.workers.stop(account_id)
                    await db_call(self.store.commit_proxy_change,cid)
                await account_client_pool().invalidate(account_id)
                account=await db_call(self.store.get_account,account_id)
                if not account.get('secret_configured') and not account.get('browser_profile_saved'):
                    await db_call(self.store.finish_proxy_change_for_login,cid)
                    return {'account':await db_call(self.store.get_account,account_id),'proxy_changed':True,'login_required':True}
                try:
                    if pending.get('state')=='checking' and proxy_id is not None:await self.health.test(proxy_id)
                    settings=await db_call(self.store.settings_for_account,account_id)
                    checked=await asyncio.wait_for(self.account_check(account_id,settings),30)
                    if checked.get('status') in ('login_required','invalid_session','unauthorized'):
                        raise PlatformError('Account authentication was rejected.',code='invalid_session',status=401)
                    if checked.get('status')!='ready' or checked.get('balance') is None:raise ValueError('Session not verified')
                    await db_call(self.store.record_account_check,account_id,status='ready',balance=checked['balance'])
                    await db_call(self.store.finish_proxy_change,cid,success=True)
                except Exception as error:
                    if self._authentication_rejected(error):
                        await db_call(self.store.finish_proxy_change,cid,success=False,error_code='account_proxy_check_failed')
                        raise PlatformError('Proxy was bound, but account authentication was rejected. Sign in through its new proxy.',code='account_proxy_check_failed',status=409) from None
                    pending=await db_call(self.store.defer_proxy_verification,cid)
                    self._schedule_retry(pending)
                    return await self._pending_result(pending)
                change=await db_call(self.store.get_proxy_change,cid)
                if change['state']=='restarting':await self._restore_worker(change)
                return {'account':await db_call(self.store.get_account,account_id),'proxy_changed':True}
        task=asyncio.create_task(operation());self._operations[account_id]=(signature,task)
        def done(_):
            if self._operations.get(account_id,(None,None))[1] is task:self._operations.pop(account_id,None)
            if not task.cancelled():task.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)

    @staticmethod
    def _authentication_rejected(error):
        # Proxy authentication, generic 403/risk controls and unknown failures
        # are not evidence that the Dreamina session has expired.
        return getattr(error,'code',None) in ('missing_session','invalid_session','unauthorized','upstream_401')

    async def _pending_result(self,change):
        return {'account_id':change['account_id'],'proxy_id':change['proxy_id'],'account':await db_call(self.store.get_account,change['account_id']),
            'proxy_changed':False,'pending':True,'state':change['state'],'retry_at':change.get('retry_at'),
            'retry_count':change.get('retry_count',0),'error_code':'account_proxy_check_retry'}

    def _schedule_retry(self,change):
        cid=change['id']
        if self._closed or cid in self._retries:return
        async def retry():
            while not self._closed:
                try:
                    current=await db_call(self.store.get_proxy_change,cid)
                    if current['state']!='checking':return
                    await asyncio.sleep(max(.05,min(300,(current.get('retry_at') or self.store.clock())-self.store.clock())))
                    await self.bind(current['account_id'],current['proxy_id'],confirm_country_change=current['confirm_country'],confirm_capacity_override=current['allow_capacity'])
                except asyncio.CancelledError:raise
                except Exception:
                    # Database/supervisor failures also retain durable state;
                    # do not spin or rewrite the chosen egress.
                    await asyncio.sleep(5)
        task=asyncio.create_task(retry());self._retries[cid]=task
        def done(_):
            if self._retries.get(cid) is task:self._retries.pop(cid,None)
            if not task.cancelled():task.exception()
        task.add_done_callback(done)

    async def _restore_worker(self,change):
        account=await db_call(self.store.get_account,change['account_id'])
        if not account['enabled']:
            await db_call(self.store.finish_proxy_restart,change['id']);return
        result=await self.workers.start(change['account_id'])
        # Capacity-wait means the supervisor durably recorded desired_running;
        # every other non-start result leaves this change resumable.
        if not isinstance(result,dict) or result.get('state') not in ('running','starting','saved_capacity_wait'):
            raise PlatformError('Verified proxy binding is saved, but worker restart is still pending.',code='proxy_restart_pending',status=409)
        await db_call(self.store.finish_proxy_restart,change['id'])

    async def recover(self):
        for change in await db_call(self.store.list_proxy_changes):
            try:await self.bind(change['account_id'],change['proxy_id'],confirm_country_change=change.get('confirm_country',False),confirm_capacity_override=change.get('allow_capacity',False))
            except Exception:pass

    async def close(self):
        self._closed=True
        tasks=[t for _,t in self._operations.values()]+list(self._probes)+list(self._retries.values())
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
