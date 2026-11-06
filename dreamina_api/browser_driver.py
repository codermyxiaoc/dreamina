"""Native Dreamina UI execution through Playwright, without a browser extension.

No request replay, signature patching, fingerprint changes or automated login.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
from functools import wraps
import inspect
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, parse_qs
import uuid

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .client import API_ORIGINS, GENERATE_PATH
from .models import model_config
from .image_models import IMAGE_MODEL, IMAGE_MODEL_NAME, IMAGE_UPSTREAM_MODEL
from .file_picker import choose_reference_files
from .prompt_mentions import has_prompt_mentions,parse_prompt_mentions,verify_prompt_mentions
from .credit_quote import parse_exact_credit_price,quote_request_fingerprint,READ_NATIVE_CREDIT_PRICE
from .upstream_errors import normalize_upstream_error,has_generation_acceptance
from .prompt_text import canonical_prompt_text,prompt_text_equal
from .browser_launch import is_browser_proxy_error

PAGE_URL='https://dreamina.capcut.com/ai-tool/generate'
CONTENT_CONFIRMATION_NOTICE='You confirm you own all necessary rights to uploaded content and will not infringe third-party rights, nor upload sensitive personal data or inappropriate content.'


def recognized_content_notice(text):
    """Allow wording variation only within the narrow upload-rights notice."""
    if not isinstance(text,str) or not 40<=len(text)<=1800:return False
    if re.search(r'captcha|verif(?:y|ication)|sign\s*in|log\s*in|password|upgrade|subscribe|subscription|purchase|buy|payment|checkout|\bpay\b|\$|€|￥|验证码|充值|购买',text,re.I):return False
    rights=re.search(r'\b(?:own|have|hold)\b.{0,65}\b(?:rights|permission|permissions)\b',text,re.I)
    material=re.search(r'\b(?:upload(?:ed|ing)?|input|reference)\b.{0,35}\b(?:content|materials?|media)\b',text,re.I)
    third_party=re.search(r'(?:third[ -]party\s+rights|rights\s+of\s+others|infring(?:e|ement))',text,re.I)
    sensitive=re.search(r'sensitive\s+personal\s+(?:data|information)|inappropriate\s+content',text,re.I)
    assent=re.search(r'\b(?:confirm|acknowledge|agree)\b',text,re.I)
    return bool(rights and material and third_party and sensitive and assent)


class UIUnavailable(RuntimeError):
    pass


class ReferenceCleanupUnavailable(UIUnavailable):
    error_code='reference_cleanup_failed'


class ReferenceCountMismatch(UIUnavailable):
    error_code='reference_count_mismatch'


class BrowserProxyUnavailable(UIUnavailable):
    error_code='proxy_error'


def classify_browser_proxy_errors(method):
    @wraps(method)
    async def wrapped(*args,**kwargs):
        try:return await method(*args,**kwargs)
        except Exception as error:
            if is_browser_proxy_error(error):
                raise BrowserProxyUnavailable('The configured browser proxy could not connect; no direct fallback was used.') from None
            raise
    return wrapped


class CreditQuoteUnavailable(UIUnavailable):
    error_code='credit_quote_unavailable'


def explicit_model_upgrade_notice(text: str, model_name: str) -> bool:
    """Recognize a direct model/upgrade statement, not a nearby price listing.

    A generic subscription panel says nothing about the selected model's rights.
    Keep this deliberately narrow: an unrecognized message remains unresolved.
    """
    if not isinstance(text,str) or not isinstance(model_name,str):return False
    text=re.sub(r'\s+',' ',text).strip()
    if len(text)>600:return False
    name=re.sub(r'^Dreamina\s+','',model_name,flags=re.I)
    model=r'(?<![\w.])(?:Dreamina\s+)?'+re.escape(name).replace(r'\ ',r'\s+')+r'(?!\w|\.[0-9]|\s+(?:Mini|Fast|Lite|Pro|Turbo)\b)'
    plan=r'(?:Standard|Advanced|Ultra|paid|premium)\s+(?:plan|subscription|membership)'
    patterns=(
        # Observed site notice; anchor the entire sentence so negation or a
        # different feature listed elsewhere cannot turn into permission proof.
        r'^Upgrade\s+to\s+use\s+this\s+feature:\s*'+model+r'[.!]?$',
        model+r'\s+requires?\s+(?:an?\s+)?(?:plan\s+upgrade|subscription\s+upgrade|'+plan+r')\b',
        r'\bupgrade\s+(?:your\s+plan|your\s+subscription|to\s+(?:the\s+)?(?:Standard|Advanced|Ultra)(?:\s+plan)?)\s+to\s+(?:use|access|generate\s+with)\s+'+model,
        model+r'\s+is\s+only\s+available\s+(?:on|with|to)\s+(?:an?\s+|the\s+)?'+plan+r'\b',
    )
    return any(re.search(pattern,text,re.I) for pattern in patterns)


def generation_request(url: str, method: str, raw: str | None, spec: dict, bindings: dict | None = None) -> dict | None:
    try:
        if not isinstance(url,str) or not isinstance(raw,str) or not isinstance(spec,dict):return None
        parsed=urlsplit(url)
        if method!='POST' or f'{parsed.scheme}://{parsed.netloc}' not in API_ORIGINS or parsed.path!=GENERATE_PATH or not raw or len(raw)>2_000_000:return None
        body=json.loads(raw);submit_id=str(uuid.UUID(body['submit_id']))
        draft=body['draft_content'];draft=json.loads(draft) if isinstance(draft,str) else draft
        component=draft['component_list'][0]
        if spec.get('kind')=='image':
            core=component['abilities']['generate']['core_param']
            if core['model']!=IMAGE_UPSTREAM_MODEL or not prompt_text_equal(core['prompt'],spec['prompt']):return None
            if core['large_image_info']['width']!=2048 or core['large_image_info']['height']!=2048 or component['abilities']['gen_option']['gen_count']!=1:return None
            refs=[]
        else:
            params=component['abilities']['gen_video']['text_to_video_params'];item=params['video_gen_inputs'][0]
            unified=item.get('unified_edit_input')
            if unified is not None and not isinstance(unified,dict):return None
            unified=unified or {}
            if not isinstance(unified.get('meta_list',[]),list):return None
            text=''.join(x.get('text','') for x in unified.get('meta_list',[]) if x.get('meta_type')=='text').strip()
            if params['model_req_key']!=model_config(spec['model'])['model_req_key'] or params['video_aspect_ratio']!=spec['ratio']:return None
            if has_prompt_mentions(spec['prompt']):
                verify_prompt_mentions(spec['prompt'],spec.get('references',[]),unified,bindings)
            elif not prompt_text_equal(item.get('prompt',''),spec['prompt']) and not prompt_text_equal(text,spec['prompt']):return None
            if item['duration_ms']!=spec['duration']*1000 or item['resolution'].lower()!=spec['resolution'].lower():return None
            refs=unified.get('material_list')
            if refs is not None and not isinstance(refs,list):return None
            refs=refs or []
            expected=spec.get('references',[])
            if len(refs)!=len(expected):return None
            for kind in ('image','video'):
                if sum(r.get('material_type')==kind for r in refs)!=sum(r['kind']==kind for r in expected):return None
        result={'submit_id':submit_id,'api_origin':f'{parsed.scheme}://{parsed.netloc}',
                'conversation_id':str(body['extend']['workspace_id']) if body.get('extend',{}).get('workspace_id') is not None else None,
                'request_summary':{'model':spec['model'],'prompt':spec['prompt'],'duration':spec.get('duration'),
                    'resolution':spec.get('resolution'),'ratio':spec.get('ratio'),'reference_count':len(refs)}}
        if spec.get('kind')!='image' and has_prompt_mentions(spec['prompt']) and bindings is not None:
            result['reference_bindings']=deepcopy(bindings)
        return result
    except (ValueError,KeyError,IndexError,TypeError,AttributeError,RecursionError,OverflowError):return None


def generation_response(request:dict, status:int, data) -> dict:
    result={**request,'http_status':status}
    diagnostic=normalize_upstream_error(data,status)
    if not isinstance(data,dict):return result|{'status':'uncertain','error':'Generation response was not a JSON object.','upstream_error':diagnostic}
    value=data.get('ret','')
    if isinstance(value,bool) or not isinstance(value,(str,int)):
        return result|{'status':'uncertain','error':'Generation response has an invalid return code.','upstream_error':diagnostic}
    ret=str(value)
    if ret and not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',ret):
        return result|{'status':'uncertain','error':'Generation response has an invalid return code.','upstream_error':diagnostic}
    result['ret']=ret
    if ret and ret!='0':
        if has_generation_acceptance(data):
            return result|{'status':'uncertain','error_code':'contradictory_generation_response',
                           'error':'Generation response contains both rejection and possible acceptance evidence; reconcile the original task.',
                           'upstream_error':diagnostic}
        return result|{'status':'failed','error':f'Generation rejected by upstream: ret={ret}.','upstream_error':diagnostic}
    nested=data.get('data');record=nested.get('aigc_data') if isinstance(nested,dict) else None
    if ret!='0' or type(status) is not int or not 200<=status<300 or not isinstance(record,dict) or not record:return result|{'status':'uncertain','error':'No confirmed generation task in response.','upstream_error':diagnostic}
    if record.get('submit_id') and record['submit_id']!=request['submit_id']:return result|{'status':'uncertain','error':'Generation task identity mismatch.','upstream_error':diagnostic}
    history_id=record.get('history_record_id')
    valid_history=isinstance(history_id,(str,int)) and not isinstance(history_id,bool) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(history_id)))
    if not record.get('submit_id') and not valid_history:return result|{'status':'uncertain','error':'Generation response lacks task identity.','upstream_error':diagnostic}
    return result|{'status':'submitted','history_record_id':str(history_id) if valid_history else ''}


def committed_resource_ids(action,data):
    """Return only concrete accepted identifiers, deduplicated within one commit."""
    if action not in ('CommitImageUpload','CommitUploadInner') or not isinstance(data,dict):return ()
    metadata=data.get('ResponseMetadata')
    if isinstance(metadata,dict) and metadata.get('Error'):return ()
    if data.get('Error') or data.get('error') or str(data.get('ret','0'))!='0':return ()
    result=data.get('Result')
    if not isinstance(result,dict) or result.get('Error'):return ()
    rows=result.get('Results') or []
    rows=rows if isinstance(rows,list) else []
    def identifier(value):
        return value if isinstance(value,str) and 0<len(value.strip())<=4096 and not any(c in value for c in '\r\n') else None
    if action=='CommitUploadInner':
        return tuple(sorted({value for row in rows if isinstance(row,dict) and not row.get('Error')
                             for value in [identifier(row.get('Vid'))] if value}))
    accepted=set();rejected=set()
    for row in rows:
        if not isinstance(row,dict):continue
        value=identifier(row.get('Uri') or row.get('ImageUri'))
        if not value:
            if row.get('UriStatus') not in (None,2000) or row.get('Error'):return ()
            continue
        (accepted if row.get('UriStatus') in (None,2000) and not row.get('Error') else rejected).add(value)
    plugins=result.get('PluginResult') or []
    for row in plugins if isinstance(plugins,list) else []:
        if not isinstance(row,dict) or row.get('Error'):continue
        value=identifier(row.get('ImageUri'))
        if value:accepted.add(value)
    return tuple(sorted(accepted-rejected))


def request_belongs_to_page(request,page):
    """Known other pages cannot supply this job's evidence; worker frames may be absent."""
    try:
        frame=request.frame
        return frame is None or frame.page is page
    except Exception:
        # Playwright deliberately raises for service-worker/early-frame requests.
        # Their payload still has to pass the full generation_request match.
        return True


class NativeUI:
    def __init__(self, page, telemetry, *, timeout_ms=20000, diagnostics_dir:Path|None=None, require_bootstrap=False, submission_timeout_ms=90000):
        self.page=page;self.telemetry=telemetry;self.timeout_ms=timeout_ms
        self.diagnostics_dir=diagnostics_dir
        self.clicked=False;self.request_observed=None;self.response_observed=None
        self.before_preflight=None;self.before_activation_turn=None;self.telemetry_failures=0
        self._content_confirmation_attempted=False
        self.reference_bindings={}
        self._reference_commit_keys=set();self._reference_resource_ids=set()
        self.submission_issue=None
        self._promotion_guard_depth=0;self._promotions_dismissed=0;self._promotion_dismissing=False
        self.submission_timeout_ms=submission_timeout_ms
        self.require_bootstrap=require_bootstrap;self.bootstrap=set();self.bootstrap_tasks=set()
        self.bootstrap_paths={'/mweb/v1/get_common_config','/mweb/v1/video_generate/get_common_config'}
        if require_bootstrap:self.page.on('response',self._bootstrap_response)

    def _bootstrap_response(self,response):
        parsed=urlsplit(response.url)
        if parsed.path not in self.bootstrap_paths or f'{parsed.scheme}://{parsed.netloc}' not in API_ORIGINS or response.status!=200:return
        async def inspect():
            try:
                body=await response.json()
                if isinstance(body,dict) and str(body.get('ret',body.get('code')))=='0':self.bootstrap.add(parsed.path)
            except Exception:pass
        task=asyncio.create_task(inspect());self.bootstrap_tasks.add(task);task.add_done_callback(self.bootstrap_tasks.discard)

    async def video_bootstrap(self):
        if not self.require_bootstrap:return
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            if self.bootstrap_paths<=self.bootstrap:
                await asyncio.wait_for(self.page.evaluate('''()=>new Promise(resolve=>{
                    let done=false;const finish=()=>{if(!done){done=true;resolve();}};
                    setTimeout(finish,250);
                    requestAnimationFrame(()=>requestAnimationFrame(finish));
                })'''),2)
                await self.stage('video_initialization_ready',{'confirmed_paths':sorted(self.bootstrap)})
                return
            await asyncio.sleep(.25)
        raise UIUnavailable('Video initialization not confirmed: '+', '.join(sorted(self.bootstrap_paths-self.bootstrap))+'. No generation submitted.')

    async def stage(self,name,detail=None,**fields):
        # Optional observability may never overwrite a durable generation receipt.
        try:await asyncio.wait_for(self.telemetry({'stage':name,'observed_at':time.time(),'detail':detail or {},**fields}),1)
        except asyncio.CancelledError:
            # A cancelled telemetry child is optional; cancellation of this
            # actual generation coroutine must still reach the receipt guard.
            if asyncio.current_task().cancelling():raise
            self.telemetry_failures+=1
        except Exception:self.telemetry_failures+=1

    async def dispose(self):
        if self.require_bootstrap:self.page.remove_listener('response',self._bootstrap_response)
        for task in self.bootstrap_tasks:task.cancel()
        if self.bootstrap_tasks:await asyncio.gather(*self.bootstrap_tasks,return_exceptions=True)

    async def visible(self,locator):
        return [item for item in await locator.all() if await item.is_visible()]

    async def one(self,locator,label):
        items=await self.visible(locator)
        if len(items)!=1:raise UIUnavailable(f'{label}: expected one visible control, found {len(items)}.')
        return items[0]

    async def prompt(self):
        return await self.one(self.page.locator('[contenteditable="true"][role="textbox"][aria-label="Prompt"]'),'Prompt editor')

    def promotion_dialogs(self):
        return self.page.get_by_role('dialog').filter(has_text=re.compile(r'\bSave up to\b',re.I)).filter(
            has=self.page.get_by_role('button',name='Get offer',exact=True))

    async def dismiss_known_promotion(self):
        """Dismiss only the observed promotion, and only before activation."""
        if self.clicked:return False
        dialogs=await self.visible(self.promotion_dialogs())
        if not dialogs:return False
        if len(dialogs)!=1:raise UIUnavailable('Multiple promotion dialogs require review before generation.')
        dialog=dialogs[0]
        if re.search(r'captcha|security verification|verify you|sign in|log in|验证码',await dialog.inner_text(),re.I):
            raise UIUnavailable('A verification or login dialog requires review before generation.')
        offers=await self.visible(dialog.get_by_role('button',name='Get offer',exact=True))
        closes=await self.visible(dialog.get_by_role('button',name='Close',exact=True))
        if len(offers)!=1 or len(closes)!=1:
            raise UIUnavailable('The promotion has no unique known Close control; no generation submitted.')
        self._promotion_dismissing=True
        try:
            await closes[0].click(timeout=min(self.timeout_ms,3000))
            await dialog.wait_for(state='hidden',timeout=min(self.timeout_ms,3000))
        finally:self._promotion_dismissing=False
        self._promotions_dismissed+=1
        await self.stage('promotion_dismissed',{'phase':'preparation','purchased':False})
        return True

    @asynccontextmanager
    async def preparation_promotions(self):
        """Handle overlays appearing during actionability waits, then unregister."""
        owner=self._promotion_guard_depth==0
        locator=self.promotion_dialogs()
        if owner:
            async def handler(_):
                # Explicit Close actions also pass through actionability checks.
                # Let that close finish instead of recursively closing it twice.
                if self._promotion_dismissing:return
                if not await self.dismiss_known_promotion():
                    raise UIUnavailable('Unresolved promotion dialog before generation.')
            # dismiss_known_promotion itself waits for hidden; this flag permits
            # the reentrant no-op above without waiting for its own pending close.
            await self.page.add_locator_handler(locator,handler,no_wait_after=True)
        self._promotion_guard_depth+=1
        try:yield
        finally:
            self._promotion_guard_depth-=1
            if owner:await self.page.remove_locator_handler(locator)

    async def diagnostic(self,tag):
        if not self.diagnostics_dir:return
        self.diagnostics_dir.mkdir(parents=True,exist_ok=True)
        try:
            await self.page.screenshot(path=str(self.diagnostics_dir/f'{tag}.png'),full_page=False)
            text=await self.page.locator('body').aria_snapshot()
            (self.diagnostics_dir/f'{tag}.txt').write_text(text[:60000],encoding='utf-8')
            editors=await self.page.locator('[contenteditable="true"][aria-label="Prompt"]').evaluate_all('(els)=>els.map(e=>({html:e.outerHTML.slice(0,5000),text:e.textContent,visible:!!e.getClientRects().length}))')
            (self.diagnostics_dir/f'{tag}-editors.json').write_text(json.dumps(editors,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception:pass

    async def check(self):
        if urlsplit(self.page.url).hostname!='dreamina.capcut.com':return {'ready':False,'reason':'Not on the official Dreamina page.'}
        try:
            await self.dismiss_known_promotion()
            body=(await self.page.locator('body').inner_text())[:30000]
            if re.search(r'verify you|captcha|security verification|拖动滑块|验证码',body,re.I):return {'ready':False,'reason':'Human verification required.'}
            translated=await self.page.locator('html').get_attribute('class') or ''
            if 'translated-' in translated:return {'ready':False,'reason':'Restore the original page language before automation.'}
            if await self.visible(self.page.get_by_role('button',name=re.compile(r'^(Sign in|Log in|登录)$',re.I))):return {'ready':False,'reason':'Login required in this dedicated browser profile.'}
            editors=await self.visible(self.page.locator('[contenteditable="true"][aria-label="Prompt"]'))
            new_session=await self.visible(self.page.get_by_role('button',name='New session',exact=True))
            return {'ready':bool(editors and new_session),'reason':'Ready' if editors and new_session else 'Waiting for the generation page controls.'}
        except Exception:return {'ready':False,'reason':'Page is loading or unavailable.'}

    async def wait_ready(self,timeout=60):
        deadline=time.monotonic()+timeout;state={}
        while time.monotonic()<deadline:
            state=await self.check()
            if state['ready']:return state
            if 'Login required' in state['reason'] or 'verification' in state['reason']:return state
            await asyncio.sleep(.5)
        return state

    async def new_session(self):
        await self.dismiss_known_promotion()
        button=await self.one(self.page.get_by_role('button',name='New session',exact=True),'New session')
        # This sidebar action may sit under its hover container; use its native keyboard activation.
        await button.press('Enter',timeout=self.timeout_ms)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                editor=await self.prompt()
                records=await self.visible(self.page.locator('[data-generated-record-key]'))
                prompt_text=await editor.evaluate('''e=>{const copy=e.cloneNode(true);copy.querySelectorAll('.rich-placeholder-widget.ProseMirror-widget[contenteditable="false"]').forEach(n=>n.remove());return copy.textContent||'';}''')
                # The landing page can retain an unsent draft; it is still a new conversation.
                landing=await self.visible(self.page.get_by_role('heading',name='What are we creating today?',exact=True))
                if prompt_text.strip() and landing and not records:
                    await editor.fill('');continue
                composer=self.page.get_by_role('region',name='Generate',exact=True)
                references=await self.visible(composer.get_by_role('button',name=re.compile(r'^Remove\s*[·•-]\s*Reference',re.I)))
                tags=await self.visible(editor.locator('.node-reference-mention-tag'))
                residual_media=await self.visible(composer.locator('img[src^="blob:"],img[src^="data:image/"],video,[data-material-id]'))
                if landing and not prompt_text.strip() and not records and references:
                    await self.clear_draft_references(composer,deadline)
                    continue
                if not prompt_text.strip() and not records and not references and not tags and not residual_media:
                    await self.stage('new_session',{'empty_editor':True,'existing_records':0,'existing_references':0},session_path=urlsplit(self.page.url).path)
                    return
            except ReferenceCleanupUnavailable:raise
            except UIUnavailable:pass
            await asyncio.sleep(.25)
        raise UIUnavailable('Could not verify a new empty conversation; no generation submitted.')

    async def clear_draft_references(self,composer,deadline):
        """Remove only draft composer attachments after new-session validation.

        The site retains cards across New session. A card's outer role=button
        can inherit its inner Remove label, so operate only actual BUTTON
        controls and require measurable progress after each native click.
        """
        pattern=re.compile(r'^Remove\s*[·•-]\s*Reference$',re.I)
        for _ in range(50):
            controls=[]
            for item in await self.visible(composer.get_by_role('button',name=pattern)):
                if await item.evaluate("e=>e.tagName==='BUTTON'"):controls.append(item)
            if not controls:return
            if time.monotonic()>=deadline:raise ReferenceCleanupUnavailable('Reference cleanup timed out before generation.')
            before=len(controls)
            if not await controls[-1].is_enabled():raise ReferenceCleanupUnavailable('Reference removal control is disabled; no generation submitted.')
            # Native keyboard activation works when the responsive hover card
            # overlays its remove button or headless rAF is temporarily paused.
            await controls[-1].press('Enter',timeout=max(1,min(self.timeout_ms,int((deadline-time.monotonic())*1000))))
            progressed=False
            for _ in range(8):
                await asyncio.sleep(.1)
                current=[item for item in await self.visible(composer.get_by_role('button',name=pattern))
                         if await item.evaluate("e=>e.tagName==='BUTTON'")]
                if len(current)<before:progressed=True;break
            if not progressed:raise ReferenceCleanupUnavailable('Reference removal did not clear a draft attachment; no generation submitted.')
        if await self.visible(composer.get_by_role('button',name=pattern)):
            raise ReferenceCleanupUnavailable('Too many draft references to clear safely; no generation submitted.')

    async def verify_reference_count(self,spec):
        """Read the current composer atomically immediately around activation.

        An outer card may itself have role=button, so only actual Remove
        buttons count. Prompt mention thumbnails are not uploaded materials.
        """
        expected=len(spec.get('references',[]));editor=await self.prompt()
        counts=await editor.evaluate('''editor=>{
          const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
          const name=e=>(e.getAttribute('aria-label')||e.getAttribute('title')||e.textContent||'').trim();
          const generate=[...document.querySelectorAll('button')].filter(e=>visible(e)&&name(e)==='Generate');
          if(generate.length!==1)return null;
          let root=editor.closest('[role="region"][aria-label="Generate"]')||editor.parentElement;
          while(root&&!root.contains(generate[0]))root=root.parentElement;
          if(!root||[...root.querySelectorAll('[contenteditable="true"][role="textbox"][aria-label="Prompt"]')].filter(visible).length!==1)return null;
          const buttons=[...root.querySelectorAll('button')].filter(e=>visible(e)&&/^Remove\\s*[·•-]\\s*Reference$/i.test(name(e)));
          const media=[...root.querySelectorAll('img,video')].filter(e=>visible(e)&&!editor.contains(e));
          return {buttons:buttons.length,media:media.length};
        }''')
        if not isinstance(counts,dict) or counts.get('buttons')!=expected or counts.get('media')!=expected:
            error=ReferenceCountMismatch('Reference cards changed before generation; expected '+str(expected)+' uploaded materials. No Generate action was sent.')
            error.native_activation_not_attempted=not self.clicked and not self.request_observed
            raise error
        return counts

    async def combo(self,label,value):
        control=await self.one(self.page.locator(f'[role="combobox"][aria-label="{label}"]'),label)
        compact=lambda x:re.sub(r'\s+','',x).lower()
        if compact(await control.inner_text())==compact(value):return
        options=self.page.locator('[role="listbox"] [role="option"]')
        deadline=time.monotonic()+10;opens=0;dismissals_at_open=self._promotions_dismissed
        while time.monotonic()<deadline:
            await self.dismiss_known_promotion()
            if compact(await control.inner_text())==compact(value):return
            if not await self.visible(options):
                # An opening menu can load asynchronously. Reopen only after an
                # observed dismissal or an explicitly collapsed control.
                if opens and self._promotions_dismissed==dismissals_at_open and await control.get_attribute('aria-expanded')!='false':
                    await asyncio.sleep(.2);continue
                if opens>=3:break
                await control.click(timeout=self.timeout_ms);opens+=1;dismissals_at_open=self._promotions_dismissed
                await asyncio.sleep(.1)
                continue
            matches=[]
            for item in await self.visible(options):
                found=await item.evaluate('''(element, expected) => [element,...element.querySelectorAll('*')].some(n=>{
                    const norm=s=>String(s||'').replace(/\\s+/g,'').toLowerCase();
                    return norm(n.textContent)===norm(expected)||norm([...n.childNodes].filter(x=>x.nodeType===3).map(x=>x.textContent).join(''))===norm(expected);
                })''',value)
                if found:matches.append(item)
            if len(matches)==1:
                dismissals=self._promotions_dismissed
                try:await matches[0].click(timeout=min(self.timeout_ms,2500))
                except PlaywrightTimeoutError:
                    # Closing the promotion may also dismiss the underlying menu.
                    # Only preparation selectors are reopened, never Generate.
                    if self._promotions_dismissed==dismissals:raise
            await asyncio.sleep(.2)
        raise UIUnavailable(f'Could not select the exact {label}: {value}.')

    async def composer_button(self,pattern):
        return await self.one(self.page.get_by_role('button',name=pattern),'Generation option')

    async def subscription_gate_visible(self):
        # Compatibility name for diagnostics: this only observes a pricing panel.
        controls=self.page.get_by_role('button',name=re.compile(r'^(Upgrade to (Standard|Advanced|Ultra)|Switch to Basic)'))
        return bool(await self.visible(controls))

    async def model_upgrade_notice_visible(self,spec):
        name=IMAGE_MODEL_NAME if spec.get('kind')=='image' else model_config(spec['model'])['name']
        notices=self.page.locator('[role="dialog"],[role="alert"],[role="status"],h1,h2,h3,p')
        for notice in await self.visible(notices):
            if explicit_model_upgrade_notice(await notice.inner_text(),name):return True
        return False

    async def choose_radio(self,value):
        labels=await self.visible(self.page.locator('label').filter(has=self.page.locator('input[type="radio"]')))
        matches=[label for label in labels if (await label.locator('input[type="radio"]').get_attribute('value') or '').lower()==value.lower()]
        if len(matches)!=1:raise UIUnavailable('Could not identify radio option '+value)
        await matches[0].click(timeout=self.timeout_ms)

    async def configure(self,spec):
        async with self.preparation_promotions():
            await self._configure(spec)

    async def _configure(self,spec):
        image=spec.get('kind')=='image'
        await self.combo('Create','AI Image' if image else 'AI Video')
        if not image:await self.video_bootstrap()
        await self.page.locator('[role="combobox"][aria-label="Model"]').filter(visible=True).wait_for(timeout=self.timeout_ms)
        await self.combo('Model',IMAGE_MODEL_NAME if image else model_config(spec['model'])['name'])
        if not image:
            mode_options=self.page.locator('[role="combobox"]:not([aria-label])')
            modes=await self.visible(mode_options)
            if len(modes)!=1:raise UIUnavailable('Cannot identify the reference mode selector.')
            if re.sub(r'\s+','',(await modes[0].inner_text())).lower()!='omnireference':
                await modes[0].click()
                await (await self.one(self.page.get_by_role('option',name=re.compile('^Omni reference')),'Omni reference')).click()
        ratio='1:1' if image else spec['ratio'];resolution='2K' if image else spec['resolution']
        dimensions=await self.composer_button(re.compile(r'^(Auto ratio|\d+:\d+)\s*(480P|720P|1080P|2K|4K)$',re.I))
        if re.sub(r'\s+','',(await dimensions.inner_text())).lower()!=(ratio+resolution).lower():
            await dimensions.click();await self.choose_radio(ratio)
            await self.choose_radio(resolution)
            await (await self.composer_button(re.compile('^'+re.escape(ratio)+r'\s*'+re.escape(resolution)+'$',re.I))).click()
        if image:
            count=await self.composer_button(re.compile('^Images to generate:'))
            if await count.get_attribute('aria-label')!='Images to generate: 1':
                await count.click();await self.choose_radio('1')
                await (await self.composer_button('Images to generate: 1')).click()
        else:
            duration=await self.composer_button(re.compile(r'^\d+s$'))
            if (await duration.inner_text()).strip()!=str(spec['duration'])+'s':
                await duration.click()
                input_=await self.one(self.page.get_by_role('spinbutton'),'Duration')
                await input_.fill(str(spec['duration']));await input_.press('Enter')
                await (await self.composer_button(str(spec['duration'])+'s')).click()
        await self.verify_configuration(spec)
        await self.stage('parameters_ready',{'model':spec['model'],'duration':spec.get('duration'),'resolution':resolution,'ratio':ratio,'readback_verified':True})

    async def verify_configuration(self,spec):
        """Read the live controls back; never silently accept a reset/default."""
        image=spec.get('kind')=='image';compact=lambda x:re.sub(r'\s+','',x).lower()
        for label,expected in (('Create','AI Image' if image else 'AI Video'),
                ('Model',IMAGE_MODEL_NAME if image else model_config(spec['model'])['name'])):
            control=await self.one(self.page.locator(f'[role="combobox"][aria-label="{label}"]'),label)
            if compact(await control.inner_text())!=compact(expected):
                raise UIUnavailable(label+' changed during preparation; no generation submitted.')
        ratio='1:1' if image else spec['ratio'];resolution='2K' if image else spec['resolution']
        dimensions=await self.composer_button(re.compile(r'^(Auto ratio|\d+:\d+)\s*(480P|720P|1080P|2K|4K)$',re.I))
        if compact(await dimensions.inner_text())!=compact(ratio+resolution):
            raise UIUnavailable('Ratio or resolution changed during preparation; no generation submitted.')
        if image:
            count=await self.composer_button(re.compile('^Images to generate:'))
            if await count.get_attribute('aria-label')!='Images to generate: 1':
                raise UIUnavailable('Image count changed during preparation; no generation submitted.')
        else:
            mode=await self.one(self.page.locator('[role="combobox"]:not([aria-label])'),'Reference mode')
            if compact(await mode.inner_text())!='omnireference':
                raise UIUnavailable('Reference mode changed during preparation; no generation submitted.')
            duration=await self.composer_button(re.compile(r'^\d+s$'))
            if (await duration.inner_text()).strip()!=str(spec['duration'])+'s':
                raise UIUnavailable('Duration changed during preparation; no generation submitted.')

    async def upload(self,spec,files):
        if has_prompt_mentions(spec.get('prompt','')):
            parse_prompt_mentions(spec['prompt'],spec.get('references',[]))
            if len(files)!=len(spec.get('references',[])):raise ValueError('Reference files do not match the validated request.')
            # Receipt identity is correlated per file, never by parallel upload order.
            self.reference_bindings={}
            self._reference_commit_keys=set();self._reference_resource_ids=set()
            for reference,path in zip(spec['references'],files):
                await self._upload({**spec,'references':[reference]},[path],capture_binding=True)
            videos=[value['resource_id'] for value in self.reference_bindings.values() if value['kind']=='video']
            if len(videos)!=len(set(videos)):
                raise UIUnavailable('Duplicate video upload identities are ambiguous; reuse one video mention instead of uploading it twice. No generation submitted.')
        else:await self._upload(spec,files)

    async def _upload(self,spec,files,*,capture_binding=False):
        references=spec.get('references',[])
        if not isinstance(references,list) or len(files)!=len(references) or any(not isinstance(r,dict) or r.get('kind') not in ('image','video') for r in references):
            raise ValueError('Reference files do not match the validated request.')
        if not files:return
        await self.stage('uploading_references',{'count':len(files),'kinds':[r['kind'] for r in spec['references']]})
        commits={'image':0,'video':0};pending=set();resources={'image':set(),'video':set()}
        # A previous file's Commit response may arrive again while the next file
        # is uploading. Keep receipt and resource deduplication across the entire
        # mention batch, not just this file's response listener.
        seen_commits=self._reference_commit_keys if capture_binding else set()
        async def observe(response):
            try:
                parsed=urlsplit(response.url);action=parse_qs(parsed.query).get('Action',[''])[0]
                host=parsed.hostname or ''
                if parsed.scheme!='https' or not any(host==d or host.endswith('.'+d) for d in ('capcutapi.us','capcutapi.com')):return
                if action not in ('CommitImageUpload','CommitUploadInner') or response.status!=200 or not request_belongs_to_page(response.request,self.page):return
                # Retransmitted commit acknowledgements for the same upload are one receipt.
                key=(action,response.request)
                try:
                    body=json.loads(response.request.post_data or '{}');session=body.get('SessionKey')
                    if isinstance(session,str) and session:key=(action,hashlib.sha256(session.encode()).digest())
                except (ValueError,TypeError,AttributeError):pass
                data=await asyncio.wait_for(response.json(),15)
                identifiers=committed_resource_ids(action,data)
                if not identifiers or key in seen_commits:return
                seen_commits.add(key)
                kind='image' if action=='CommitImageUpload' else 'video'
                if capture_binding:
                    if kind!=references[0]['kind']:return
                    identifiers=tuple(value for value in identifiers if (kind,value) not in self._reference_resource_ids)
                    if not identifiers:return
                    self._reference_resource_ids.update((kind,value) for value in identifiers)
                resources[kind].update(identifiers);commits[kind]+=len(identifiers)
            except Exception:pass
        def response_event(response):
            task=asyncio.create_task(observe(response));pending.add(task);task.add_done_callback(pending.discard)
        self.page.on('response',response_event)
        try:
            inputs=self.page.locator('input[type="file"]')
            options=[]
            for input_ in await inputs.all():
                accept=(await input_.get_attribute('accept') or '').lower()
                if not accept or all(('video' in accept or 'mp4' in accept) if r['kind']=='video' else ('image' in accept or 'png' in accept) for r in spec['references']):options.append(input_)
            if options:
                multiple=[i for i in options if await i.get_attribute('multiple') is not None]
                target=multiple[0] if multiple else options[0]
                if len(files)>1 and not multiple:
                    for path in files:await target.set_input_files(str(path))
                else:await target.set_input_files([str(p) for p in files])
            else:
                upload=await self.composer_button(re.compile(r'^(Reference|Upload references)$'))
                selection=await choose_reference_files(self.page,upload,files,timeout_ms=self.timeout_ms)
                await self.stage('reference_files_selected',selection)
            required={kind:sum(r['kind']==kind for r in spec['references']) for kind in ('image','video')}
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                body=await self.page.locator('body').inner_text()
                if re.search(r'upload failed|unsupported file|上传失败',body,re.I):raise UIUnavailable('Reference upload rejected by the page.')
                if all(commits[k]>=required[k] for k in required):
                    if capture_binding:
                        reference=references[0];kind=reference['kind'];asset_id=reference.get('asset_id')
                        if not isinstance(asset_id,str) or len(resources[kind])!=1:
                            raise UIUnavailable('Reference upload identity is ambiguous; no generation submitted.')
                        resource_id=next(iter(resources[kind]))
                        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_.-]{0,1023}',resource_id):
                            raise UIUnavailable('Reference upload identifier cannot be persisted safely; no generation submitted.')
                        self.reference_bindings[asset_id]={'kind':kind,'resource_id':resource_id}
                    await self.stage('references_ready',{'count':len(files),'commit_counts':commits})
                    return
                await asyncio.sleep(.5)
            raise UIUnavailable('Reference commits were not fully confirmed; no generation submitted.')
        finally:
            self.page.remove_listener('response',response_event)
            for task in pending:task.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)

    async def write_prompt(self,spec):
        editor=await self.prompt()
        if spec.get('kind')=='image' or not has_prompt_mentions(spec['prompt']):
            await editor.fill(spec['prompt']);return
        segments=parse_prompt_mentions(spec['prompt'],spec.get('references',[]))
        await editor.fill('')
        count=0
        for segment in segments:
            await editor.press('Control+End')
            if segment['type']=='text':
                await self.page.keyboard.insert_text(segment['text']);continue
            asset_id=segment['asset_id']
            if not isinstance(asset_id,str) or not re.fullmatch(r'asset_\d{1,2}_[0-9a-f]{24}',asset_id):
                raise UIUnavailable('Prompt mention has no trusted upload identity; no generation submitted.')
            await editor.press_sequentially('@')
            options=self.page.locator('[role="listbox"] [role="option"]').filter(has=self.page.get_by_text(asset_id,exact=True))
            await options.wait_for(state='visible',timeout=self.timeout_ms)
            await (await self.one(options,'Exact reference mention')).click(timeout=self.timeout_ms)
            count+=1
            await self.page.wait_for_function('''({count})=>{
              const es=[...document.querySelectorAll('[contenteditable="true"][aria-label="Prompt"]')].filter(e=>e.getClientRects().length);
              return es.length===1&&es[0].querySelectorAll('.node-reference-mention-tag').length===count;
            }''',arg={'count':count},timeout=self.timeout_ms)
        await self.verify_prompt(spec)
        await self.stage('prompt_mentions_ready',{'mention_count':count,'bindings_verified':True})

    async def verify_prompt(self,spec):
        if spec.get('kind')=='image' or not has_prompt_mentions(spec['prompt']):return
        segments=parse_prompt_mentions(spec['prompt'],spec.get('references',[]))
        expected=[]
        def text(value):
            if not value:return
            if expected and expected[-1]['type']=='text':expected[-1]['text']+=value
            else:expected.append({'type':'text','text':value})
        for segment in segments:
            if segment['type']=='text':text(segment['text'])
            else:expected.append({'type':'reference','asset_id':segment['asset_id']})
        editor=await self.prompt()
        actual=await editor.evaluate('''editor=>{
          const out=[];
          const text=s=>{if(!s)return;const last=out[out.length-1];if(last?.type==='text')last.text+=s;else out.push({type:'text',text:s});};
          const walk=n=>{
            if(n.nodeType===Node.TEXT_NODE){text(n.textContent);return;}
            if(n.nodeType!==Node.ELEMENT_NODE)return;
            if(n.matches('.node-reference-mention-tag')){out.push({type:'reference',asset_id:n.textContent.trim()});return;}
            if(n.matches('.rich-placeholder-widget.ProseMirror-widget[contenteditable="false"],.ProseMirror-separator,.ProseMirror-trailingBreak'))return;
            if(n.tagName==='BR'){text('\\n');return;}
            if(n.tagName==='P'&&n.previousElementSibling?.tagName==='P')text('\\n');
            for(const child of n.childNodes)walk(child);
          };
          for(const node of editor.childNodes)walk(node);
          return out;
        }''')
        expected=[dict(item,text=canonical_prompt_text(item['text'])) if item['type']=='text' else item for item in expected]
        actual=[dict(item,text=canonical_prompt_text(item['text'])) if item['type']=='text' else item for item in actual]
        if actual!=expected:raise UIUnavailable('Prompt text or native reference tags changed during preparation; no generation submitted.')

    async def credit_quote(self,spec,*,timeout_ms=None):
        """Read one exact account-specific quote twice while parameters stay fixed."""
        timeout_ms=min(self.timeout_ms,15000) if timeout_ms is None else timeout_ms
        deadline=time.monotonic()+timeout_ms/1000;previous=None;since=0;reason='price_unavailable'
        while time.monotonic()<deadline:
            await self.dismiss_known_promotion();await self.verify_configuration(spec);await self.verify_prompt(spec)
            region=await self.one(self.page.get_by_role('region',name='Generate',exact=True),'Generation price region')
            snapshot=await region.evaluate(READ_NATIVE_CREDIT_PRICE)
            try:
                if not isinstance(snapshot,dict) or snapshot.get('available') is not True:
                    reason=snapshot.get('reason','price_unavailable') if isinstance(snapshot,dict) else 'price_unavailable'
                    raise ValueError(reason)
                required=parse_exact_credit_price(snapshot.get('text'),kind='image' if spec.get('kind')=='image' else 'video',count=spec.get('n',1))
            except ValueError:
                previous=None;since=0
            else:
                marker=(required,quote_request_fingerprint(spec))
                if marker==previous and time.monotonic()-since>=.5:
                    return {'required_credits':required,'source':'native_page','observed_at':time.time(),
                            'model':spec['model'],'request_fingerprint':marker[1]}
                if marker!=previous:previous=marker;since=time.monotonic()
            await asyncio.sleep(.25)
        raise CreditQuoteUnavailable('Credit quote unavailable or not stable ('+str(reason)+'); no generation submitted.')

    async def confirm_content_notice(self,spec,can_continue,*,timeout_ms):
        """Continue only the upload-rights content notice after one activation.

        This is part of that activation, never a second Generate click. Mark the
        attempt before the native click: a timeout cannot cause another assent.
        """
        if not self.clicked or self._content_confirmation_attempted or self.request_observed or not can_continue():return False
        dialogs=await self.visible(self.page.get_by_role('dialog'))
        matches=await self.visible(self.page.get_by_role('dialog',name=re.compile(r'^(?:Before you continue|Content confirmation|Confirm content rights)$',re.I)))
        if len(dialogs)!=1 or len(matches)!=1:return False
        dialog=matches[0]
        buttons=await self.visible(dialog.get_by_role('button'))
        confirm=await self.visible(dialog.get_by_role('button',name='Confirm',exact=True))
        cancel=await self.visible(dialog.get_by_role('button',name='Cancel',exact=True))
        if len(buttons)!=2 or len(confirm)!=1 or len(cancel)!=1:return False
        if await self.visible(dialog.locator('input,textarea,select,[role="checkbox"],[role="switch"],[role="combobox"],[role="textbox"],[contenteditable="true"]')):return False
        compact=lambda value:re.sub(r'\s+',' ',value).strip()
        body=compact(await dialog.inner_text())
        if not recognized_content_notice(body):return False
        if not await confirm[0].is_enabled():return False
        await self.verify_configuration(spec);await self.verify_prompt(spec)
        if spec.get('kind')=='image' or not has_prompt_mentions(spec['prompt']):
            editor=await self.prompt()
            if not prompt_text_equal(await editor.inner_text(),spec['prompt']):
                raise UIUnavailable('Prompt changed before content confirmation; no additional activation attempted.')
        if self.request_observed or not can_continue():return False
        self._content_confirmation_attempted=True
        try:await confirm[0].click(timeout=timeout_ms)
        except PlaywrightTimeoutError:return False
        try:await asyncio.wait_for(self.stage('content_confirmation',{'dialog':'Before you continue','attempt':1,'generation_reactivated':False}),.5)
        except Exception:pass  # Receipt persistence is independent of this optional event.
        return True

    @classify_browser_proxy_errors
    async def submit(self,spec,files,before_click,on_request_observed=None,on_preflight=None):
        """Activate once and persist matching request evidence before any response.

        The optional synchronous callback receives the first unique match. If a
        second ID appears it receives an uncertainty notification containing only
        ``observed_submit_ids`` rather than an arbitrarily selected primary ID.
        """
        if self.clicked or self.request_observed:
            raise UIUnavailable('This driver already attempted a submission; reconcile the original task.')
        if on_request_observed is not None and not callable(on_request_observed):
            raise ValueError('on_request_observed must be a synchronous callback.')
        if on_preflight is not None and not callable(on_preflight):
            raise ValueError('on_preflight must be an asynchronous callback.')
        async with self.preparation_promotions():
            await self.new_session();await self.configure(spec);await self.upload(spec,files)
            await self.write_prompt(spec)
            await self.dismiss_known_promotion();await self.verify_configuration(spec)
            button=await self.one(self.page.get_by_role('button',name='Generate',exact=True),'Generate')
            await button.wait_for(state='visible',timeout=self.timeout_ms)
            if on_preflight is None and not await button.is_enabled():raise UIUnavailable('Generate button is disabled.')
        future=asyncio.get_running_loop().create_future();pending=set()
        observed_ids={};persistence_failed=False;ambiguous=False
        def uncertainty():
            if ambiguous:
                return {'status':'uncertain','error_code':'ambiguous_submission','observed_submit_ids':list(observed_ids),
                        'error':'Multiple different matching submission IDs were observed; no primary ID was selected.'}
            if persistence_failed:
                return {**(self.request_observed or {}),'status':'uncertain','error_code':'receipt_persistence_failed',
                        'error':'The observed submission receipt could not be persisted; reconcile the original task without resubmitting.'}
            return None
        def persist(value):
            nonlocal persistence_failed
            if on_request_observed is None:return
            try:
                result=on_request_observed(deepcopy(value))
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):result.close()
                    raise TypeError('The receipt callback must be synchronous.')
            except Exception:persistence_failed=True
        def remember(request):
            nonlocal ambiguous
            if not self.clicked or not request_belongs_to_page(request,self.page):return None
            try:matched=generation_request(request.url,request.method,request.post_data,spec,self.reference_bindings or None)
            except Exception:return None
            if not matched:return None
            submit_id=matched['submit_id']
            if submit_id not in observed_ids:
                # The bounded set is diagnostic evidence, never an automatic multi-submit retry plan.
                if len(observed_ids)<16:observed_ids[submit_id]=matched
                if len(observed_ids)==1 and not ambiguous:
                    self.request_observed=matched;persist(matched)
                else:
                    ambiguous=True;self.request_observed=None;persist(uncertainty())
            override=uncertainty()
            self.submission_issue=deepcopy(override)
            if override and not future.done():future.set_result(override)
            return matched
        def observe_request(request):
            remember(request)
        async def observe(response):
            matched=remember(response.request)
            if not matched:return
            if uncertainty():return
            try:data=await asyncio.wait_for(response.json(),15)
            except Exception:data=None
            result=generation_response(matched,response.status,data)
            if not future.done():future.set_result(result)
        def response_event(response):
            try:
                if urlsplit(response.url).path!=GENERATE_PATH:return
            except (ValueError,TypeError):return
            task=asyncio.create_task(observe(response));pending.add(task);task.add_done_callback(pending.discard)
        self.page.context.on('request',observe_request)
        self.page.context.on('response',response_event)
        try:
            await self.dismiss_known_promotion();await self.verify_configuration(spec)
            await self.verify_prompt(spec)
            if await self.visible(self.page.get_by_role('dialog')):
                raise UIUnavailable('A blocking dialog remains before generation; review it without submitting.')
            if self.before_preflight is not None:await self.before_preflight()
            if on_preflight is not None:
                quote=await self.credit_quote(spec)
                await self.stage('credit_quote_ready',quote)
                await on_preflight(deepcopy(quote))
                checked=await self.credit_quote(spec)
                if checked['required_credits']!=quote['required_credits'] or checked['request_fingerprint']!=quote['request_fingerprint']:
                    raise CreditQuoteUnavailable('Credit quote changed after preflight; no generation submitted.')
                if await self.visible(self.page.get_by_role('dialog')):
                    raise UIUnavailable('A blocking dialog appeared after credit preflight; no generation submitted.')
                if not await button.is_enabled():raise UIUnavailable('Generate button is disabled after credit preflight.')
            if self.before_activation_turn is not None:
                waited=time.monotonic();await self.before_activation_turn()
                if on_preflight is not None:
                    current=await self.credit_quote(spec)
                    if current['required_credits']!=quote['required_credits'] or current['request_fingerprint']!=quote['request_fingerprint']:
                        raise CreditQuoteUnavailable('Credit quote changed while waiting for a submission turn; no generation submitted.')
                    if time.monotonic()-waited>=30:
                        await on_preflight(deepcopy(current))
                        checked=await self.credit_quote(spec)
                        if checked['required_credits']!=current['required_credits'] or checked['request_fingerprint']!=current['request_fingerprint']:
                            raise CreditQuoteUnavailable('Credit quote changed after refreshed preflight; no generation submitted.')
                    if not await button.is_enabled() or await self.visible(self.page.get_by_role('dialog')):
                        raise UIUnavailable('Generation controls changed while waiting for a submission turn; no generation submitted.')
            await self.verify_reference_count(spec)
            await before_click()
            await self.verify_reference_count(spec)
            self.clicked=True
            # Native keyboard activation is stable when the responsive credit badge shifts beside this button.
            try:await button.press('Enter',timeout=min(self.timeout_ms,self.submission_timeout_ms))
            except PlaywrightTimeoutError:pass  # The event may already have dispatched; wait for its original receipt.
            deadline=time.monotonic()+self.submission_timeout_ms/1000;subscription_seen=False
            while not future.done() and time.monotonic()<deadline:
                if not self.request_observed and not self._content_confirmation_attempted:
                    remaining=max(0,deadline-time.monotonic())
                    if remaining:
                        try:
                            await asyncio.wait_for(self.confirm_content_notice(spec,lambda:not future.done(),
                                timeout_ms=max(1,min(self.timeout_ms,5000,int(remaining*1000)))),remaining)
                        except asyncio.TimeoutError:pass
                if future.done():break
                if not self.request_observed and await self.subscription_gate_visible():
                    if not subscription_seen:
                        subscription_seen=True
                        await self.stage('subscription_ui_observed',{'generation_request_observed':False,'model_permission_confirmed':False})
                    # The site's initialization or upsell may precede its request.
                    # Keep observing the original activation until its usual deadline.
                await asyncio.sleep(.25)
            override=uncertainty()
            if override:return override
            if not future.done() and not self.request_observed and subscription_seen:
                explicit=await self.model_upgrade_notice_visible(spec)
                stage='account_plan_required' if explicit else 'subscription_panel_unresolved'
                await self.diagnostic(stage.replace('_','-'))
                if not future.done() and not self.request_observed:
                    await self.stage(stage,{'generation_request_observed':False,'model_permission_confirmed':explicit})
                    # A late request/response during diagnostics still wins over UI text.
                    if not future.done() and not self.request_observed:
                        if explicit:
                            return {'status':'failed','error_code':'account_plan_required','error':'The page explicitly requires a plan upgrade for the selected model. No matching generation request was observed.'}
                        return {'status':'uncertain','error_code':'subscription_panel_unresolved','error':'A subscription panel appeared after activation, but the selected model permission and submission outcome could not be confirmed. Do not resubmit before reconciliation.'}
            if not future.done():return {**(self.request_observed or {}),'status':'uncertain','error':'No matching generation response after the single click; reconcile any observed submit ID.'}
            result=future.result()
            self.response_observed=deepcopy(result)
            await self.stage('generation_response',{k:v for k,v in result.items() if k not in ('error','conversation_id')},conversation_id=result.get('conversation_id'))
            return result
        finally:
            self.page.context.remove_listener('request',observe_request)
            self.page.context.remove_listener('response',response_event)
            if not future.done():future.cancel()
            for task in pending:task.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)

    async def progress(self,submit_id):
        try:
            cards=self.page.locator('[data-generated-record-key]').filter(visible=True)
            for card in await cards.all():
                if await card.get_attribute('data-generated-record-key')!=submit_id:continue
                found=re.search(r'(?:^|\s)(\d{1,2})%',await card.inner_text())
                if found:return int(found[1])
        except Exception:pass
        return None
