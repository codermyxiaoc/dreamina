/* Pure protocol/selection rules, also exercised by the Node test suite. */
(() => {
  const MODEL = 'seedance-2.0-mini';
  const MODEL_NAME = 'Dreamina Seedance 2.0 Mini';
  const MODELS = {"seedance-2.5":{"name":"Dreamina Seedance 2.5","upstream":"dreamina_seedance_45_pro","min":4,"max":30,"prompt_limit":15000,"resolutions":["480p","720p","1080p"],"ratios":["21:9","16:9","4:3","1:1","3:4","9:16"],"max_images":30,"max_videos":10,"max_total":50},"seedance-2.0-mini":{"name":"Dreamina Seedance 2.0 Mini","upstream":"dreamina_seedance_40_mini","min":4,"max":15,"prompt_limit":4000,"resolutions":["720p","1080p"],"ratios":["21:9","16:9","4:3","1:1","3:4","9:16"],"max_images":9,"max_videos":3,"max_total":12},"seedance-2.0-fast":{"name":"Dreamina Seedance 2.0 Fast","upstream":"dreamina_seedance_40","min":4,"max":15,"prompt_limit":4000,"resolutions":["720p","1080p"],"ratios":["21:9","16:9","4:3","1:1","3:4","9:16"],"max_images":9,"max_videos":3,"max_total":12},"seedance-2.0":{"name":"Dreamina Seedance 2.0","upstream":"dreamina_seedance_40_pro","min":4,"max":15,"prompt_limit":4000,"resolutions":["720p","1080p"],"ratios":["21:9","16:9","4:3","1:1","3:4","9:16"],"max_images":9,"max_videos":3,"max_total":12}};
  const IMAGE_MODEL = 'seedream-5.0-lite';
  const IMAGE_MODEL_NAME = 'Seedream 5.0 Lite';
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const compact = value => String(value ?? '').replace(/\s+/g, '').toLowerCase();
  function validateSpec(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('生成参数必须是对象。');
    if (input.kind === 'image' || input.model === IMAGE_MODEL) {
      const allowed = new Set(['kind','model','prompt','n','size','ratio','resolution','new_session']);
      const spec = {kind:'image',model:IMAGE_MODEL,n:1,size:'2048x2048',ratio:'1:1',resolution:'2K',...input};
      if (Object.keys(input).some(k=>!allowed.has(k)) || spec.kind !== 'image' || spec.model !== IMAGE_MODEL || spec.n !== 1 || spec.size !== '2048x2048' || spec.ratio !== '1:1' || compact(spec.resolution) !== '2k') throw new Error('图片桥接仅支持 Seedream 5.0 Lite / 2048×2048 / 1张。');
      if (typeof spec.prompt !== 'string' || !spec.prompt.trim() || spec.prompt.length > 2000) throw new Error('图片提示词需要 1–2000 字符。');
      return {...spec,prompt:spec.prompt.trim(),resolution:'2K'};
    }
    const allowed=new Set(['model','prompt','duration','resolution','ratio','mode','references','asset_owner_task_id','new_session']);
    if(Object.keys(input).some(k=>!allowed.has(k)))throw new Error('不支持的生成字段。');
    const spec={model:MODEL,duration:4,resolution:'720p',ratio:'16:9',mode:'omni',references:[],...input,new_session:true};
    const model=MODELS[spec.model];
    if(!model||!Number.isInteger(spec.duration)||spec.duration<model.min||spec.duration>model.max||!model.resolutions.includes(spec.resolution.toLowerCase())||!model.ratios.includes(spec.ratio)||spec.mode!=='omni')throw new Error('模型、时长、比例或原生分辨率无效。');
    if(typeof spec.prompt!=='string'||!spec.prompt.trim()||spec.prompt.length>model.prompt_limit)throw new Error('提示词长度超出模型限制。');
    if(!Array.isArray(spec.references)||spec.references.length>model.max_total)throw new Error('参考素材数量超限。');
    for(const kind of ['image','video'])if(spec.references.filter(r=>r.kind===kind).length>model[kind==='image'?'max_images':'max_videos'])throw new Error('参考素材数量超出模型限制。');
    for(const ref of spec.references)if(!['image','video'].includes(ref.kind)||!/^asset_\d{1,2}_[a-f0-9]{24}$/.test(ref.asset_id)||!Number.isInteger(ref.size_bytes)||ref.size_bytes<=0||ref.size_bytes>50*1024*1024||!/^[a-f0-9]{64}$/.test(ref.sha256))throw new Error('参考素材元数据无效。');
    return {...spec,prompt:spec.prompt.trim(),resolution:spec.resolution.toLowerCase()};
  }

  function selectNewRecord(cards, before, spec, options = {}) {
    const old = new Set(before);
    const matches = cards.filter(card => {
      const labels = new Set((card.labels || card.prompts).filter(p => p !== spec.prompt).map(compact));
      if(options.requireHistoryId && !/^\d{5,}$/.test(card.history_id || '')) return false;
      const expected = spec.kind === 'image' ? [IMAGE_MODEL_NAME,'1:1','2K'] : [MODELS[spec.model]?.name||MODEL_NAME,spec.ratio,spec.resolution,spec.duration+'s'];
      return uuid.test(card.id) && !old.has(card.id) && card.prompts.some(p => p === spec.prompt) && expected.every(x=>labels.has(compact(x)));
    });
    const unique = new Map(matches.map(card => [card.id, card]));
    if (unique.size > 1) throw new Error('检测到多个相同提示词的新任务，无法安全确定归属。');
    return unique.size ? [...unique.values()][0] : null;
  }
  function recoveryAction(inflight, content, now) {
    if (!inflight) return {action: 'none'};
    if (inflight.phase === 'uncertain') {
      if (content?.lastResult?.job_id === inflight.id && content.lastResult.status === 'submitted') return {action: 'report', result: content.lastResult};
      return {action: 'hold'}; // Preserve the original claim for a late task ID; never redispatch.
    }
    if (content?.lastResult?.job_id === inflight.id) return {action: 'report', result: content.lastResult};
    if (content?.activeJobId === inflight.id && now < inflight.deadline) return {action: 'wait'};
    return {action: 'uncertain'}; // Never dispatch/run again after a persisted claim.
  }
  function validResult(result) {
    if (!result || !['submitted', 'failed', 'uncertain'].includes(result.status)) return false;
    return result.status !== 'submitted' || uuid.test(result.submit_id || '');
  }
  const api = Object.freeze({MODEL, MODEL_NAME, MODELS, IMAGE_MODEL, IMAGE_MODEL_NAME, compact, validateSpec, selectNewRecord, recoveryAction, validResult});
  globalThis.DreaminaBridgeLogic = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
