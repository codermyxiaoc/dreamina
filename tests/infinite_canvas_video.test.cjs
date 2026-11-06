const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../examples/infinite_canvas_video.js'),'utf8');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const names=['prompt','images','videos','audios','params','model','baseUrl','apiKey','request','poll','signal'];
const execute=new AsyncFunction(...names,source);
const id='video_'+'a'.repeat(32), model='seedance-2.0-mini';
const png='data:image/png;base64,aGVsbG8=';
const video=new File([new Uint8Array([1,2,3])],'motion.mp4',{type:'video/mp4'});
const blob=new Blob([new Uint8Array([0,0,0,24,102,116,121,112])],{type:'video/mp4'});

function harness(changes={}) {
  const calls=[];let pollCalls=0;
  const options={prompt:'让@图片1参考@视频1走起来',images:[png],videos:[video],audios:[],
    params:{mode:'reference',seconds:'4',size:'16:9',resolution:'720p'},model,baseUrl:'http://gateway.example/v1/',apiKey:'sk-dr-synthetic',signal:undefined,
    request:async req=>{const chosen=String(options.model||model).trim().toLowerCase();calls.push(req);if(req.method==='post')return {id,model:chosen,status:'queued'};
      if(req.url.endsWith('/content'))return blob;
      return {id,model:chosen,status:++pollCalls<2?'in_progress':'completed',progress:pollCalls<2?50:100};},
    poll:async (request,extract,config)=>{assert.equal(config.timeoutMs,1800000);for(let i=0;i<12;i++){const value=extract(await request());if(value)return value;}throw new Error('Synthetic timeout');},...changes};
  return {calls,options,run:()=>execute(...names.map(name=>options[name]))};
}

test('uploads native references, keeps aliases, creates once, polls and downloads authenticated Blob',async()=>{
  const env=harness(),result=await env.run();assert.equal(result.blob,blob);
  const create=env.calls[0];assert.equal(create.url,'http://gateway.example/v1/videos');
  assert.equal(create.headers.Authorization,'Bearer sk-dr-synthetic');assert.match(create.headers['Idempotency-Key'],/^canvas-[a-f0-9-]+$/);
  assert.equal(create.headers['Content-Type'],undefined);assert.ok(create.data instanceof FormData);
  assert.equal(create.data.has('references'),false);
  const upload=create.data.getAll('reference_images');assert.equal(upload.length,1);
  assert.equal(upload[0].type,'image/png');assert.equal(await upload[0].text(),'hello');
  assert.equal(create.data.getAll('reference_videos').length,1);assert.equal(create.data.get('prompt'),env.options.prompt);
  assert.equal(create.data.get('mode'),'omni');assert.equal(create.data.get('ratio'),'16:9');
  assert.equal(create.data.has('generate_audio'),false);assert.equal(create.data.has('watermark'),false);
  assert.equal(env.calls.filter(c=>c.method==='post').length,1);
  assert.equal(env.calls.at(-1).responseType,'blob');assert.equal(env.calls.at(-1).headers.Authorization,'Bearer sk-dr-synthetic');
});

test('supports body and Blob wrapped in Axios-style data',async()=>{
  const env=harness();const original=env.options.request;
  env.options.request=async req=>({data:await original(req),status:200});assert.equal((await env.run()).blob,blob);
});

for(const thrown of [false,true])test('recovers allocated task from 409 without another POST; thrown='+thrown,async()=>{
  const env=harness();const original=env.options.request;
  env.options.request=async req=>{if(req.method!=='post')return original(req);env.calls.push(req);
    const body={task_id:id,error:{code:'dispatch_uncertain',message:'Check original task'}};
    if(thrown)throw {response:{status:409,data:body}};return body;};
  assert.equal((await env.run()).blob,blob);assert.equal(env.calls.filter(c=>c.method==='post').length,1);
});

for(const params of [
  {seconds:4,size:'1920x1080'}, {seconds:4,size:'1080x1920',resolution:'1080p',ratio:'9:16'},
  {seconds:4,size:'auto',resolution:'720p',ratio:'21:9'},
])test('normalizes dimensions without conflicting API fields '+JSON.stringify(params),async()=>{
  const env=harness({params});await env.run();const data=env.calls[0].data;
  assert.equal(data.has('size'),false);assert.ok(['720p','1080p'].includes(data.get('resolution')));
  if(params.size==='1920x1080')assert.equal(data.get('resolution'),'1080p');
  if(params.ratio)assert.equal(data.get('ratio'),params.ratio);
});

for(const changes of [
  {params:{mode:'frames'}}, {audios:[video]}, {params:{seconds:3}}, {params:{seconds:16}},
  {params:{size:'1280x720',resolution:'1080p'}},{params:{size:'720x1280',ratio:'16:9'}},
  {params:{resolution:'480p'}},{images:['https://remote.example/image.png']},{apiKey:''},
  {images:Array(10).fill(png)}, {videos:Array(4).fill(video)},{model:'seedance-unknown'},
  {model:'constructor'},{params:{seconds:'0x4'}},{params:{seconds:true}},
  {params:{resolution:'2k'}},{params:{resolution:'4k'}},
])test('unsupported request fails before any network '+JSON.stringify(changes).slice(0,90),async()=>{
  const env=harness(changes);await assert.rejects(env.run());assert.equal(env.calls.length,0);
});

test('text-only frames UI default does not invent frame semantics',async()=>{
  const env=harness({images:[],videos:[],prompt:'云朵移动',params:{mode:'frames'}});await env.run();assert.equal(env.calls[0].data.get('mode'),'omni');
});

test('more than two images becomes reference as Canvas specifies',async()=>{
  const env=harness({images:[png,png,png],videos:[],params:{mode:'frames'}});await env.run();assert.equal(env.calls[0].data.get('mode'),'omni');
});

for(const state of [
  {id,model,status:'in_progress',metadata:{verification_required:true}},
  {id,model,status:'failed',error:{code:'review_failed',message:'Synthetic rejected'}},
  {id:'video_'+'b'.repeat(32),model,status:'completed'},
])test('uncertain failed or mismatched task stops without downloading or resubmitting '+state.status,async()=>{
  const env=harness();const original=env.options.request;
  env.options.request=async req=>{if(req.method==='post')return original(req);env.calls.push(req);return state;};
  await assert.rejects(env.run(),new RegExp(id));assert.equal(env.calls.filter(c=>c.method==='post').length,1);
  assert.equal(env.calls.some(c=>c.url.endsWith('/content')),false);
});

test('only transient reads retry; create network failure is never automatically repeated',async()=>{
  const env=harness();const original=env.options.request;let failures=0;
  env.options.request=async req=>{if(req.method==='get'&&!req.url.endsWith('/content')&&failures++<2){env.calls.push(req);throw {response:{status:503}};}return original(req);};
  assert.equal((await env.run()).blob,blob);assert.equal(env.calls.filter(c=>c.method==='post').length,1);
  const bad=harness();bad.options.request=async req=>{bad.calls.push(req);throw new Error('Network failed');};
  await assert.rejects(bad.run(),/canvas-/);assert.equal(bad.calls.length,1);
});

test('timeout and cancellation retain original task identity',async()=>{
  const env=harness({poll:async()=>null});await assert.rejects(env.run(),new RegExp(id));assert.equal(env.calls.length,1);
  const ctrl=new AbortController(),aborted=harness({signal:ctrl.signal});ctrl.abort();
  await assert.rejects(aborted.run(),/取消/);assert.equal(aborted.calls.length,0);
  const late=new AbortController(),running=harness({signal:late.signal,poll:async(read)=>{late.abort();return read();}});
  await assert.rejects(running.run(),new RegExp(id));assert.equal(running.calls.length,1);
});

const backendModels=JSON.parse(fs.readFileSync(path.join(__dirname,'../dreamina_api/data/model-config.json'),'utf8')).models;
for(const config of backendModels) {
  const canonical=config.id;
  for(const seconds of [config.duration.min,config.duration.max])test(`${canonical} accepts backend duration boundary ${seconds}`,async()=>{
    const env=harness({model:canonical,params:{seconds}});assert.equal((await env.run()).blob,blob);
    assert.equal(env.calls[0].data.get('model'),canonical);assert.equal(env.calls[0].data.get('seconds'),String(seconds));
    assert.equal(env.calls.filter(c=>c.method==='post').length,1);
  });
  test(`${canonical} rejects excessive duration and prompt before POST`,async()=>{
    for(const changes of [{params:{seconds:config.duration.max+1}},{prompt:'x'.repeat(config.max_prompt_length+1)}]){
      const env=harness({model:canonical,...changes});await assert.rejects(env.run());assert.equal(env.calls.length,0);
    }
  });
  test(`${canonical} respects model-specific material count boundaries`,async()=>{
    const count=(kind)=>config.materials.supported_materials.find(m=>m.material_type===kind).limit.max_count;
    const env=harness({model:canonical,images:Array(count(1)).fill(png),videos:Array(count(2)).fill(video)});
    await env.run();assert.equal(env.calls[0].data.getAll('reference_images').length,count(1));assert.equal(env.calls[0].data.getAll('reference_videos').length,count(2));
    for(const changes of [{images:Array(count(1)+1).fill(png)},{videos:Array(count(2)+1).fill(video)}]){
      const bad=harness({model:canonical,...changes});await assert.rejects(bad.run());assert.equal(bad.calls.length,0);
    }
  });
  for(const resolution of config.resolutions.filter(r=>/^(480|720|1080)p$/i.test(r))) {
    test(`${canonical} supports each backend native size at ${resolution}`,async()=>{
      for(const ratio of config.ratios) {
        const tier=Number.parseInt(resolution,10),[a,b]=ratio.split(':').map(Number);
        const size=a>=b?`${Math.round(tier*a/b)}x${tier}`:`${tier}x${Math.round(tier*b/a)}`;
        const env=harness({model:canonical,params:{size}});await env.run();const form=env.calls[0].data;
        assert.equal(form.get('resolution'),resolution);assert.equal(form.get('ratio'),ratio);assert.equal(form.has('size'),false);
      }
    });
  }
}

test('large reference images are binary files, bypassing the gateway text-field limit',async()=>{
  const bytes=Buffer.alloc(250000,97);const url='data:image/png;base64,'+bytes.toString('base64');
  const env=harness({images:[url,url],videos:[],prompt:'@图片1 和 @图片2 保持一致'});await env.run();
  const form=env.calls[0].data;assert.equal(form.has('references'),false);
  const files=form.getAll('reference_images');assert.equal(files.length,2);
  for(const file of files){assert.equal(file.size,bytes.length);assert.deepEqual(Buffer.from(await file.arrayBuffer()),bytes);}
  const wire=new Request('http://fixture.invalid',{method:'POST',body:form});
  assert.match(wire.headers.get('content-type'),/^multipart\/form-data; boundary=/);
  assert.ok((await wire.arrayBuffer()).byteLength<bytes.length*2+5000);
});

test('preserves upload order and precise mentions without adding a system prompt',async()=>{
  const env=harness({model:'seedance-2.5',images:[png,'data:image/webp;base64,d29ybGQ='],videos:[video,video],prompt:'@图片2 按 @视频1 运动，保持 @图片1 的颜色，再参考 @视频2。'});
  await env.run();const form=env.calls[0].data,images=form.getAll('reference_images');
  assert.equal(form.get('prompt'),env.options.prompt);assert.equal(await images[0].text(),'hello');assert.equal(await images[1].text(),'world');
  assert.equal(images[0].name,'image-1.png');assert.equal(images[1].name,'image-2.webp');
});

test('server retry can return to queued while Canvas keeps the same ID and single POST',async()=>{
  const env=harness({model:'seedance-2.0-fast'}),original=env.options.request;
  const states=[{status:'in_progress'},{status:'queued',metadata:{submission_retry:{attempt:2,stage:'waiting'}}},{status:'in_progress',metadata:{submission_retry:{attempt:2,stage:'preparing'}}},{status:'completed'}];
  env.options.request=async req=>{
    if(req.method==='post'||req.url.endsWith('/content'))return original(req);
    env.calls.push(req);return {id,model:'seedance-2.0-fast',...states.shift()};
  };
  assert.equal((await env.run()).blob,blob);assert.equal(states.length,0);assert.equal(env.calls.filter(c=>c.method==='post').length,1);
});

for(const images of [['data:image/png;base64,a'],['data:image/png;base64,a==='],['data:image/gif;base64,aGVsbG8=']])test('invalid image encoding/format fails before create '+images[0],async()=>{
  const env=harness({images});await assert.rejects(env.run());assert.equal(env.calls.length,0);
});

test('2.5 accepts longer UTF16 prompt without incorrectly applying the 2.0 limit',async()=>{
  const env=harness({model:'seedance-2.5',prompt:'图'.repeat(10000),params:{seconds:30,resolution:'480p'}});
  await env.run();assert.equal(env.calls[0].data.get('prompt').length,10000);
  const mini=harness({prompt:'🌸'.repeat(2001)});await assert.rejects(mini.run(),/UTF-16/);assert.equal(mini.calls.length,0);
});
