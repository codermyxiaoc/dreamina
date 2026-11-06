const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function setup(remoteStatus){
 const calls=[];const state={key:'synthetic-local-key',worker_id:'worker',enabled:false,inflight:{id:'job',claim_token:'claim',phase:'uncertain'},outbox:{job_id:'job',body:{worker_id:'worker',claim_token:'claim',status:'uncertain'}}};
 const event={addListener(){}};
 const c=vm.createContext({console,URL,Promise,Map,Date,Uint8Array,AbortSignal,crypto:require('node:crypto').webcrypto,
  DreaminaBridgeLogic:require('../logic.js'),synthetic:state,
  chrome:{storage:{local:{async setAccessLevel(){},async get(){return {bridge:state}},async set(x){Object.assign(state,x.bridge)}}},
   alarms:{async get(){return {}},async create(){},onAlarm:event},runtime:{onStartup:event,onMessage:event}},
  fetch:async(url)=>{calls.push(url);return {ok:true,json:async()=>({status:remoteStatus,submit_id:null})}}});
 const src=fs.readFileSync(path.join(__dirname,'../background.js'),'utf8').replace("import './logic.js';",'');vm.runInContext(src,c);
 return {c,state,calls};
}
test('a terminal backend task clears an uncertain outbox without re-reporting or clicking',async()=>{
 const {c,state,calls}=setup('failed');await vm.runInContext('flush(synthetic)',c);
 assert.equal(state.outbox,null);assert.equal(state.inflight,null);assert.equal(state.enabled,false);
 assert.equal(calls.length,1);assert.ok(calls[0].endsWith('/status'));
});
test('completed server receipts also release stale pending reports safely',async()=>{
 const {c,state,calls}=setup('completed');await vm.runInContext('flush(synthetic)',c);
 assert.equal(state.last_job.status,'completed');assert.equal(state.outbox,null);assert.equal(calls.length,1);
});
