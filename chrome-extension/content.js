/* Isolated DOM driver; submission is confirmed by the passive MAIN observer. No Cookie access. */
(() => {
  if (globalThis.__dreaminaLocalBridge) return;
  const logic = globalThis.DreaminaBridgeLogic;
  const state = {activeJobId: null, clicked: false, lastResult: null, lastHumanActivity: 0};
  globalThis.__dreaminaLocalBridge = state;
  const seenJobs = new Set();
  for (const event of ['pointerdown', 'keydown']) {
    document.addEventListener(event, e => { if (e.isTrusted) state.lastHumanActivity = Date.now(); }, true);
  }
  const visible = e => !!e && !e.closest('[aria-hidden="true"],[hidden]') && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
  const name = e => e.getAttribute('aria-label') || e.textContent?.trim() || '';
  const buttons = scope => [...scope.querySelectorAll('button,[role="button"]')].filter(visible);
  const generateButtons = scope => buttons(scope).filter(e => name(e).trim() === 'Generate');
  function inspect(spec = null) {
    if (location.origin !== 'https://dreamina.capcut.com') return {ready: false, reason: '当前页面不是 Dreamina。'};
    if (document.documentElement.classList.contains('translated-ltr') || document.documentElement.classList.contains('translated-rtl')) return {ready:false,reason:'请将 Chrome 网页翻译切回显示原文，避免任务提示词被改写。'};
    const editors = [...document.querySelectorAll('[contenteditable="true"][role="textbox"][aria-label="Prompt"]')].filter(visible);
    if (editors.length !== 1) return {ready: false, reason: '未找到唯一可见的 Prompt 编辑器。'};
    const editor = editors[0];
    let composer = editor.parentElement;
    while (composer && composer !== document.body && !composer.querySelector('[role="combobox"][aria-label="Create"]')) composer = composer.parentElement;
    if (!composer || composer === document.body) {
      const controls=[...document.querySelectorAll('[role="combobox"]')].filter(visible).map(e=>(e.getAttribute('aria-label')||'无标签')+':'+e.textContent.trim().slice(0,60));
      return {ready:false,reason:'无法确认独立生成区。可见选择器：'+(controls.join(' / ')||'无')};
    }
    let shared=composer;
    while(shared&&shared!==document.body&&!generateButtons(shared).length)shared=shared.parentElement;
    if(shared&&shared!==document.body)composer=shared;
    const models = [...composer.querySelectorAll('[role="combobox"][aria-label="Model"]')].filter(visible);
    const creates = [...composer.querySelectorAll('[role="combobox"][aria-label="Create"]')].filter(visible);
    if (creates.length !== 1 || (spec && models.length !== 1)) return {ready: false, reason: '无法确认唯一可见的模型和创建类型选择器。'};
    const model = models[0];
    const create = creates[0];
    const combos = [...composer.querySelectorAll('[role="combobox"]')].filter(visible);
    const localButtons = buttons(composer);
    if (!create || !['aivideo','aiimage','aiagent'].includes(logic.compact(create.textContent))) return {ready:false,reason:'请进入 AI Video、AI Image 或 AI Agent 输入区。'};
    if (spec?.kind === 'image') {
      if (logic.compact(create.textContent) !== 'aiimage' || logic.compact(model.textContent) !== logic.compact(logic.IMAGE_MODEL_NAME)) return {ready:false,reason:'图片模型尚未就绪。'};
      if (!localButtons.some(e=>logic.compact(name(e))==='1:12k') || !localButtons.some(e=>e.getAttribute('aria-label')==='Images to generate: 1')) return {ready:false,reason:'图片尺寸或数量尚未就绪。'};
    } else if (spec) {
      if (logic.compact(create.textContent) !== 'aivideo' || logic.compact(model.textContent)!==logic.compact(logic.MODELS[spec.model]?.name)) return {ready:false,reason:'视频模型尚未就绪。'};
      if (!combos.some(e=>logic.compact(e.textContent)==='omnireference') || !localButtons.some(e=>logic.compact(name(e))===logic.compact(spec.ratio+spec.resolution)) || !localButtons.some(e=>logic.compact(name(e))===spec.duration+'s')) return {ready:false,reason:'视频参数尚未就绪。'};
    }
    // Fail closed on attached media; icons are SVG in the observed composer.
    if (!spec?.references?.length && ([...composer.querySelectorAll('img:not(.ProseMirror-separator),video,audio')].some(visible) || editor.querySelector('img:not(.ProseMirror-separator),video,audio,[data-type="mention"]'))) return {ready: false, reason: '首版仅支持无参考素材，请先移除输入区域中的素材。'};
    const submit = generateButtons(composer);
    if (spec && submit.length !== 1) return {ready: false, reason: '未找到唯一 Generate 按钮。'};
    if (document.querySelector('[role="dialog"][aria-modal="true"]')) return {ready: false, reason: '页面存在对话框，请先手动处理。'};
    return {ready: true, reason: '图片 / 视频原生生成区已连接。', editor, composer, button: submit[0]};
  }
  async function until(find, message) {
    const deadline=Date.now()+12000;
    while(Date.now()<deadline) {const value=find();if(value)return value;await new Promise(r=>setTimeout(r,150));}
    throw new Error(message);
  }
  const visibleAll=selector=>[...document.querySelectorAll(selector)].filter(visible);
  const hasExactLabel=(element,text)=>[element,...element.querySelectorAll('*')].some(node=>
    logic.compact(node.textContent)===logic.compact(text) ||
    logic.compact([...node.childNodes].filter(x=>x.nodeType===Node.TEXT_NODE).map(x=>x.textContent).join(''))===logic.compact(text));
  async function selectCombo(label, text) {
    const combo=await until(()=>{const view=inspect();if(!view.ready)return null;const matches=[...view.composer.querySelectorAll(`[role="combobox"][aria-label="${label}"]`)].filter(visible);return matches.length===1?matches[0]:null;}, '未找到唯一生成区选择器。');
    if(logic.compact(combo.textContent)===logic.compact(text))return;
    combo.click();
    const option=await until(()=>{const matches=visibleAll('[role="listbox"] [role="option"]').filter(e=>hasExactLabel(e,text));return matches.length===1?matches[0]:null;},`未找到唯一 ${text}。`);
    option.click();
    await until(()=>visibleAll(`[role="combobox"][aria-label="${label}"]`).some(e=>logic.compact(e.textContent)===logic.compact(text)), '选择未生效。');
  }
  async function configure(spec) {
    const initial=inspect();if(!initial.ready)throw new Error(initial.reason);
    await selectCombo('Create',spec.kind==='image'?'AI Image':'AI Video');
    await selectCombo('Model',spec.kind==='image'?logic.IMAGE_MODEL_NAME:logic.MODELS[spec.model].name);
    let view=await until(()=>{const v=inspect();return v.ready&&v;},'生成区域尚未就绪。');
    if(spec.kind==='image') {
      const dimensions=buttons(view.composer).find(e=>/^(?:autoratio|\d+:\d+)(?:2k|4k)$/i.test(logic.compact(name(e))));
      if(!dimensions)throw new Error('未找到图片尺寸按钮。');
      if(logic.compact(name(dimensions))!=='1:12k') {
        dimensions.click();
        for(const value of ['1:1','2K']) {
          const radio=await until(()=>visibleAll('label.lv-radio').find(e=>logic.compact(e.innerText)===logic.compact(value)),'未找到图片尺寸选项。');radio.click();
        }
        view=inspect();const close=buttons(view.composer).find(e=>logic.compact(name(e))==='1:12k');if(close)close.click();
      }
      view=inspect();const count=buttons(view.composer).find(e=>/^Images to generate:/.test(e.getAttribute('aria-label')||''));
      if(!count)throw new Error('未找到图片数量按钮。');
      if(count.getAttribute('aria-label')!=='Images to generate: 1') {
        count.click();const one=await until(()=>visibleAll('label.lv-radio').find(e=>e.innerText.trim()==='1'),'未找到单张数量选项。');one.click();
        const close=buttons(inspect().composer).find(e=>e.getAttribute('aria-label')==='Images to generate: 1');if(close)close.click();
      }
    }
    if(spec.kind!=='image') {
      view=inspect();
      const modes=[...view.composer.querySelectorAll('[role="combobox"]')].filter(e=>visible(e)&&!e.getAttribute('aria-label'));
      if(modes.length!==1)throw new Error('未找到唯一视频模式控件。');
      if(logic.compact(modes[0].textContent)!=='omnireference') {
        modes[0].click();
        const option=await until(()=>visibleAll('[role="option"]').find(e=>hasExactLabel(e,'Omni reference')),'未找到 Omni reference。');option.click();
      }
      view=inspect();
      const dimensions=buttons(view.composer).find(e=>/^\d+:\d+(720p|1080p|2k|4k)$/i.test(logic.compact(name(e))));
      if(!dimensions)throw new Error('未找到视频尺寸按钮。');
      if(logic.compact(name(dimensions))!==logic.compact(spec.ratio+spec.resolution)) {
        dimensions.click();
        for(const value of [spec.ratio,spec.resolution]) {
          const choice=await until(()=>visibleAll('label.lv-radio').find(e=>logic.compact(e.querySelector('input')?.value)===logic.compact(value)),'未找到视频尺寸选项。');choice.click();
        }
        const close=buttons(inspect().composer).find(e=>logic.compact(name(e))===logic.compact(spec.ratio+spec.resolution));if(close)close.click();
      }
      const duration=buttons(inspect().composer).find(e=>/^\d+s$/.test(name(e).trim()));
      if(!duration)throw new Error('未找到视频时长按钮。');
      if(name(duration).trim()!==spec.duration+'s') {
        duration.click();const input=await until(()=>visibleAll('input[role="spinbutton"]').find(e=>e.getAttribute('aria-valuemin')==='4'),'未找到时长输入框。');
        input.focus();input.select();if(!document.execCommand('insertText',false,String(spec.duration)))throw new Error('时长输入失败。');
        input.dispatchEvent(new Event('change',{bubbles:true}));
        input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));
        input.dispatchEvent(new KeyboardEvent('keyup',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));
        await until(()=>buttons(inspect().composer).find(e=>name(e).trim()===spec.duration+'s'),'时长确认未生效。');
        input.blur();
        const close=buttons(inspect().composer).find(e=>/^\d+s$/.test(name(e).trim()));
        if(!close)throw new Error('未找到时长弹层关闭按钮。');
        close.click();
        await until(()=>buttons(inspect().composer).find(e=>name(e).trim()===spec.duration+'s'),'时长修改未生效。');
      }
    }
    const checked=inspect(spec);if(!checked.ready)throw new Error(checked.reason);return checked;
  }

  async function telemetry(jobId,stage,detail={},extra={}) {
    const reply=await chrome.runtime.sendMessage({type:'JOB_TELEMETRY',job_id:jobId,stage,observed_at:Date.now()/1000,detail,...extra});
    if(reply?.ok===false)throw new Error(reply.error||'任务状态回报失败。');
  }
  async function newConversation(jobId) {
    const candidates=buttons(document).filter(e=>name(e).trim()==='New session');
    if(candidates.length!==1)throw new Error('未找到唯一 New session 按钮，未提交。');
    candidates[0].click();
    await until(()=>{const view=inspect();return view.ready&&!view.editor.textContent.trim()&&document.querySelectorAll('[data-generated-record-key]').length===0;},'无法确认新的空白会话，未提交。');
    const workspace=new URL(location.href).searchParams.get('workspace');
    state.conversationId=workspace||null;
    await telemetry(jobId,'new_session',{empty_editor:true,existing_records:0},{conversation_id:workspace||undefined,session_path:location.pathname});
  }
  async function uploadReferences(jobId,spec) {
    const refs=spec.references||[];
    if(!refs.length)return;
    await telemetry(jobId,'loading_references',{count:refs.length,kinds:refs.map(r=>r.kind)});
    const files=[];
    for(const meta of refs) {
      const chunks=[];let offset=0;
      while(offset<meta.size_bytes) {
        const reply=await chrome.runtime.sendMessage({type:'ASSET_CHUNK',job_id:jobId,asset_id:meta.asset_id,offset});
        if(!reply?.ok||typeof reply.data!=='string')throw new Error(reply?.error||'参考素材读取失败。');
        const decoded=atob(reply.data);const chunk=Uint8Array.from(decoded,c=>c.charCodeAt(0));
        if(!chunk.length)throw new Error('参考素材数据不完整。');chunks.push(chunk);offset+=chunk.length;
      }
      if(offset!==meta.size_bytes)throw new Error('参考素材大小不一致。');
      files.push(new File(chunks,meta.name,{type:meta.mime_type,lastModified:Date.now()}));
    }
    const candidates=[...document.querySelectorAll('input[type="file"]')].filter(e=>!e.disabled&&(!e.accept||refs.every(r=>r.kind==='video'?/video|mp4|mov/i.test(e.accept):/image|png|jpg|jpeg/i.test(e.accept))));
    let input=candidates.find(e=>e.multiple)|| (files.length===1&&candidates.length===1?candidates[0]:null);
    if(!input) {
      const view=inspect(spec);const button=buttons(view.composer).find(e=>/^(Reference|Upload references)$/.test(name(e).trim()));
      if(button)button.click();
      input=await until(()=>[...document.querySelectorAll('input[type="file"]')].find(e=>!e.disabled&&e.multiple&&(!e.accept||refs.every(r=>r.kind==='video'?/video|mp4|mov/i.test(e.accept):/image|png|jpg|jpeg/i.test(e.accept)))),'未找到支持全部素材的多文件上传控件。');
    }
    const transfer=new DataTransfer();files.forEach(f=>transfer.items.add(f));input.files=transfer.files;
    input.dispatchEvent(new Event('change',{bubbles:true}));
    await telemetry(jobId,'uploading_references',{count:refs.length});
    const deadline=Date.now()+180000;let stable=0;
    while(Date.now()<deadline) {
      const view=inspect(spec);const scope=view.composer;
      if(scope) {
        const text=scope.innerText;
        if(/upload failed|unsupported file|try again|上传失败/i.test(text))throw new Error('页面报告参考素材上传失败。');
        const count=[...scope.querySelectorAll('img:not(.ProseMirror-separator),video')].filter(visible).length;
        const busy=scope.querySelector('[role="progressbar"]')||/uploading|processing|正在上传/i.test(text);
        if(count>=files.length&&!busy) {if(!stable)stable=Date.now();if(Date.now()-stable>2500)break;}else stable=0;
      }
      await new Promise(r=>setTimeout(r,500));
    }
    if(!stable||Date.now()-stable<2500)throw new Error('参考素材未确认上传完成，未提交。');
    await telemetry(jobId,'references_ready',{count:refs.length,assets:refs.map(r=>({asset_id:r.asset_id,kind:r.kind,name:r.name,sha256:r.sha256}))});
  }
  function taskProgress(submitId) {
    if(!/^[0-9a-f-]{36}$/i.test(submitId||''))return null;
    const card=[...document.querySelectorAll('[data-generated-record-key]')].find(e=>e.getAttribute('data-generated-record-key')===submitId);
    if(!card)return null;
    const found=card.innerText.match(/(?:^|\s)(\d{1,2})%/);
    return found?{progress:Number(found[1]),progress_source:'page',stage:'generating',observed_at:Date.now()/1000}:null;
  }

  function publicState() {
    const found = inspect();
    const humanBusy = Date.now() - state.lastHumanActivity < 3000;
    return {ready: found.ready && !state.activeJobId && !humanBusy, reason: state.activeJobId ? '已领取任务，等待提交结果。' : humanBusy ? '用户正在操作页面，稍后再领取。' : found.reason,
      activeJobId: state.activeJobId, clicked: state.clicked, lastResult: state.lastResult,
      page_url: location.origin + location.pathname};
  }
  function cards() {
    return [...document.querySelectorAll('[data-generated-record-key]')].map(e => {
      const promptNodes = [...e.querySelectorAll('span[class*="prompt-value-container"]')];
      const spans = [...e.querySelectorAll('span')];
      return {id: e.getAttribute('data-generated-record-key'), history_id:e.getAttribute('data-generated-record-handoff-key'), text: e.innerText || '',
        prompts: (promptNodes.length ? promptNodes : spans).map(s => s.textContent?.trim() || ''),
        labels: spans.filter(s => !promptNodes.some(p => p === s || p.contains(s) || s.contains(p))).map(s => s.textContent?.trim() || ''),
      };
    });
  }
  function observeGeneration(spec) {
    const channel = 'dreamina-local-bridge-v3';
    const nonce = crypto.randomUUID();
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    let readyResolve, readyReject, resultResolve, readyTimer, resultTimer, closed = false;
    const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
    const result = new Promise(resolve => { resultResolve = resolve; });
    const post = type => window.postMessage({channel, direction: 'to-observer', type, nonce, ...(type === 'arm' ? {spec} : {})}, location.origin);
    const onMessage = event => {
      if (event.source !== window || event.origin !== location.origin) return;
      const message = event.data;
      if (!message || message.channel !== channel || message.direction !== 'from-observer' || message.nonce !== nonce) return;
      if (message.type === 'armed') {
        clearTimeout(readyTimer);
        if (message.ok === true) readyResolve();
        else readyReject(new Error('生成响应观察器未就绪，请刷新网页并重新加载扩展；没有提交。'));
        return;
      }
      if (message.type !== 'result') return;
      void chrome.runtime.sendMessage({type:'JOB_TELEMETRY',job_id:state.activeJobId,stage:'generation_response',observed_at:Date.now()/1000,conversation_id:message.conversation_id||undefined,detail:{http_status:message.http_status,ret:message.ret,request_submit_id:message.request_submit_id,history_record_id:message.history_record_id,request:message.request_summary}}).catch(()=>{});
      const ret = typeof message.ret === 'string' && /^-?\d{1,12}$/.test(message.ret) ? message.ret : null;
      const requestId = typeof message.request_submit_id === 'string' && uuid.test(message.request_submit_id) ? message.request_submit_id : null;
      const responseId = typeof message.response_submit_id === 'string' && uuid.test(message.response_submit_id) ? message.response_submit_id : null;
      const historyId = typeof message.history_record_id === 'string' && /^[1-9]\d{0,39}$/.test(message.history_record_id) ? message.history_record_id : null;
      const kind = spec.kind === 'image' ? 'image' : 'video';
      if (message.kind !== kind) return;
      clearTimeout(resultTimer);
      // The page can spoof messages; these are observations, and the backend must verify the ID.
      if (requestId && ret !== null && ret !== '0') resultResolve({status: 'failed', error: `生成接口拒绝：ret=${ret}。不会自动重发。`});
      else if (message.outcome === 'submitted' && ret === '0' && requestId && Number.isInteger(message.http_status) && message.http_status >= 200 && message.http_status < 300 && (!responseId || responseId.toLowerCase() === requestId.toLowerCase()) && (responseId || historyId)) {
        const origins=['https://dreamina-api.us.capcut.com','https://dreamina-api-us-ttp2.us.capcut.com','https://mweb-api-sg.capcut.com'];
        resultResolve({status: 'submitted', submit_id: requestId, ...(origins.includes(message.api_origin)?{api_origin:message.api_origin}:{})});
      } else resultResolve({status: 'uncertain', error: '已点击，但生成接口未返回一致的任务确认；请查询原记录，不会自动重发。'});
    };
    window.addEventListener('message', onMessage);
    readyTimer = setTimeout(() => readyReject(new Error('生成响应观察器握手超时；没有提交。')), 3000);
    resultTimer = setTimeout(() => resultResolve({status: 'uncertain', error: '点击后未收到匹配的生成接口响应；不会自动重试。'}), 90000);
    post('arm');
    return {ready, result, close() {
      if (closed) return; closed = true;
      clearTimeout(readyTimer); clearTimeout(resultTimer); window.removeEventListener('message', onMessage); post('disarm');
    }};
  }
  async function finish(result) {
    state.lastResult = result;
    state.activeJobId = null;
    try { await chrome.runtime.sendMessage({type: 'JOB_RESULT', ...result}); } catch { /* Background recovery reads lastResult; no second click. */ }
  }
  async function run(job) {
    let spec;
    try { spec = logic.validateSpec(job.request); }
    catch (e) { return finish({job_id: job.id, status: 'failed', error: e.message}); }
    let interactionAt = state.lastHumanActivity;
    let networkObservation = null;
    try {
      await newConversation(job.id);
      await configure(spec);
      await uploadReferences(job.id,spec);
      const observation=inspect(spec);
      if(!observation.ready)throw new Error(observation.reason);
      interactionAt=state.lastHumanActivity;
      const editor = observation.editor;
      editor.focus();
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(editor);
      selection.removeAllRanges(); selection.addRange(range);
      if (!document.execCommand('insertText', false, spec.prompt)) throw new Error('编辑器拒绝文本插入；没有提交。');
      await until(()=>{const view=inspect(spec);return view.ready&&view.editor.textContent.trim()===spec.prompt&&!view.button.disabled&&view.button.getAttribute('aria-disabled')!=='true';},'提示词或生成按钮尚未就绪。');
      const fresh = inspect(spec);
      if (!fresh.ready || fresh.editor.textContent.trim() !== spec.prompt || state.lastHumanActivity !== interactionAt) throw new Error('输入或页面状态发生变化；没有提交。');
      if (fresh.button.disabled || fresh.button.getAttribute('aria-disabled') === 'true') throw new Error('Generate 按钮仍不可用；没有提交。');
      networkObservation = observeGeneration(spec);
      await networkObservation.ready;
      // Persist the intent in the extension worker BEFORE the sole click.
      const ack = await chrome.runtime.sendMessage({type: 'JOB_CLICKING', job_id: job.id});
      if (!ack?.ok) throw new Error('桥接未确认持久化任务状态；没有提交。');
      const final = inspect(spec);
      if (!final.ready || final.editor.textContent.trim() !== spec.prompt || state.lastHumanActivity !== interactionAt || final.button.disabled || final.button.getAttribute('aria-disabled') === 'true') throw new Error('点击前页面或人工操作发生变化；没有提交。');
      state.clicked = true;
      final.button.click();
      const confirmed = await networkObservation.result;
      await finish({job_id: job.id, ...confirmed});
    } catch (error) {
      await finish({job_id: job.id, status: state.clicked ? 'uncertain' : 'failed', error: String(error.message || error).slice(0, 300)});
    } finally {
      networkObservation?.close();
    }
  }
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id) return;
    if(message?.type==='POLL_PROGRESS'){respond(taskProgress(message.submit_id));return;}
    if (message?.type === 'INSPECT') { respond(publicState()); return; }
    if (message?.type !== 'RUN') return;
    const job = message.job;
    if (!job || typeof job.id !== 'string' || state.activeJobId || seenJobs.has(job.id)) { respond({accepted: false, reason: '任务已处理或标签页忙，拒绝再次执行。'}); return; }
    seenJobs.add(job.id); state.activeJobId = job.id; state.clicked = false; state.lastResult = null;
    respond({accepted: true});
    void run(job);
  });
})();
