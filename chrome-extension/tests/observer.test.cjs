const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const observer = require('../observer.js');
const ORIGIN = 'https://dreamina.capcut.com';
const URL_GENERATE = 'https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate?aid=513641';
const ID = '11111111-2222-4333-8444-555555555555';
const ID2 = '11111111-2222-4333-8444-555555555556';
const NONCE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const videoSpec = {model:'seedance-2.0-mini',prompt:'A silent watercolour lake.',duration:4,ratio:'16:9',resolution:'720p',mode:'omni'};
const imageSpec = {kind:'image',model:'seedream-5.0-lite',prompt:'A small turquoise vase.',n:1,size:'2048x2048',ratio:'1:1',resolution:'2K'};
function videoBody(id = ID, changes = {}) {
  return JSON.stringify({submit_id:id,extend:{root_model:'dreamina_seedance_40_mini'},draft_content:JSON.stringify({component_list:[{
    type:'video_base_component',abilities:{gen_video:{text_to_video_params:{model_req_key:'dreamina_seedance_40_mini',video_aspect_ratio:'16:9',
      video_gen_inputs:[{prompt:videoSpec.prompt,duration_ms:4000,resolution:'720p',...changes}]}}}
  }]})});
}
function imageBody() {
  return JSON.stringify({submit_id:ID,extend:{root_model:'high_aes_general_v50'},draft_content:JSON.stringify({component_list:[{
    type:'image_base_component',abilities:{generate:{core_param:{model:'high_aes_general_v50',prompt:imageSpec.prompt,
      large_image_info:{width:2048,height:2048,resolution_type:'2k'}}},gen_option:{gen_count:1}}
  }]})});
}
const success = (id = ID) => ({ret:'0',data:{aigc_data:{submit_id:id,history_record_id:'5389151150854'}}});

test('target is limited to POST on exact known generation paths and hosts', () => {
  for (const host of ['dreamina-api.us.capcut.com','dreamina-api-us-ttp2.us.capcut.com','mweb-api-sg.capcut.com']) assert.equal(observer.target(`https://${host}/mweb/v1/aigc_draft/generate`, 'POST'),true);
  for (const url of ['https://evil.example/mweb/v1/aigc_draft/generate','https://dreamina-api.us.capcut.com/mweb/v1/get_history_by_ids','http://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate','https://token@dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate']) assert.equal(observer.target(url,'POST'),false);
  assert.equal(observer.target(URL_GENERATE,'GET'),false);
});
test('video request identity comes from actual body, with all relevant parameters matched', () => {
  const rule = observer.expected(videoSpec);
  assert.equal(observer.parseRequest(videoBody(),rule).request_submit_id,ID);
  assert.equal(observer.parseRequest(videoBody(),rule).request_summary.model,'dreamina_seedance_40_mini');
  for (const changes of [{prompt:'Other prompt'}, {duration_ms:14000}, {resolution:'1080p'}, {first_frame_image:{image_uri:'x'}}]) assert.equal(observer.parseRequest(videoBody(ID,changes),rule),null);
  assert.equal(observer.parseRequest(videoBody('optimistic-dom-key'),rule),null);
});
test('image request matches the known core_param/model/size/count structure', () => {
  assert.equal(observer.parseRequest(imageBody(),observer.expected(imageSpec)).request_submit_id,ID);
  assert.equal(observer.parseRequest(imageBody(),observer.expected(videoSpec)),null);
  assert.equal(observer.expected({...imageSpec,n:2}),null);
});
test('success requires ret=0, matching server identity, and successful HTTP status', () => {
  const req = {kind:'video',request_submit_id:ID};
  assert.equal(observer.classifyResponse(req,200,success()).outcome,'submitted');
  assert.equal(observer.classifyResponse(req,200,{ret:0,data:{aigc_data:{history_record_id:'5389151150854'}}}).outcome,'submitted');
  for (const response of [null,{data:{}},{ret:0,data:{}},{ret:0,data:{aigc_data:{}}},success(ID2)]) assert.equal(observer.classifyResponse(req,200,response).outcome,'uncertain');
  assert.equal(observer.classifyResponse(req,500,success()).outcome,'uncertain');
});
test('explicit upstream rejection is failed and does not expose raw server messages', () => {
  const result=observer.classifyResponse({kind:'video',request_submit_id:ID},200,{ret:-6,errmsg:'secret-cookie=https://evil.example/?token=private'});
  assert.equal(result.outcome,'failed'); assert.equal(result.ret,'-6');
  assert.equal(JSON.stringify(result).includes('private'),false);
  assert.equal(JSON.stringify(result).includes('https'),false);
});
test('current private successful image draft remains compatible, when fixture exists', t => {
  const file=path.resolve(__dirname,'../../evidence/native-image-record.json');
  if (!fs.existsSync(file)) {t.skip('private evidence not distributed');return;}
  const record=JSON.parse(fs.readFileSync(file,'utf8'));
  const draft=typeof record.draft_content==='string'?JSON.parse(record.draft_content):record.draft_content;
  const prompt=draft.component_list[0].abilities.generate.core_param.prompt;
  const body=JSON.stringify({submit_id:ID,extend:{root_model:'high_aes_general_v50'},draft_content:JSON.stringify(draft)});
  assert.equal(observer.parseRequest(body,observer.expected({...imageSpec,prompt})).request_submit_id,ID);
});

function harness(fetchImplementation, ready = true) {
  const messages=[]; const listeners=[];
  class XHR {
    constructor(){this.handlers=[];this.status=0;this.responseType='';this.responseText='';this.sent=[];}
    open(...args){this.openArgs=args;return 'original-open-result';}
    send(...args){this.sent.push(args);return 'original-send-result';}
    addEventListener(type,listener,options){if(type==='loadend')this.handlers.push({listener,once:options?.once});}
    removeEventListener(type,listener){this.handlers=this.handlers.filter(h=>h.listener!==listener);}
    complete(body,status=200){this.status=status;this.responseText=JSON.stringify(body);for(const h of [...this.handlers]){if(h.once)this.removeEventListener('loadend',h.listener);h.listener.call(this);}}
  }
  const window={location:{origin:ORIGIN},document:{readyState:ready?'complete':'loading'},byted_acrawler:{isWebmssdk:true},_mssdk:{cacheOpts:{'513641':{aid:513641}}},fetch:fetchImplementation,XMLHttpRequest:XHR,
    addEventListener(type,callback){if(type==='message')listeners.push(callback);},
    postMessage(data,targetOrigin){assert.equal(targetOrigin,ORIGIN);messages.push(data);for(const fn of listeners)fn({source:window,origin:ORIGIN,data});}};
  const context=vm.createContext({window,URL,Request,Response,Promise,Date,WeakMap,Reflect,Set,Object,Array,JSON,Number,String,module:undefined});
  const source=fs.readFileSync(path.resolve(__dirname,'../observer.js'),'utf8');
  const install=()=>vm.runInContext(source,context);
  const readyResult=install();
  const arm=(spec=videoSpec,nonce=NONCE)=>window.postMessage({channel:observer.CHANNEL,direction:'to-observer',type:'arm',nonce,spec},ORIGIN);
  const results=()=>messages.filter(m=>m.direction==='from-observer'&&m.type==='result');
  return {window,messages,arm,results,XHR,install,readyResult};
}
const settle=()=>new Promise(resolve=>setTimeout(resolve,20));
test('early installation is a no-op without a guard, and can retry after SDK readiness', () => {
  const h=harness(()=>Promise.resolve(new Response('{}')),false);
  assert.equal(h.readyResult,false);assert.equal(h.window.__dreaminaBridgeObserverV3,undefined);
  h.window.document.readyState='complete';assert.equal(h.install(),true);
  h.arm();assert.equal(h.messages.find(m=>m.direction==='from-observer'&&m.type==='armed')?.ok,true);
});
test('fetch keeps original Promise and Response usable, observes once without replay', async () => {
  const calls=[]; const response=new Response(JSON.stringify(success()),{status:200});const originalPromise=Promise.resolve(response);
  const h=harness(function(...args){calls.push({self:this,args});return originalPromise;});
  h.arm(); const options={method:'POST',body:videoBody()}; const receiver={marker:true};
  const actual=h.window.fetch.call(receiver,URL_GENERATE,options);
  assert.equal(actual,originalPromise);assert.equal(calls.length,1);assert.equal(calls[0].self,receiver);assert.equal(calls[0].args[1],options);
  assert.deepEqual(await (await actual).json(),success());await settle();
  assert.equal(h.results().length,1);assert.equal(h.results()[0].request_submit_id,ID);assert.equal(h.results()[0].outcome,'submitted');
  assert.equal(calls.length,1);
});
test('unarmed and nonmatching requests are never reported', async () => {
  const h=harness(()=>Promise.resolve(new Response(JSON.stringify(success()),{status:200})));
  await h.window.fetch(URL_GENERATE,{method:'POST',body:videoBody()});h.arm();
  await h.window.fetch(URL_GENERATE,{method:'POST',body:videoBody(ID,{prompt:'Other prompt'})});await settle();assert.equal(h.results().length,0);
});
test('XHR keeps open/send return values and arguments, with no extra send', () => {
  const h=harness(()=>Promise.resolve(new Response('{}')));h.arm();const xhr=new h.XHR();
  assert.equal(xhr.open('POST',URL_GENERATE,true),'original-open-result');
  const body=videoBody();assert.equal(xhr.send(body),'original-send-result');
  assert.deepEqual(xhr.sent,[[body]]);xhr.complete(success());
  assert.equal(h.results().length,1);assert.equal(h.results()[0].outcome,'submitted');assert.deepEqual(xhr.sent,[[body]]);
});
test('different matching request IDs in one armed job become uncertain', () => {
  const h=harness(()=>Promise.resolve(new Response('{}')));h.arm();
  const a=new h.XHR();a.open('POST',URL_GENERATE);a.send(videoBody());
  const b=new h.XHR();b.open('POST',URL_GENERATE);b.send(videoBody(ID2));
  a.complete(success());b.complete(success(ID2));
  assert.equal(h.results().length,1);assert.equal(h.results()[0].outcome,'uncertain');assert.equal(h.results()[0].reason,'multiple_matching_requests');
});
test('disarm ignores a late response, preserving normal request behavior', () => {
  const h=harness(()=>Promise.resolve(new Response('{}')));h.arm();const xhr=new h.XHR();xhr.open('POST',URL_GENERATE);xhr.send(videoBody());
  h.window.postMessage({channel:observer.CHANNEL,direction:'to-observer',type:'disarm',nonce:NONCE},ORIGIN);xhr.complete(success());
  assert.equal(h.results().length,0);assert.equal(xhr.sent.length,1);
});
