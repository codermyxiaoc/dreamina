/* MAIN-world passive observer. It never sends/replays a request or reads headers/Cookies. */
(() => {
  const CHANNEL = 'dreamina-local-bridge-v3';
  const ORIGIN = 'https://dreamina.capcut.com';
  const PATH = '/mweb/v1/aigc_draft/generate';
  const HOSTS = new Set(['dreamina-api.us.capcut.com', 'dreamina-api-us-ttp2.us.capcut.com', 'mweb-api-sg.capcut.com']);
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const LIMIT = 2 * 1024 * 1024;
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  function target(url, method = 'GET') {
    if (typeof method !== 'string' || method.toUpperCase() !== 'POST') return false;
    try {
      const parsed = new URL(url, ORIGIN);
      return parsed.protocol === 'https:' && !parsed.username && !parsed.password && HOSTS.has(parsed.hostname) && parsed.pathname === PATH;
    } catch { return false; }
  }
  const MODEL_KEYS={"seedance-2.5":"dreamina_seedance_45_pro","seedance-2.0-mini":"dreamina_seedance_40_mini","seedance-2.0-fast":"dreamina_seedance_40","seedance-2.0":"dreamina_seedance_40_pro"};
  function expected(spec) {
    if(!object(spec)||typeof spec.prompt!=='string'||!spec.prompt||spec.prompt.length>15000)return null;
    if(spec.kind==='image'&&spec.model==='seedream-5.0-lite'&&spec.n===1&&spec.size==='2048x2048')return {kind:'image',model:'high_aes_general_v50',prompt:spec.prompt};
    if(!spec.kind&&MODEL_KEYS[spec.model]&&Number.isInteger(spec.duration)&&['480p','720p','1080p'].includes(String(spec.resolution).toLowerCase()))return {kind:'video',model:MODEL_KEYS[spec.model],prompt:spec.prompt,duration_ms:spec.duration*1000,resolution:String(spec.resolution).toLowerCase(),ratio:spec.ratio,references:(spec.references||[]).map(r=>r.kind)};
    return null;
  }
  function parseRequest(text, rule) {
    if (!rule || typeof text !== 'string' || text.length > LIMIT) return null;
    try {
      const body = JSON.parse(text);
      if (!object(body) || !UUID.test(body.submit_id || '')) return null;
      const draft = typeof body.draft_content === 'string' ? JSON.parse(body.draft_content) : body.draft_content;
      if (!object(draft) || !Array.isArray(draft.component_list) || draft.component_list.length !== 1) return null;
      const component = draft.component_list[0];
      if (body.extend?.root_model && body.extend.root_model !== rule.model) return null;
      if (rule.kind === 'video') {
        const params = component?.abilities?.gen_video?.text_to_video_params;
        if (component?.type !== 'video_base_component' || !object(params) || params.model_req_key !== rule.model || params.video_aspect_ratio !== rule.ratio || !Array.isArray(params.video_gen_inputs) || params.video_gen_inputs.length !== 1) return null;
        const input = params.video_gen_inputs[0];
        const text=(input.unified_edit_input?.meta_list||[]).filter(m=>m.meta_type==='text').map(m=>m.text||'').join('').trim();
        if ((input.prompt !== rule.prompt && text!==rule.prompt) || input.duration_ms !== rule.duration_ms || String(input.resolution).toLowerCase() !== rule.resolution || input.first_frame_image || input.end_frame_image) return null;
        const materials=input.unified_edit_input?.material_list||[];
        const expectedKinds=rule.references||[];
        if(materials.length!==expectedKinds.length)return null;
        for(const kind of ['image','video'])if(materials.filter(m=>m.material_type===kind).length!==expectedKinds.filter(k=>k===kind).length)return null;
      } else {
        const core = component?.abilities?.generate?.core_param;
        const size = core?.large_image_info;
        if (component?.type !== 'image_base_component' || !object(core) || core.model !== rule.model || core.prompt !== rule.prompt || size?.width !== 2048 || size?.height !== 2048 || String(size?.resolution_type).toLowerCase() !== '2k' || component?.abilities?.gen_option?.gen_count !== 1) return null;
      }
      return {kind:rule.kind,request_submit_id:body.submit_id,conversation_id:body.extend?.workspace_id!=null?String(body.extend.workspace_id):null,request_summary:{model:rule.model,prompt:rule.prompt,duration_ms:rule.duration_ms,resolution:rule.resolution,ratio:rule.ratio,reference_kinds:rule.references||[]}};
    } catch { return null; }
  }
  function classifyResponse(request, httpStatus, value) {
    const base = {kind: request.kind, request_submit_id: request.request_submit_id, http_status: Number.isInteger(httpStatus) ? httpStatus : 0};
    if(request.api_origin)base.api_origin=request.api_origin;
    if(request.conversation_id)base.conversation_id=request.conversation_id;
    base.request_summary=request.request_summary;
    if (!object(value)) return {...base, outcome: 'uncertain', reason: 'invalid_response'};
    const ret = typeof value.ret === 'number' && Number.isInteger(value.ret) ? String(value.ret) : typeof value.ret === 'string' && /^-?\d{1,12}$/.test(value.ret) ? value.ret : null;
    if (ret === null) return {...base, outcome: 'uncertain', reason: 'missing_ret'};
    base.ret = ret;
    if (ret !== '0') return {...base, outcome: 'failed', reason: 'business_rejected'};
    const data = value.data?.aigc_data;
    if (httpStatus < 200 || httpStatus >= 300 || !object(data)) return {...base, outcome: 'uncertain', reason: 'missing_confirmation'};
    const responseId = typeof data.submit_id === 'string' && UUID.test(data.submit_id) ? data.submit_id : null;
    const historyId = typeof data.history_record_id === 'string' && /^[1-9]\d{0,39}$/.test(data.history_record_id) ? data.history_record_id : Number.isSafeInteger(data.history_record_id) && data.history_record_id > 0 ? String(data.history_record_id) : null;
    if (responseId) base.response_submit_id = responseId;
    if (historyId) base.history_record_id = historyId;
    if (responseId && responseId.toLowerCase() !== request.request_submit_id.toLowerCase()) return {...base, outcome: 'uncertain', reason: 'submit_id_mismatch'};
    if (!responseId && !historyId) return {...base, outcome: 'uncertain', reason: 'missing_confirmation'};
    return {...base, outcome: 'submitted', reason: 'confirmed_response'};
  }
  if (typeof window === 'undefined') {
    if (typeof module !== 'undefined' && module.exports) module.exports = {CHANNEL, target, expected, parseRequest, classifyResponse};
    return;
  }
  if (window.location.origin !== ORIGIN) return false;
  if (window.__dreaminaBridgeObserverV3) return window.__dreaminaBridgeObserverV3.version === '0.4.0';
  // Do not pin a guard before the official SDK finishes installing its hooks.
  // A later background inspection can safely retry this no-op installation.
  if (window.document.readyState !== 'complete' || !window.byted_acrawler?.isWebmssdk) return false;

  let armed = null;
  const xhrMeta = new WeakMap();
  const originalFetch = window.fetch;
  const originalOpen = window.XMLHttpRequest.prototype.open;
  const originalSend = window.XMLHttpRequest.prototype.send;
  function emit(type, nonce, fields = {}) {
    window.postMessage({channel: CHANNEL, direction: 'from-observer', type, nonce, ...fields}, ORIGIN);
  }
  function current(ref) { return ref && armed === ref && !ref.finished && Date.now() <= ref.deadline; }
  function complete(ref, result) {
    if (!current(ref)) return;
    ref.finished = true;
    emit('result', ref.nonce, result);
  }
  function register(ref, text, url) {
    if (!current(ref)) return null;
    const found = parseRequest(text, ref.rule);
    if (!found) return null;
    try {const origin=new URL(url,ORIGIN).origin;if(HOSTS.has(new URL(origin).hostname))found.api_origin=origin;}catch{}
    if (ref.requestId && ref.requestId !== found.request_submit_id) {
      complete(ref, {outcome: 'uncertain', reason: 'multiple_matching_requests', kind: ref.rule.kind});
      return null;
    }
    ref.requestId = found.request_submit_id;
    return found;
  }
  function transportError(ref, request) {
    complete(ref, {...request, outcome: 'uncertain', reason: 'transport_error'});
  }
  function jsonResponse(ref, request, status, text) {
    let value = null;
    try { if (typeof text === 'string' && text.length <= LIMIT) value = JSON.parse(text); } catch { /* malformed stays uncertain */ }
    complete(ref, classifyResponse(request, status, value));
  }
  function wrappedFetch(...args) {
    const ref = armed;
    let bodyRead = null;
    let observedUrl = null;
    try {
      const [input, options] = args;
      const isRequest = typeof Request !== 'undefined' && input instanceof Request;
      const url = typeof input === 'string' || input instanceof URL ? String(input) : isRequest ? input.url : null;
      const method = options?.method || (isRequest ? input.method : 'GET');
      if (current(ref) && target(url, method)) {
        observedUrl=url;
        if (typeof options?.body === 'string') bodyRead = Promise.resolve(options.body);
        else if (isRequest && options?.body === undefined) bodyRead = input.clone().text();
      }
    } catch { /* Observation must not change the original fetch call. */ }
    let returned;
    try { returned = Reflect.apply(originalFetch, this, args); }
    catch (error) {
      if (bodyRead) void bodyRead.then(text => { const req = register(ref, text, observedUrl); if (req) transportError(ref, req); }).catch(() => {});
      throw error;
    }
    if (bodyRead) {
      // Register this reaction before returning the original Promise: clone the
      // response before a caller can consume its body, without replacing it.
      const responseRead = Promise.resolve(returned).then(response => {
        const clone = response.clone();
        return clone.text().then(text => ({status: response.status, text}));
      }).catch(() => ({error: true}));
      const requestRead = bodyRead.then(text => register(ref, text, observedUrl)).catch(() => null);
      void Promise.all([requestRead, responseRead]).then(([req, response]) => {
        if (!req) return;
        if (response.error) transportError(ref, req);
        else jsonResponse(ref, req, response.status, response.text);
      }).catch(() => {});
    }
    return returned;
  }
  function wrappedOpen(...args) {
    const returned = Reflect.apply(originalOpen, this, args);
    xhrMeta.set(this, {method: args[0], url: args[1]});
    return returned;
  }
  function wrappedSend(...args) {
    const ref = armed;
    const meta = xhrMeta.get(this);
    const req = current(ref) && meta && target(meta.url, meta.method) ? register(ref, args[0], meta.url) : null;
    let listener = null;
    if (req) {
      listener = () => {
        try {
          if (this.status === 0) { transportError(ref, req); return; }
          if (this.responseType === 'json') complete(ref, classifyResponse(req, this.status, this.response));
          else jsonResponse(ref, req, this.status, this.responseText);
        } catch { transportError(ref, req); }
      };
      this.addEventListener('loadend', listener, {once: true});
    }
    try { return Reflect.apply(originalSend, this, args); }
    catch (error) {
      if (listener) this.removeEventListener('loadend', listener);
      if (req) transportError(ref, req);
      throw error;
    }
  }
  if (typeof originalFetch === 'function') window.fetch = wrappedFetch;
  window.XMLHttpRequest.prototype.open = wrappedOpen;
  window.XMLHttpRequest.prototype.send = wrappedSend;
  window.addEventListener('message', event => {
    if (event.source !== window || event.origin !== ORIGIN) return;
    const message = event.data;
    if (!object(message) || message.channel !== CHANNEL || message.direction !== 'to-observer' || !UUID.test(message.nonce || '')) return;
    if (message.type === 'disarm') { if (armed?.nonce === message.nonce) armed = null; return; }
    if (message.type !== 'arm') return;
    const rule = expected(message.spec);
    if (!rule || (armed && !armed.finished && Date.now() < armed.deadline && armed.nonce !== message.nonce)) {
      emit('armed', message.nonce, {ok: false, reason: 'invalid_or_busy'}); return;
    }
    if (window.fetch !== wrappedFetch || window.XMLHttpRequest.prototype.open !== wrappedOpen || window.XMLHttpRequest.prototype.send !== wrappedSend) {
      emit('armed', message.nonce, {ok: false, reason: 'observer_replaced'}); return;
    }
    armed = {nonce: message.nonce, rule, deadline: Date.now() + 95000, requestId: null, finished: false};
    emit('armed', message.nonce, {ok: true});
  });
  window.__dreaminaBridgeObserverV3 = Object.freeze({version: '0.4.0'});
  return true;
})();
