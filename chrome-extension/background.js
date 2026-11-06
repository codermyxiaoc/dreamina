import './logic.js';

const logic = globalThis.DreaminaBridgeLogic;
const BASE = 'http://127.0.0.1:8787';
const PREFIX = '/v1/chrome';
const ALARM = 'dreamina-bridge-poll';
let ticking = false;
const assetCache=new Map();
let operations = Promise.resolve();
const start = initialize();

function exclusive(task) {
  const current = operations.then(task, task);
  operations = current.catch(() => {});
  return current;
}
function tick() { return exclusive(tickInternal); }

async function initialize() {
  await chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
  if (!(await chrome.alarms.get(ALARM))) await chrome.alarms.create(ALARM, {periodInMinutes: 0.5});
}
async function load() { return (await chrome.storage.local.get('bridge')).bridge || {enabled: false}; }
async function save(value) { await chrome.storage.local.set({bridge: value}); }
async function api(config, path, data) {
  const response = await fetch(BASE + PREFIX + path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Chrome-Bridge-Key': config.key},
    body: JSON.stringify(data), credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(10000)});
  if (!response.ok) throw new Error(`本地桥接接口 HTTP ${response.status}；检查服务和扩展 ID 配置。`);
  return response.json();
}
async function inspectTab(tabId, inject = true) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab.url || new URL(tab.url).origin !== 'https://dreamina.capcut.com') throw new Error('绑定标签页已离开 Dreamina。');
  let observerReady = true;
  let observerReason = '';
  if (inject) {
    const observerState = await chrome.scripting.executeScript({target: {tabId}, world: 'MAIN', files: ['observer.js']});
    observerReady = observerState?.[0]?.result === true;
    if(!observerReady){
      const checks=await chrome.scripting.executeScript({target:{tabId},world:'MAIN',func:()=>({loaded:document.readyState==='complete',sdk:!!window.byted_acrawler?.isWebmssdk,observer:window.__dreaminaBridgeObserverV3?.version||null})});
      const check=checks?.[0]?.result||{};
      observerReason=check.observer&&check.observer!=='0.4.0'?'网页仍有旧版观察器，请刷新 Dreamina 标签页。':!check.loaded?'等待网页加载完成。':!check.sdk?'等待官方 SDK 加载，请确认已登录并能正常使用生成页面。':'观察器尚未就绪，请刷新页面。';
    }
    await chrome.scripting.executeScript({target: {tabId}, world: 'ISOLATED', files: ['logic.js', 'content.js']});
  }
  const state = await chrome.tabs.sendMessage(tabId, {type: 'INSPECT'});
  return observerReady ? state : {...state, ready: false, reason: observerReason};
}
async function report(config, result) {
  if (!config.inflight || result.job_id !== config.inflight.id || !logic.validResult(result)) throw new Error('任务结果不匹配，已停止。');
  const body = {worker_id: config.worker_id, claim_token: config.inflight.claim_token, status: result.status};
  if (result.submit_id) body.submit_id = result.submit_id;
  if (result.api_origin) body.api_origin = result.api_origin;
  if (result.error) body.error = result.error.slice(0, 300);
  config.outbox = {job_id: config.inflight.id, body};
  config.status = '正在回报任务结果。';
  await save(config);
  await flush(config);
}
async function flush(config) {
  if (!config.outbox) return;
  const remoteState=await api(config,`/jobs/${config.outbox.job_id}/status`,{worker_id:config.worker_id,claim_token:config.outbox.body.claim_token});
  if(['completed','failed'].includes(remoteState.status)){
    config.last_job={id:config.outbox.job_id,status:remoteState.status,submit_id:remoteState.submit_id||null};
    config.outbox=null;config.inflight=null;config.status='旧任务已由后台结束，待回报锁已清理。';await save(config);return;
  }
  await api(config, `/jobs/${encodeURIComponent(config.outbox.job_id)}/report`, config.outbox.body);
  const status = config.outbox.body.status;
  config.last_job = {id: config.outbox.job_id, status, submit_id: config.outbox.body.submit_id || null};
  if (status === 'uncertain') {
    config.enabled = false;
    config.inflight.phase = 'uncertain'; // Keep claim_token for a late submitted report.
  } else {
    if(status==='submitted'&&config.inflight.tab_id){config.monitors=config.monitors||[];config.monitors.push({...config.inflight,submit_id:config.outbox.body.submit_id,last_progress:null});}
    for(const key of assetCache.keys())if(key.startsWith(config.inflight.id+':'))assetCache.delete(key);
    config.inflight=null;
  }
  config.outbox = null;
  config.status = status === 'submitted' ? '生成接口已返回任务确认，等待 API 查询结果。' : status === 'uncertain' ? '提交结果不确定，已暂停，禁止自动重试。' : '任务在提交前失败或被接口拒绝。';
  await save(config);
}

async function readyRunTab(config,tabId) {
  const deadline=Date.now()+75000;let lastHeartbeat=0,lastReason='等待页面';
  while(Date.now()<deadline) {
    if(Date.now()-lastHeartbeat>15000){await api(config,'/heartbeat',{account_id:config.account_id,worker_id:config.worker_id,protocol_version:4,page_url:'https://dreamina.capcut.com/ai-tool/generate',ready:false});lastHeartbeat=Date.now();}
    try{const state=await inspectTab(tabId);lastReason=state.reason;if(state.ready)return;}catch(e){lastReason=String(e.message||e).slice(0,120);}
    if(Date.now()-lastHeartbeat<2000)await api(config,`/jobs/${config.inflight.id}/telemetry`,{worker_id:config.worker_id,claim_token:config.inflight.claim_token,stage:'preparing_session',observed_at:Date.now()/1000,detail:{reason:lastReason,clicked:false}});
    await new Promise(r=>setTimeout(r,1000));
  }
  throw new Error('新任务标签页未就绪：'+lastReason+'；未提交。');
}
async function assetChunk(config,message) {
  const item=config.inflight.references.find(r=>r.asset_id===message.asset_id);
  if(!item||!Number.isInteger(message.offset)||message.offset<0||message.offset>=item.size_bytes)throw new Error('非法素材读取。');
  const key=config.inflight.id+':'+item.asset_id;
  if(!assetCache.has(key)){
    const response=await fetch(BASE+PREFIX+`/jobs/${config.inflight.id}/assets/${item.asset_id}`,{credentials:'omit',cache:'no-store',headers:{'X-Chrome-Bridge-Key':config.key,'X-Chrome-Claim-Token':config.inflight.claim_token,'X-Chrome-Worker-Id':config.worker_id},signal:AbortSignal.timeout(60000)});
    if(!response.ok)throw new Error('任务素材不可读取。');
    const bytes=new Uint8Array(await response.arrayBuffer());
    const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
    if(bytes.length!==item.size_bytes||sha!==item.sha256)throw new Error('任务素材完整性校验失败。');
    assetCache.set(key,bytes);
  }
  const part=assetCache.get(key).subarray(message.offset,message.offset+256*1024);let text='';
  for(let i=0;i<part.length;i+=8192)text+=String.fromCharCode(...part.subarray(i,i+8192));
  return {ok:true,data:btoa(text)};
}
async function pollMonitors(config) {
  const pending=[];
  for(const item of config.monitors||[]) {
    try{
      const result=await api(config,`/jobs/${item.id}/status`,{worker_id:config.worker_id,claim_token:item.claim_token});
      if(result.status==='completed'){try{await chrome.tabs.remove(item.tab_id);}catch{}continue;}
      if(result.status==='failed')continue;
      try{
        const progress=await chrome.tabs.sendMessage(item.tab_id,{type:'POLL_PROGRESS',submit_id:item.submit_id});
        if(progress&&progress.progress!==item.last_progress){await api(config,`/jobs/${item.id}/telemetry`,{worker_id:config.worker_id,claim_token:item.claim_token,...progress});item.last_progress=progress.progress;}
      }catch{}
      pending.push(item);
    }catch{pending.push(item);}
  }
  config.monitors=pending;await save(config);
}

async function tickInternal() {
  await start;
  if (ticking) return;
  ticking = true;
  try {
    let config = await load();
    if (!config.key || !config.worker_id || !Number.isInteger(config.tab_id)) return;
    if (config.outbox) { await flush(config); config = await load(); }
    await pollMonitors(config);config=await load();
    let content = null;
    try { content = await inspectTab(config.inflight?.tab_id || config.tab_id); }
    catch (e) { config.status = e.message; }
    if (config.inflight) {
      if(config.inflight.phase==='uncertain'){
        const terminal=await api(config,`/jobs/${config.inflight.id}/status`,{worker_id:config.worker_id,claim_token:config.inflight.claim_token});
        if(['completed','failed'].includes(terminal.status)){
          config.inflight=null;config.outbox=null;config.status='旧任务已由后台结束；请点击绑定当前页并启用以继续。';await save(config);return;
        }
      }
      const recovery = logic.recoveryAction(config.inflight, content, Date.now());
      if (recovery.action === 'report') await report(config, recovery.result);
      else if (recovery.action === 'uncertain') await report(config, {job_id: config.inflight.id, status: 'uncertain', error: '已领取任务的页面状态丢失或超时；不会重新点击。'});
      else if (recovery.action === 'hold') {
        await api(config, '/heartbeat', {account_id: config.account_id, worker_id: config.worker_id, protocol_version:4, page_url: content?.page_url || 'https://dreamina.capcut.com/ai-tool/generate', ready: false});
        config.status = '结果不确定，已暂停；保留原任务凭据等待核对或晚到任务 ID。'; await save(config);
      }
      else {
        await api(config, '/heartbeat', {account_id: config.account_id, worker_id: config.worker_id, protocol_version:4, page_url: content.page_url, ready: false});
        config.status = '已领取任务，等待网页结果；不会重复执行。'; await save(config);
      }
      return;
    }
    const ready = !!(config.enabled && content?.ready);
    await api(config, '/heartbeat', {account_id: config.account_id, worker_id: config.worker_id, protocol_version:4, page_url: content?.page_url || 'https://dreamina.capcut.com/ai-tool/generate', ready});
    config.status = content?.reason || config.status || '标签页不可用。'; await save(config);
    if (!ready) return;
    let response;
    try { response = await api(config, '/claim', {account_id: config.account_id, worker_id: config.worker_id}); }
    catch (e) {
      config.enabled = false; config.status = '领取结果不确定，已暂停。请核对服务端任务状态后再启用。'; await save(config); throw e;
    }
    if (!response.job) return;
    const job = response.job;
    if (typeof job.id !== 'string' || typeof job.claim_token !== 'string' || job.account_id !== config.account_id) {
      config.enabled = false; config.status = '服务端领取结构异常，已暂停，未点击。'; await save(config); return;
    }
    config.inflight = {id: job.id, claim_token: job.claim_token, deadline: Date.now() + 480000, phase: 'preparing', references:job.request.references||[]};
    config.status = '已领取任务，准备唯一一次原生提交。'; await save(config);
    try {
      logic.validateSpec(job.request);
      const runTab=await chrome.tabs.create({url:'https://dreamina.capcut.com/ai-tool/generate?type='+(job.request.kind==='image'?'image':'video'),active:true});
      config.inflight.tab_id=runTab.id;await save(config);
      await readyRunTab(config,runTab.id);
      config.inflight.phase='claimed';await save(config);
      const ack = await chrome.tabs.sendMessage(runTab.id, {type: 'RUN', job: {id: job.id, request: job.request}});
      if (!ack?.accepted) await report(await load(), {job_id: job.id, status: 'uncertain', error: '页面未确认接收；不会重派或再次点击。'});
    } catch (error) {
      // Delivery may have succeeded before the message channel broke: do not label it safely retryable.
      const current=await load();
      const preparing=current.inflight?.phase==='preparing';
      if(preparing)await api(current,`/jobs/${job.id}/telemetry`,{worker_id:current.worker_id,claim_token:current.inflight.claim_token,stage:'prepare_failed',observed_at:Date.now()/1000,detail:{reason:String(error.message||error).slice(0,300),clicked:false}});
      await report(current, {job_id: job.id, status: preparing?'failed':'uncertain', error: preparing?String(error.message||error).slice(0,300):'投递或页面响应不确定；不会重派或再次点击。'});
    }
  } catch (error) {
    const config = await load(); config.status = String(error.message || error).slice(0, 300); await save(config);
  } finally { ticking = false; }
}
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) void tick(); });
chrome.runtime.onStartup.addListener(() => { void tick(); });

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return;
  void exclusive(async () => {
    await start;
    const config = await load();
    const fromPopup = !sender.tab && sender.url === chrome.runtime.getURL('popup.html');
    if (message?.type === 'GET_STATUS' && fromPopup) return {extension_id: chrome.runtime.id, enabled: config.enabled,
      account_id: config.account_id || 'default', tab_id: config.tab_id, key_configured: !!config.key, status: config.status || '尚未绑定。', last_job: config.last_job, busy: !!config.inflight};
    if (message?.type === 'BIND' && fromPopup) {
      if(config.inflight){
        const terminal=await api(config,`/jobs/${config.inflight.id}/status`,{worker_id:config.worker_id,claim_token:config.inflight.claim_token});
        if(['completed','failed'].includes(terminal.status)){config.inflight=null;config.outbox=null;await save(config);}
      }
      if (config.inflight || config.outbox) throw new Error('仍有未核对任务，不能换标签页或账号。');
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(message.account_id)) throw new Error('账号 ID 只允许字母、数字、下划线和连字符。');
      const key = message.key || config.key;
      if (typeof key !== 'string' || key.length < 24 || /\s/.test(key)) throw new Error('请输入至少 24 字符且不含空白的本地配对密钥。');
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      if (!tab || !Number.isInteger(tab.id) || !tab.url || new URL(tab.url).origin !== 'https://dreamina.capcut.com') throw new Error('请先切到已登录的 Dreamina 标签页，再打开扩展。');
      const fresh = {...config, key, account_id: message.account_id, worker_id: config.worker_id || crypto.randomUUID(), tab_id: tab.id, enabled: true, status: '已绑定，正在连接本地服务。'};
      await save(fresh); await tickInternal(); return {ok: true};
    }
    if (message?.type === 'PAUSE' && fromPopup) { config.enabled = false; config.status = '已暂停领取新任务；已点击的任务仍会回报。'; await save(config); return {ok: true}; }
    if (message?.type === 'SYNC' && fromPopup) { await tickInternal(); return {ok: true}; }
    if (sender.tab?.id !== config.inflight?.tab_id || !config.inflight || message.job_id !== config.inflight.id) throw new Error('拒绝不属于绑定标签页的任务消息。');
    if(message.type==='JOB_TELEMETRY'){await api(config,`/jobs/${config.inflight.id}/telemetry`,{worker_id:config.worker_id,claim_token:config.inflight.claim_token,stage:message.stage,observed_at:message.observed_at,progress:message.progress,progress_source:message.progress_source,conversation_id:message.conversation_id,session_path:message.session_path,detail:message.detail});return {ok:true};}
    if(message.type==='ASSET_CHUNK')return assetChunk(config,message);
    if (message.type === 'JOB_CLICKING') {
      if (config.inflight.phase !== 'claimed') return {ok: false};
      config.inflight.phase = 'clicking'; await save(config); return {ok: true};
    }
    if (message.type === 'JOB_RESULT') {
      if (config.inflight.phase === 'uncertain' && message.status !== 'submitted') return {ok: true, held: true};
      await report(config, message); return {ok: true};
    }
    throw new Error('未知扩展消息。');
  }).then(value => respond(value), error => respond({ok: false, error: String(error.message || error).slice(0, 300)}));
  return true;
});
