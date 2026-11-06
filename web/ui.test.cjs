const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'app.js'),'utf8');
function scope(fetchImpl=async()=>({ok:true,status:200,json:async()=>({ok:true})})){
  const node={innerHTML:'',className:'',style:{},append(){},querySelector(){return null},showModal(){this.open=true},close(){this.open=false}};
  const context=vm.createContext({console,URL,Date,Intl,Map,Set,Error,Array,Number,String,Object,JSON,Math,Promise,AbortController,
    document:{querySelector(){return node},getElementById(){return node}},
    location:{origin:'http://localhost:8787',hash:'#/overview'},fetch:fetchImpl,FormData:class{},
    localStorage:{setItem(){}},setTimeout(){},clearTimeout(){},requestAnimationFrame(fn){fn()},navigator:{clipboard:{writeText:async()=>{}}}});
  vm.runInContext(source.slice(0,source.indexOf("document.addEventListener('click'")),context);
  return context;
}
test('untrusted names and prompts render as text, never executable markup',()=>{
  const c=scope();const result=vm.runInContext("tasksTable([{id:'test',kind:'video',model:'mini',account_id:'default',prompt:'<img src=x onerror=alert(1)>',status:'failed',created_at:1}])",c);
  assert.ok(result.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!result.includes('<img src=x'));
  assert.ok(result.includes('失败'));
});
test('zero request quota is not displayed as unlimited',()=>{
  const c=scope();const output=vm.runInContext("keysTable([{id:'k',name:'zero',prefix:'sk-dr-x',enabled:true,request_limit:0,used_requests:0,max_concurrent:1,rate_limit_per_minute:60}])",c);
  assert.ok(!output.includes('不限'));
  assert.ok(output.includes('/ 0'));
});
test('key list does not render a key field even if one is accidentally provided',()=>{
  const c=scope();vm.runInContext("globalThis.result=keysTable([{id:'k',name:'safe',prefix:'sk-dr-prefix',key:'do-not-render-secret',enabled:true,max_concurrent:1}]);",c);
  assert.ok(!c.result.includes('do-not-render-secret'));
  assert.ok(c.result.includes('sk-dr-prefix'));
});
test('disabled accounts take precedence over online badge',()=>{
  const c=scope();const output=vm.runInContext("accountsTable([{id:'a',name:'a',region:'US',enabled:false,online:true,status:'healthy',priority:0,max_concurrent:1}])",c);
  assert.ok(output.includes('已停用'));
  assert.ok(!output.includes('>就绪<'));
});
test('login state takes precedence over stale online worker heartbeat',()=>{
  const c=scope();
  for(const [status,label] of [['login_pending','正在登录'],['login_required','需要登录']]){
    const output=vm.runInContext(`accountsTable([{id:'a',name:'a',region:'US',enabled:true,online:true,ready:true,status:'${status}',priority:0,max_concurrent:1}])`,c);
    assert.ok(output.includes(label));
    assert.ok(!output.includes('>就绪<'));
  }
});

test('low-credit scheduling pause takes precedence over a ready browser without changing manual disabled state',()=>{
  const c=scope();
  c.account={enabled:true,online:true,ready:true,status:'ready',scheduling_paused:true,scheduling_pause_reason:'below_credit_threshold'};
  let output=vm.runInContext('accountStatusBadge(account)',c);
  assert.ok(output.includes('积分暂停')&&!output.includes('>就绪<'));
  c.account.enabled=false;output=vm.runInContext('accountStatusBadge(account)',c);
  assert.ok(output.includes('已停用')&&!output.includes('积分暂停'));
  c.account.enabled=true;c.account.status='login_required';output=vm.runInContext('accountStatusBadge(account)',c);
  assert.ok(output.includes('需要登录')&&!output.includes('积分暂停'));
  c.account={enabled:true,online:true,ready:true,status:'ready',balance:null,min_credit_balance:20,scheduling_paused:false};
  output=vm.runInContext('accountStatusBadge(account)',c);
  assert.ok(output.includes('待确认余额')&&!output.includes('>就绪<'));
});

test('credit thresholds and holds show actual zero while unknown balances stay unknown',()=>{
  const c=scope();
  c.account={balance:0,min_credit_balance:20,reserved_credits:0,available_credits:0,scheduling_paused:true,scheduling_pause_reason:'below_credit_threshold'};
  let output=vm.runInContext('accountCreditSummary(account)',c);
  assert.ok(output.includes('<strong>0</strong>')&&output.includes('本地保留 0')&&output.includes('可分配 0'));
  assert.ok(output.includes('余额低于 20 暂停')&&output.includes('刷新余额达标后恢复'));
  c.account={balance:null,min_credit_balance:20,reserved_credits:0,available_credits:null};
  output=vm.runInContext('accountCreditSummary(account)',c);
  assert.ok(output.includes('未确认')&&!output.includes('可分配 0')&&!output.includes('刷新余额达标后恢复'));
  c.account={balance:20,min_credit_balance:20,scheduling_paused:false};
  assert.ok(!vm.runInContext('accountCreditSummary(account)',c).includes('刷新余额达标后恢复'));
  c.account={scheduling_paused:true,scheduling_pause_reason:'<img src=x onerror=alert(1)>'};
  output=vm.runInContext('accountCreditSummary(account)',c);
  assert.ok(output.includes('&lt;img src=x')&&!output.includes('<img src=x'));
});

test('configured concurrency never masquerades as running browser slots',()=>{
  const c=scope();
  c.account={active_tasks:2,max_concurrent:4,max_inflight:6,priority:1,managed_worker:{effective_concurrency:2,active_slots:1,attention_slots:1}};
  let output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('在途任务 2 / 上限 6')&&output.includes('标签页配置 4')&&output.includes('运行标签页 1 / 2'));
  assert.ok(output.includes('空闲后生效')&&output.includes('1 个槽位需要核对'));
  c.account={max_concurrent:8,active_tasks:0};output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('运行槽位未上报')&&!output.includes('运行标签页 0 / 8'));
});

test('account editor includes separate threshold and bounded concurrency fields',()=>{
  const c=scope();
  vm.runInContext("document.querySelector('#account-form').addEventListener=()=>{};state.cache.accounts={items:[{id:'one',name:'One',min_credit_balance:35,max_concurrent:3}]};accountDialog('one')",c);
  const html=vm.runInContext("document.querySelector('#modal').innerHTML",c);
  assert.ok(html.includes('name="min_credit_balance"')&&html.includes('value="35"'));
  assert.ok(html.includes('单账号并发标签页数')&&html.includes('max="8"'));
  assert.ok(html.includes('上游在途任务上限')&&html.includes('name="max_inflight"')&&html.includes('max="100"'));
  assert.ok(html.includes('默认1')&&html.includes('缩减会关闭并清理多余槽位')&&html.includes('以节点回报确认生效'));
  assert.ok(html.includes('0关闭')&&html.includes('不取消已提交任务'));
});

test('account save sends threshold including explicit zero without leaking existing credentials',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,json:async()=>({id:'one'})};});
  vm.runInContext("FormData=class{constructor(form){this.values=form.values}get(n){return this.values[n]??null}getAll(n){return this.values[n]||[]}has(n){return !!this.values[n]}};state.csrf='csrf';navigate=async()=>{};toast=()=>{};globalThis.form={isConnected:true,values:{name:'One',region:'US',enabled:true,priority:'0',max_concurrent:'3',min_credit_balance:'0',models:['seedance-2.0-mini']},querySelector(){return {textContent:''}}}",c);
  await vm.runInContext("saveAccount({preventDefault(){},currentTarget:form},'one')",c);
  assert.equal(calls.length,1);const body=JSON.parse(calls[0].opts.body);
  assert.equal(body.min_credit_balance,0);assert.equal(body.max_concurrent,3);
  assert.equal(body.max_inflight,1);
  assert.equal(body.cookie,undefined);assert.equal(body.session_id,undefined);
  assert.equal(calls[0].opts.headers['X-CSRF-Token'],'csrf');
});

test('inflight and physical slot capacities remain distinct including old-node fallback',()=>{
  const c=scope();c.account={max_concurrent:1,max_inflight:4,current_inflight_capacity:1,active_tasks:1,detached_generation_supported:false,managed_worker:{effective_concurrency:1,active_slots:0}};
  const html=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(html.includes('在途任务 1 / 上限 4')&&html.includes('标签页配置 1')&&html.includes('运行标签页 0 / 1'));
  assert.ok(html.includes('当前生效在途上限 1')&&html.includes('旧节点'));
});

test('supervision displays deliberate stops, backoff and open circuits without raw reason secrets',()=>{
  const c=scope();c.worker={desired_running:false,reason:'cookie-secret'};
  let html=vm.runInContext('workerSupervisionSummary(worker)',c);assert.ok(html.includes('不自动启动')&&!html.includes('cookie-secret'));
  c.worker={desired_running:true,next_restart_at:1800000000,restart_failures:2};html=vm.runInContext('workerSupervisionSummary(worker)',c);
  assert.ok(html.includes('计划重启')&&html.includes('连续恢复失败 2 次'));
  c.worker={restart_circuit_open:true,next_restart_at:1800000000};html=vm.runInContext('workerSupervisionSummary(worker)',c);
  assert.ok(html.includes('熔断')&&html.includes('手动启动')&&!html.includes('计划重启'));
});

test('monitoring shows unknown values honestly and renders only bounded whitelisted diagnostics',()=>{
  const c=scope();c.monitor={observed_at:1800000000,metrics:{active_tasks:2,queued_tasks:0,inflight_tasks:1,uncertain_tasks:1,held_credits:75,ready_slots:4,desired_workers:4,crashed_workers:0},alerts:[{severity:'warning',message:'<img src=x>',code:'original_pending',task_id:'task-safe',password:'should-not-render'}],maintenance:{state:'completed',bytes_reclaimed:1048576,disk_free_bytes:10737418240},pacing:[{scope:'egress:primary',awaiting_receipt:true,token:'should-not-render'}]};
  let html=vm.runInContext('renderMonitoring(monitor)',c);
  assert.ok(html.includes('活动任务')&&html.includes('保留积分')&&html.includes('75'));
  assert.ok(html.includes('&lt;img src=x&gt;')&&!html.includes('<img src=x')&&!html.includes('should-not-render'));
  assert.ok(html.includes('等待原提交回执')&&html.includes('10.0 GiB')&&html.includes('1.0 MiB'));
  html=vm.runInContext('renderMonitoring({metrics:{active_tasks:null}})',c);assert.ok(html.includes('—')&&!html.includes('>null<'));
  assert.ok(vm.runInContext('renderMonitoring(null)',c).includes('暂未取得'));
});

test('uncertain reconciliation UI only queries originals and never offers force release',()=>{
  const c=scope();c.task={id:'video_fixture',status:'uncertain',submit_id:'00000000-0000-0000-0000-000000000001'};
  let html=vm.runInContext('taskReconcilePanel(task)',c);
  assert.ok(html.includes('核对原任务')&&html.includes('readonly')&&html.includes('查不到任务或没有扣费记录不能作为释放依据'));
  assert.ok(!html.includes('强制释放')&&!html.includes('force')&&!html.includes('重新生成'));
  c.task={status:'completed'};assert.equal(vm.runInContext('taskReconcilePanel(task)',c),'');
  c.task={status:'uncertain'};html=vm.runInContext('taskReconcilePanel(task)',c);assert.ok(!html.includes('readonly')&&html.includes('maxlength="36"'));
});

test('reconcile submits optional original UUID and note exactly once with CSRF',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,json:async()=>({status:'uncertain'})};});
  vm.runInContext("state.csrf='csrf';toast=()=>{};taskDialog=async()=>{};globalThis.err={textContent:''};globalThis.btn={disabled:false,isConnected:true};globalThis.form={elements:{reconcile_submit_id:{value:'00000000-0000-0000-0000-000000000001'},reconcile_note:{value:'Checked original page'}},querySelector(s){return s==='[type=submit]'?btn:err}}",c);
  await vm.runInContext("submitTaskReconcile({preventDefault(){},currentTarget:form},{id:'video_safe',status:'uncertain'})",c);
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/admin/tasks/video_safe/reconcile');assert.equal(calls[0].opts.headers['X-CSRF-Token'],'csrf');
  assert.deepEqual(JSON.parse(calls[0].opts.body),{submit_id:'00000000-0000-0000-0000-000000000001',note:'Checked original page'});
});

test('known-ID reconciliation cannot replace identity and errors do not claim release',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:false,status:409,json:async()=>({detail:'Still unconfirmed; hold retained'})};});
  vm.runInContext("state.csrf='csrf';globalThis.err={textContent:''};globalThis.btn={disabled:false,isConnected:true};globalThis.form={elements:{reconcile_submit_id:{value:'00000000-0000-0000-0000-000000000002'},reconcile_note:{value:''}},querySelector(s){return s==='[type=submit]'?btn:err}}",c);
  await vm.runInContext("submitTaskReconcile({preventDefault(){},currentTarget:form},{id:'video_safe',submit_id:'00000000-0000-0000-0000-000000000001'})",c);
  assert.equal(calls.length,1);assert.equal(JSON.parse(calls[0].opts.body).submit_id,undefined);
  assert.equal(c.btn.disabled,false);assert.ok(c.err.textContent.includes('hold retained'));
});

test('invalid reconciliation input is rejected before a request',async()=>{
  const calls=[];const c=scope(async(...args)=>{calls.push(args);throw Error('must not request')});
  vm.runInContext("globalThis.err={textContent:''};globalThis.btn={disabled:false};globalThis.form={elements:{reconcile_submit_id:{value:'not-a-uuid'},reconcile_note:{value:''}},querySelector(s){return s==='[type=submit]'?btn:err}}",c);
  await vm.runInContext("submitTaskReconcile({preventDefault(){},currentTarget:form},{id:'video_safe'})",c);
  assert.equal(calls.length,0);assert.ok(c.err.textContent.includes('UUID'));
  vm.runInContext("form.elements.reconcile_submit_id.value='';form.elements.reconcile_note.value='x'.repeat(501)",c);
  await vm.runInContext("submitTaskReconcile({preventDefault(){},currentTarget:form},{id:'video_safe'})",c);assert.equal(calls.length,0);assert.ok(c.err.textContent.includes('500'));
});
test('unknown model capabilities are not shown as integrated',()=>{
  const c=scope();assert.equal(vm.runInContext("modelAvailable({id:'candidate'})",c),false);
  assert.equal(vm.runInContext("modelAvailable({id:'tested',available:true})",c),true);
});

test('video reliability shows actual denominators and never presents the 99 percent target as measured success',()=>{
  const c=scope();c.snapshot={scope:'all_recorded_video_tasks',target_percent:99,overall:{accepted:10,completed:3,automatic_completed:2,failed:4,cancelled:1,active:1,uncertain:1,unknown:0,terminal:8,outstanding:2,closed_success_rate_percent:37.5,automatic_closed_success_rate_percent:25,accepted_completion_rate_percent:30,wilson_95_lower_percent:13.7,automatic_wilson_95_lower_percent:7.1479,manual_reconciled:1,manual_reconciled_completed:1,sample_supports_target:false},by_model:[],failure_categories:[{label:'套餐或账号权限',count:4}],evidence:{minimum_all_success_terminal_samples_for_target:381}};
  const output=vm.runInContext('renderReliability(snapshot)',c);
  assert.ok(output.includes('目标 99%'));
  assert.ok(output.includes('25.00%')&&output.includes('37.50%')&&output.includes('30.00%'));
  assert.ok(output.includes('2 / 8')&&output.includes('3 / 8')&&output.includes('3 / 10'));
  assert.ok(output.includes('自动完成率的 95% 双侧 Wilson 下界 7.14%'));
  assert.ok(output.includes('总交付成功率 37.50%'));
  assert.ok(output.includes('自动查询恢复仍计入自动完成，手工核对完成单列'));
  assert.ok(output.includes('未完成 / 待核对')&&output.includes('1 / 1'));
  assert.ok(output.includes('套餐或账号权限 · 4'));
  assert.ok(output.includes('观测样本尚未支持 99% 目标'));
  assert.ok(!output.includes('保证 99%'));
});

test('existing combined dashboard rate states its image and video scope separately from reliability',()=>{
  const c=scope();
  const output=vm.runInContext('renderOverview()',c);
  assert.ok(output.includes('综合历史成功率'));
  assert.ok(output.includes('图片+视频：完成 ÷（完成+失败）'));
  assert.ok(output.includes('视频生成可靠性'));
});

test('video reliability with no samples or unavailable endpoint never fabricates a zero or 99 percent observation',async()=>{
  const c=scope(async()=>({ok:false,status:503,json:async()=>({error:{message:'Synthetic unavailable'}})}));
  assert.equal(await vm.runInContext('loadReliability()',c),null);
  const unavailable=vm.runInContext('renderReliability(null)',c);
  assert.ok(unavailable.includes('统计暂不可用')&&!unavailable.includes('0.00%'));
  c.snapshot={overall:{accepted:0,terminal:0,automatic_completed:0,automatic_closed_success_rate_percent:null,automatic_wilson_95_lower_percent:null,closed_success_rate_percent:null,accepted_completion_rate_percent:null,wilson_95_lower_percent:null},by_model:[],failure_categories:[]};
  const empty=vm.runInContext('renderReliability(snapshot)',c);
  assert.ok(empty.includes('0 / 0')&&!empty.includes('0.00%')&&!empty.includes('99.00%'));
});

test('reliability model labels are escaped and statistical lower bounds are not rounded up to the target',()=>{
  const c=scope();c.snapshot={overall:{accepted:380,terminal:380,completed:380,automatic_completed:380,automatic_closed_success_rate_percent:100,automatic_wilson_95_lower_percent:98.9976,closed_success_rate_percent:100,accepted_completion_rate_percent:100,wilson_95_lower_percent:98.9976,sample_supports_target:false},by_model:[{model:'<img src=x onerror=alert(1)>',accepted:380,completed:380,automatic_completed:380,terminal:380,automatic_closed_success_rate_percent:100,closed_success_rate_percent:100}],failure_categories:[]};
  const html=vm.runInContext('renderReliability(snapshot)',c);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!html.includes('<img src=x'));
  assert.ok(html.includes('下界 98.99%'));
  assert.ok(!html.includes('下界 99.00%'));
});

test('manually recovered deliveries stay visible but cannot appear as automatic 99 percent evidence',()=>{
  const c=scope();c.snapshot={overall:{accepted:381,terminal:381,completed:381,automatic_completed:0,automatic_closed_success_rate_percent:0,automatic_wilson_95_lower_percent:0,closed_success_rate_percent:100,accepted_completion_rate_percent:100,wilson_95_lower_percent:99.0019,manual_reconciled:381,manual_reconciled_completed:381,sample_supports_target:false},by_model:[],failure_categories:[]};
  const output=vm.runInContext('renderReliability(snapshot)',c);
  assert.ok(output.includes('自动完成率</span><strong>0.00%'));
  assert.ok(output.includes('0 / 381')&&output.includes('381 / 381'));
  assert.ok(output.includes('总交付成功率 100.00%'));
  assert.ok(output.includes('自动完成率的 95% 双侧 Wilson 下界 0.00%'));
  assert.ok(!output.includes('下界 99.00%'));
  assert.ok(output.includes('观测样本尚未支持 99% 目标'));
});
test('mutations carry CSRF and cookies while reads omit mutation headers',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,json:async()=>({items:[]})};});
  await vm.runInContext("state.csrf='csrf-test';api('/accounts',{method:'POST',body:{name:'a'}})",c);
  await vm.runInContext("api('/accounts')",c);
  assert.equal(calls[0].opts.headers['X-CSRF-Token'],'csrf-test');
  assert.equal(calls[0].opts.credentials,'same-origin');
  assert.equal(calls[0].opts.body,'{"name":"a"}');
  assert.equal(calls[1].opts.headers['X-CSRF-Token'],undefined);
});
test('no task data produces honest empty state instead of fabricated rows',()=>{
  const c=scope();const output=vm.runInContext("tasksTable([])",c);
  assert.ok(output.includes('还没有任务记录'));
  assert.ok(!output.includes('<tbody>'));
});
test('UI source stores only theme in localStorage, not API/admin secrets',()=>{
  assert.equal((source.match(/localStorage\.setItem\(/g)||[]).length,1);
  assert.ok(source.includes("localStorage.setItem('dreamina_admin_theme'"));
  assert.ok(!source.includes('document.cookie'));
});

test('API examples do not duplicate a configured v1 base path',()=>{
  const c=scope();
  const output=vm.runInContext("state.settings.api_base_url='https://gateway.example/v1/';state.docsTab='video';renderDocs();state.docsCode",c);
  assert.ok(output.startsWith('curl https://gateway.example/v1/videos'));
  assert.ok(!output.includes('/v1/v1/'));
  const image=vm.runInContext("state.docsTab='image';renderDocs();state.docsCode",c);
  assert.ok(image.startsWith('curl https://gateway.example/v1/images/generations'));
});

test('reference examples bind every prompt mention to an uploaded same-kind ordinal',()=>{
  const c=scope();
  for(const kind of ['images','video','mixed','json']){
    const result=vm.runInContext(`videoDocExample('https://gateway.example','${kind}')`,c);
    let prompt,counts;
    if(kind==='json'){
      const body=JSON.parse(result.code.split("  -d '")[1].slice(0,-1));
      prompt=body.prompt;
      counts={图片:body.references.filter(r=>r.type==='image').length,视频:body.references.filter(r=>r.type==='video').length};
      assert.ok(body.references.every(r=>r.data_url.startsWith('data:')));
    }else{
      prompt=result.code.match(/-F "prompt=([^"]+)"/)[1];
      counts={图片:(result.code.match(/-F "reference_images=@/g)||[]).length,视频:(result.code.match(/-F "reference_videos=@/g)||[]).length};
      assert.ok(!result.code.includes('Content-Type:'),'multipart boundary must be supplied by curl');
    }
    const mentions=[...prompt.matchAll(/@(图片|视频)([1-9][0-9]*)/g)];
    assert.ok(mentions.length>0);
    assert.ok(mentions.every(([,type,index])=>Number(index)<=counts[type]));
    vm.runInContext(`state.docsTab='video';state.docsExample='${kind}';renderDocs()`,c);
    assert.equal(vm.runInContext('state.docsCode',c),result.code.replace('https://gateway.example','http://localhost:8787'));
  }
  const notes=vm.runInContext('videoReferenceNotes()',c);
  assert.ok(notes.includes('分别从1编号')&&notes.includes('创建任务前返回400'));
});

test('unknown progress is indeterminate, real observed progress is numeric',()=>{
  const c=scope();
  const unknown=vm.runInContext("taskProgress({status:'in_progress',progress:null})",c);
  assert.ok(unknown.includes('indeterminate'));
  assert.ok(!unknown.includes('aria-valuenow'));
  const real=vm.runInContext("taskProgress({status:'in_progress',progress:57,progress_details:{source:'page'}})",c);
  assert.ok(real.includes('57%'));
  assert.ok(real.includes('aria-valuenow="57"'));
});

test('uncertain outcomes stop the indeterminate animation and expose a useful error without claiming cancellation',()=>{
  const c=scope();
  c.task={status:'uncertain',error:{code:'submission_unconfirmed',message:'<img src=x> outcome unknown'}};
  const progress=vm.runInContext('taskProgress(task)',c),issue=vm.runInContext('taskIssueSummary(task)',c);
  assert.ok(!progress.includes('indeterminate')&&progress.includes('结果未确认'));
  assert.ok(issue.includes('submission_unconfirmed')&&issue.includes('&lt;img src=x&gt;')&&!issue.includes('<img src=x>'));
  assert.ok(issue.includes('保留当前任务 ID')&&!issue.includes('已取消'));
  const normal=vm.runInContext("taskProgress({status:'in_progress'})",c);
  assert.ok(normal.includes('indeterminate'));
  assert.equal(vm.runInContext("taskIssueSummary({status:'completed'})",c),'');
});

test('a generation timeout with an original pending task stops waiting but never claims upstream cancellation',()=>{
  const c=scope();
  c.task={status:'failed',error:{code:'generation_timeout',message:'Wait exceeded'},metadata:{original_task_pending:true}};
  const issue=vm.runInContext('taskIssueSummary(task,true)',c),progress=vm.runInContext('taskProgress(task)',c);
  assert.ok(issue.includes('生成等待超时')&&issue.includes('后台继续查询原任务'));
  assert.ok(progress.includes('等待已超时')&&!progress.includes('indeterminate')&&!progress.includes('100%'));
  assert.ok(!issue.includes('已取消'));
});

test('server slot cap is distinguished from an unapplied account concurrency edit',()=>{
  const c=scope();c.account={max_concurrent:8,active_tasks:2,managed_worker:{effective_concurrency:4,active_slots:2,server_slot_limit:4,configuration_pending:false}};
  let output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('服务器单账号上限 4')&&output.includes('运行槽位受服务器上限约束'));
  assert.ok(!output.includes('配置待空闲后生效'));
  c.account.managed_worker.configuration_pending=true;output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('配置正在同步，等待节点确认')&&output.includes('已有任务继续执行'));
});

test('pending slot shrink and expansion remain distinct from confirmed running counts',()=>{
  const c=scope();c.account={max_concurrent:1,managed_worker:{state:'running',effective_concurrency:2,active_slots:1,server_slot_limit:4,configuration_pending:true}};
  let output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('运行标签页 1 / 2')&&output.includes('标签页缩减待生效：运行 2，目标 1'));
  assert.ok(output.includes('已有任务完成或核对后，关闭并清理多余槽位'));
  assert.ok(!output.includes('缩减已完成')&&!output.includes('标签页扩容待生效'));
  c.account.max_concurrent=4;output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('标签页扩容待生效：运行 2，目标 4')&&!output.includes('标签页缩减待生效'));
  c.account.managed_worker.target_concurrency=1;output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('标签页缩减待生效：运行 2，目标 1'));
});

test('stopped browser does not report previous configured slots as still running',()=>{
  const c=scope();c.account={max_concurrent:1,managed_worker:{state:'stopped',effective_concurrency:2,active_slots:1,configuration_pending:false}};
  const output=vm.runInContext('accountConcurrencySummary(account)',c);
  assert.ok(output.includes('运行标签页 0 / 0')&&output.includes('浏览器节点未运行'));
  assert.ok(!output.includes('运行标签页 1 / 2')&&!output.includes('配置与运行槽位不同'));
});

test('paused-account filtering is independent of enabled and online states',()=>{
  const c=scope();c.accounts=[{id:'low',enabled:true,online:true,scheduling_paused:true},{id:'disabled',enabled:false,scheduling_paused:true},{id:'ready',enabled:true,online:true}];
  const selected=JSON.parse(vm.runInContext("state.filter='paused';JSON.stringify(filtered(accounts,'accounts').map(a=>a.id))",c));
  assert.deepEqual(selected,['low']);
});

test('worker inventory counts virtual execution slots rather than inventing browser process counts',()=>{
  const c=scope();
  vm.runInContext("state.cache.workers={items:[{worker_id:'slot-a',account_id:'one',owner_id:'browser-one',slot_index:1,slot_count:2,online:true,ready:true,driver:'native'},{worker_id:'slot-b',account_id:'one',owner_id:'browser-one',slot_index:2,slot_count:2,online:true,ready:false,driver:'native'}]}",c);
  const output=vm.runInContext('renderWorkers()',c);
  assert.ok(output.includes('2 个执行槽位在线')&&output.includes('同一浏览器 · 槽位 1 / 2')&&output.includes('同一浏览器 · 槽位 2 / 2'));
  assert.ok(output.includes('并非浏览器进程数量')&&!output.includes('2 个节点在线'));
  const mini=vm.runInContext("updateWorkerMini();document.querySelector('#worker-mini').innerHTML",c);
  assert.ok(mini.includes('个执行槽位在线')&&!mini.includes('个浏览器节点在线'));
  assert.equal(vm.runInContext("workerSlotLabel({slot_index:0,slot_count:2})",c),'执行槽位');
});

test('offline heartbeat with stale ready flag cannot claim to allow new work',()=>{
  const c=scope();
  vm.runInContext("state.cache.workers={items:[{worker_id:'old-slot',account_id:'one',slot_index:2,slot_count:2,online:false,ready:true,driver:'native'}]}",c);
  const output=vm.runInContext('renderWorkers()',c);
  assert.ok(output.includes('0 个执行槽位在线')&&output.includes('已离线，不领取新任务'));
  assert.ok(!output.includes('允许领取任务'));
  assert.ok(output.includes('old-slot')&&!output.includes('离线记录（'));
});

test('parked slot readiness uses server ready rather than wakeable alone',()=>{
  const c=scope();c.worker={online:true,entry_parked:true,wakeable:true,ready:false};
  let output=vm.runInContext('workerReadinessBadge(worker)+workerTaskProtection(worker)',c);
  assert.ok(output.includes('休眠 · 待复验')&&!output.includes('可唤醒')&&!output.includes('允许领取'));
  c.worker.ready=true;c.worker.wakeable=false;
  output=vm.runInContext('workerReadinessBadge(worker)+workerTaskProtection(worker)',c);
  assert.ok(output.includes('休眠 · 可唤醒')&&output.includes('允许领取，提交前唤醒复验'));
  c.worker.online=false;
  output=vm.runInContext('workerReadinessBadge(worker)+workerTaskProtection(worker)',c);
  assert.ok(output.includes('离线')&&!output.includes('可唤醒')&&!output.includes('允许领取'));
  c.worker.online=true;c.worker.ready='true';
  output=vm.runInContext('workerReadinessBadge(worker)+workerTaskProtection(worker)',c);
  assert.ok(!output.includes('可唤醒')&&!output.includes('允许领取'));
});

test('parked account requires both online status and server admission permission',()=>{
  const c=scope();c.account={enabled:true,status:'ready',online:true,ready:true,managed_worker:{state:'running',worker_state:'browser_parked'}};
  assert.ok(vm.runInContext('accountStatusBadge(account)',c).includes('休眠 · 可唤醒'));
  c.account.online=false;
  assert.ok(vm.runInContext('accountStatusBadge(account)',c).includes('休眠 · 待复验'));
  c.account.online=true;c.account.ready=false;
  assert.ok(vm.runInContext('accountStatusBadge(account)',c).includes('休眠 · 待复验'));
  c.account.scheduling_paused=true;c.account.scheduling_pause_reason='upstream_risk_cooldown';
  assert.ok(vm.runInContext('accountStatusBadge(account)',c).includes('风控冷却'));
});

test('parked worker inventory reports target page and real verification without inventing current URL',()=>{
  const c=scope();
  vm.runInContext("state.cache.workers={items:[{worker_id:'parked-slot',account_id:'one',slot_index:1,slot_count:1,online:true,ready:true,entry_parked:true,wakeable:true,last_entry_ready_at:1000,driver:'native',page_path:'/ai-tool/generate',reason:'Ready'}]}",c);
  const output=vm.runInContext('renderWorkers()',c);
  assert.ok(output.includes('1 个执行槽位在线')&&output.includes('休眠 · 可唤醒'));
  assert.ok(output.includes('目标页面')&&output.includes('/ai-tool/generate')&&output.includes('最近页面核验'));
  assert.ok(output.includes('入口休眠或唤醒中；任务会先唤醒并复验。'));
  assert.ok(!output.includes('about:blank')&&!output.includes('实际页面'));
  vm.runInContext('state.cache.workers.items[0].last_entry_ready_at=null',c);
  assert.ok(vm.runInContext('renderWorkers()',c).includes('尚未取得'));
});

test('parked and waking browser states remain managed running nodes with normal stop protection',()=>{
  const c=scope();
  for(const state of ['browser_parked','browser_waking']){
    for(const worker of [{state:'running',worker_state:state},{state}]){
      c.account={id:'one',enabled:true,status:'ready',online:true,ready:state==='browser_parked',browser_profile_saved:true,max_concurrent:1,max_inflight:1,managed_worker:{...worker,effective_concurrency:1,active_slots:0}};
      const button=vm.runInContext('managedWorkerButton(account)',c),summary=vm.runInContext('accountConcurrencySummary(account)',c);
      assert.ok(button.includes('account-worker-stop')&&!button.includes('account-worker-start'));
      assert.ok(summary.includes('运行标签页 0 / 1')&&!summary.includes('节点未运行'));
      assert.ok(summary.includes(state==='browser_parked'?'浏览器空闲休眠':'正在唤醒浏览器并复验页面'));
      if(state==='browser_waking')assert.ok(vm.runInContext('accountStatusBadge(account)',c).includes('正在唤醒复验'));
      c.account.blocking_tasks=1;
      assert.ok(vm.runInContext('managedWorkerButton(account)',c).includes('<button disabled '));
    }
  }
});

test('cached node inventory is refreshed from the backend rather than hidden by local slot settings',async()=>{
  const calls=[];const c=scope(async(url)=>{calls.push(url);return {ok:true,status:200,json:async()=>({items:[{worker_id:'remaining',account_id:'a',online:true},{worker_id:'closing',account_id:'a',online:false}]})};});
  vm.runInContext("state.cache.workers={items:[{worker_id:'old',account_id:'a',online:true}]}",c);
  await vm.runInContext('refreshCachedWorkers()',c);
  assert.deepEqual(calls,['/api/admin/workers']);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(list(state.cache.workers).map(w=>w.worker_id))',c)),['remaining','closing']);
});

test('inventory refresh failure discards a stale cache without claiming confirmed cleanup',async()=>{
  const c=scope(async()=>({ok:false,status:503,json:async()=>({error:{message:'Temporary unavailable'}})}));
  vm.runInContext("state.cache.workers={items:[{worker_id:'old',account_id:'a',online:true}]}",c);
  await vm.runInContext('refreshCachedWorkers()',c);
  assert.equal(vm.runInContext("Object.hasOwn(state.cache,'workers')",c),false);
});

test('saved browser profiles expose managed node controls, busy nodes cannot stop',()=>{
  const c=scope();
  assert.equal(vm.runInContext("managedWorkerButton({id:'a',online:true})",c),'');
  const start=vm.runInContext("managedWorkerButton({id:'a',browser_profile_saved:true,managed_worker:{state:'stopped'}})",c);
  assert.ok(start.includes('account-worker-start'));
  const busy=vm.runInContext("managedWorkerButton({id:'a',browser_profile_saved:true,active_tasks:1,managed_worker:{state:'running'}})",c);
  assert.ok(busy.includes('account-worker-stop'));
  assert.ok(busy.includes('<button disabled '));
});

test('background-only known task permits idle stop and relogin but still prevents deletion',()=>{
  const c=scope();c.account={id:'one',name:'One',enabled:true,secret_configured:true,browser_profile_saved:true,active_tasks:1,blocking_tasks:0,managed_worker:{state:'running'}};
  const stop=vm.runInContext('managedWorkerButton(account)',c),login=vm.runInContext('accountLoginButton(account)',c),remove=vm.runInContext('accountDeleteButton(account)',c);
  assert.ok(stop.includes('account-worker-stop')&&!stop.includes('<button disabled '));
  assert.ok(login.includes('account-login')&&!login.includes('<button disabled '));
  assert.ok(remove.includes('<button disabled '));
  c.account.blocked_by_uncertain=true;
  assert.ok(!vm.runInContext('managedWorkerButton(account)',c).includes('<button disabled '));
  assert.ok(!vm.runInContext('accountLoginButton(account)',c).includes('<button disabled '));
  c.account.blocking_tasks=1;
  assert.ok(vm.runInContext('managedWorkerButton(account)',c).includes('<button disabled '));
  assert.ok(vm.runInContext('accountLoginButton(account)',c).includes('<button disabled '));
});

test('unknown and legacy outstanding tasks keep stop and relogin protected',async()=>{
  const c=scope();let calls=0;c.fetch=async()=>{calls++;throw new Error('No session request should be sent')};
  for(const extra of [{},{blocking_tasks:null},{blocking_tasks:-1},{blocking_tasks:NaN},{blocked_by_uncertain:true}]){
    c.account={id:'one',enabled:true,secret_configured:true,browser_profile_saved:true,active_tasks:1,managed_worker:{state:'running'},...extra};
    assert.ok(vm.runInContext('managedWorkerButton(account)',c).includes('<button disabled '));
    assert.ok(vm.runInContext('accountLoginButton(account)',c).includes('<button disabled '));
    vm.runInContext("state.cache.accounts={items:[account]};toast=()=>{};state.dialog=null",c);
    await vm.runInContext("openAccountLogin('one')",c);
    assert.equal(vm.runInContext('state.dialog',c),null);
  }
  assert.equal(calls,0);
});

test('account cooldowns render escaped model details only when present and never unblock automatically',()=>{
  let calls=0;const c=scope(async()=>{calls++;return {ok:true,status:200,json:async()=>({ok:true})}});
  assert.equal(vm.runInContext("accountCooldowns({id:'a'})",c),'');
  c.account={id:'a',model_blocks:[{model:'<img src=x onerror=alert(1)>',reason:'secret-not-shown',blocked_until:1793000000}]};
  const html=vm.runInContext('accountCooldowns(account)',c);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!html.includes('<img src=x')&&!html.includes('secret-not-shown'));
  assert.ok(html.includes('本地冷却至')&&html.includes('解除冷却'));
  assert.ok(html.includes('不会更改网站套餐或权限'));
  assert.equal(calls,0);
});

test('explicit cooldown reset uses encoded account and model segments with CSRF exactly once',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,json:async()=>({ok:true})}});
  vm.runInContext("state.csrf='synthetic-csrf';navigate=async()=>{};toast=()=>{};globalThis.control={disabled:false,isConnected:true}",c);
  await vm.runInContext("clearModelCooldown('account one','seedance/2.5',control)",c);
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/admin/accounts/account%20one/model-blocks/seedance%2F2.5');
  assert.equal(calls[0].opts.method,'DELETE');
  assert.equal(calls[0].opts.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.equal(calls[0].opts.credentials,'same-origin');
});

test('failed cooldown reset leaves retry control enabled and does not pretend permissions changed',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:false,status:503,json:async()=>({error:{message:'Synthetic unavailable'}})}});
  vm.runInContext("state.csrf='synthetic-csrf';navigate=async()=>{throw new Error('must not refresh after failure')};toast=()=>{};globalThis.control={disabled:false,isConnected:true}",c);
  await vm.runInContext("clearModelCooldown('one','seedance-2.5',control)",c);
  assert.equal(calls.length,1);
  assert.equal(c.control.disabled,false);
});

test('account and key deletion remain distinct from disabling and hide deleted rows',()=>{
  const c=scope();
  const accounts=vm.runInContext("accountsTable([{id:'live',name:'Live',region:'US',enabled:true,max_concurrent:1},{id:'gone',name:'Deleted',deleted_at:123,max_concurrent:1}])",c);
  assert.ok(accounts.includes('data-action="account-delete"')&&accounts.includes('data-action="account-toggle"'));
  assert.ok(!accounts.includes('data-id="gone"'));
  const keys=vm.runInContext("keysTable([{id:'live-key',name:'Key',enabled:true},{id:'gone-key',name:'Old',deleted_at:123}])",c);
  assert.ok(keys.includes('data-action="key-delete"')&&keys.includes('data-action="key-toggle"'));
  assert.ok(!keys.includes('gone-key'));
});

test('busy login and uncertain account deletion are blocked before a request',()=>{
  const c=scope();
  for(const properties of [{active_tasks:1},{status:'login_pending'},{blocked_by_uncertain:true},{status:'uncertain'}]){
    c.account={id:'a',name:'Account',...properties};
    const output=vm.runInContext('accountDeleteButton(account)',c);
    assert.ok(output.includes('<button disabled '));
    vm.runInContext("state.cache.accounts={items:[account]};state.dialog=null;toast=()=>{};confirmDelete('account','a')",c);
    assert.equal(vm.runInContext('state.dialog',c),null);
  }
});

test('delete confirmation escapes names and explains retained history and irreversible keys',()=>{
  const c=scope();
  const html=vm.runInContext("state.cache.keys={items:[{id:'k',name:'<img src=x onerror=alert(1)>'}]};confirmDelete('key','k');document.querySelector('#modal').innerHTML",c);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!html.includes('<img src=x'));
  assert.ok(html.includes('立即失效')&&html.includes('不能恢复使用')&&html.includes('历史任务和结果保留'));
  const account=vm.runInContext("state.cache.accounts={items:[{id:'a',name:'Account'}]};confirmDelete('account','a');document.querySelector('#modal').innerHTML",c);
  assert.ok(account.includes('查询所需的加密凭据和浏览器资料也会保留'));
  assert.ok(account.includes('停止此账号对应的浏览器节点并清理节点登记'));
  assert.ok(account.includes('节点未能停止时不会确认删除'));
  assert.ok(account.includes('data-action="confirm-delete"'));
});

test('confirmed account deletion also removes its cached nodes without touching other accounts',async()=>{
  const c=scope(async()=>({ok:true,status:200,json:async()=>({deleted:true})}));
  vm.runInContext("state.page='accounts';state.cache.accounts={items:[{id:'a',name:'A'},{id:'b',name:'B'}]};state.cache.workers={items:[{worker_id:'a-one',account_id:'a',online:true},{worker_id:'a-two',account_id:'a',online:false},{worker_id:'b-one',account_id:'b',online:true}]};state.dialog={kind:'delete',resource:'account',id:'a'};navigate=async()=>{};refreshTable=()=>{};toast=()=>{};globalThis.control={disabled:false,isConnected:true}",c);
  await vm.runInContext('doDelete(control)',c);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(list(state.cache.accounts).map(a=>a.id))',c)),['b']);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(list(state.cache.workers).map(w=>w.worker_id))',c)),['b-one']);
  const mini=vm.runInContext("updateWorkerMini();document.querySelector('#worker-mini').innerHTML",c);
  assert.ok(mini.includes('1 个执行槽位在线'));
});

test('account deleted with node cleanup pending reports pending and uses authoritative inventory',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,json:async()=>opts.method==='DELETE'?{deleted:true,nodes_cleanup_pending:true}:{items:[{worker_id:'a-pending',account_id:'a',online:false},{worker_id:'b-ready',account_id:'b',online:true}]}};});
  vm.runInContext("state.page='accounts';state.cache.accounts={items:[{id:'a'},{id:'b'}]};state.cache.workers={items:[{worker_id:'a-old',account_id:'a',online:true}]};state.dialog={kind:'delete',resource:'account',id:'a'};navigate=async()=>{};refreshTable=()=>{};globalThis.messages=[];toast=(message)=>messages.push(message);globalThis.control={disabled:false,isConnected:true}",c);
  await vm.runInContext('doDelete(control)',c);
  assert.deepEqual(calls.map(x=>x.url),['/api/admin/accounts/a','/api/admin/workers']);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(list(state.cache.accounts).map(a=>a.id))',c)),['b']);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(list(state.cache.workers).map(w=>w.worker_id))',c)),['a-pending','b-ready']);
  assert.deepEqual(Array.from(c.messages),['账号已删除，节点清理待完成']);
  assert.ok(!c.messages.some(message=>message.includes('对应节点已删除')));
});

test('confirmed delete sends one scoped CSRF request and removes only that cached resource',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:true,status:200,json:async()=>({deleted:true})};});
  vm.runInContext("state.csrf='synthetic-csrf';state.page='keys';state.cache.keys={items:[{id:'key/a',name:'A'},{id:'keep',name:'B'}]};state.dialog={kind:'delete',resource:'key',id:'key/a'};navigate=async()=>{};toast=()=>{};globalThis.control={disabled:false,isConnected:true}",c);
  await vm.runInContext('doDelete(control)',c);
  await vm.runInContext('doDelete(control)',c);
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/admin/keys/key%2Fa');
  assert.equal(calls[0].opts.method,'DELETE');assert.equal(calls[0].opts.headers['X-CSRF-Token'],'synthetic-csrf');
  assert.equal(calls[0].opts.credentials,'same-origin');
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(list(state.cache.keys).map(x=>x.id))',c)),['keep']);
});

test('failed delete keeps cache and confirmation and re-enables retry',async()=>{
  const calls=[];const c=scope(async(url,opts)=>{calls.push({url,opts});return {ok:false,status:409,json:async()=>({error:{message:'Account has active work'}})};});
  vm.runInContext("state.csrf='synthetic-csrf';state.page='accounts';state.cache.accounts={items:[{id:'a',name:'A'}]};state.cache.workers={items:[{worker_id:'a-one',account_id:'a',online:true}]};state.dialog={kind:'delete',resource:'account',id:'a'};navigate=async()=>{throw new Error('must not refresh')};toast=()=>{};globalThis.control={disabled:false,isConnected:true}",c);
  await vm.runInContext('doDelete(control)',c);
  assert.equal(calls.length,1);assert.equal(c.control.disabled,false);
  assert.equal(vm.runInContext('state.dialog.kind',c),'delete');
  assert.equal(vm.runInContext('list(state.cache.accounts).length',c),1);
  assert.equal(vm.runInContext('list(state.cache.workers).length',c),1);
});

test('old disable-only DELETE response is never presented as confirmed deletion',async()=>{
  const c=scope(async()=>({ok:true,status:200,json:async()=>({id:'k',enabled:false})}));
  vm.runInContext("state.cache.keys={items:[{id:'k',name:'Key'}]};state.dialog={kind:'delete',resource:'key',id:'k'};navigate=async()=>{throw new Error('must not refresh')};toast=()=>{};globalThis.control={disabled:false,isConnected:true}",c);
  await vm.runInContext('doDelete(control)',c);
  assert.equal(c.control.disabled,false);
  assert.equal(vm.runInContext('state.dialog.kind',c),'delete');
  assert.equal(vm.runInContext('list(state.cache.keys).length',c),1);
});

test('editing a restricted key keeps deleted account IDs as explicit selected choices',()=>{
  const c=scope();
  vm.runInContext("state.cache.accounts={items:[{id:'ready',name:'Ready'},{id:'deleted',name:'Old',deleted_at:123}]}",c);
  const choices=JSON.parse(vm.runInContext("JSON.stringify(keyAccountChoices(['deleted','missing']))",c));
  assert.deepEqual(choices.map(x=>x.id),['ready','deleted','missing']);
  assert.equal(choices[1].unavailable,true);assert.ok(choices[1].name.includes('已删除或不可用'));
  assert.equal(choices[2].unavailable,true);
});

test('credit preflight shows known zero values and never changes unknown values into zero',()=>{
  const c=scope();
  c.task={events:[{stage:'credit_preflight',created_at:1,detail:{account_id:'a',required_credits:0,balance:20,allowed:true}}]};
  let output=vm.runInContext('taskCreditSummary(task)',c);
  assert.ok(output.includes('0 积分')&&output.includes('20 积分')&&output.includes('余额满足当前报价'));
  c.task={events:[{stage:'credit_quote_unavailable',created_at:1,detail:{account_id:'a',required_credits:null,balance:null}}]};
  output=vm.runInContext('taskCreditSummary(task)',c);
  assert.ok(output.includes('未确认')&&!output.includes('0 积分')&&!output.includes('余额满足当前报价'));
  c.task={events:[{stage:'credit_preflight',created_at:1,detail:{required_credits:'20',balance:Infinity,allowed:true}}]};
  output=vm.runInContext('taskCreditSummary(task)',c);assert.ok(!output.includes('余额满足当前报价')&&!output.includes('20 积分'));
});

test('credit summary escapes account identities and preserves reassignment history',()=>{
  const c=scope();c.task={events:[
    {stage:'credit_insufficient',created_at:1,detail:{detail:{account_id:'<img src=x>',required_credits:68,balance:20,allowed:false}}},
    {stage:'account_reassigned',detail:{from_account_id:'<img src=x>',to_account_id:'account-two',reason:'insufficient_credits'}},
    {stage:'credit_preflight',created_at:2,detail:{account_id:'account-two',required_credits:68,balance:100,allowed:true}},
    {stage:'credit_failover_exhausted',detail:{tried_account_ids:['a','b']}},
  ]};
  const output=vm.runInContext('taskCreditSummary(task)',c);
  assert.ok(output.includes('&lt;img src=x&gt;')&&!output.includes('<img src=x>'));
  assert.ok(output.includes('account-two')&&output.includes('68 积分')&&output.includes('100 积分'));
  assert.ok(output.includes('当前没有可继续切换的合适账号'));
  assert.equal(vm.runInContext("taskEventLabel('account_reassigned')",c),'生成前切换账号');
  assert.equal(vm.runInContext("taskEventLabel('new_future_event')",c),'new_future_event');
  assert.equal(vm.runInContext('taskCreditSummary({events:[]})',c),'');
});

test('retry summary displays the same task attempt and its scheduled time',()=>{
  const c=scope();c.task={id:'video_original',status:'queued',request_detail:{submission_retry:{attempt:2,max_attempts:2,stage:'waiting',next_retry_at:3000000000,reason:'temporary_busy'}}};
  const output=vm.runInContext('taskRetrySummary(task,true)',c);
  assert.ok(output.includes('等待新会话重试')&&output.includes('第 2 / 2 次尝试')&&output.includes('计划 '));
  assert.ok(output.includes('temporary_busy')&&output.includes('对外任务 ID 保持不变'));
  assert.ok(output.includes('总时限')&&output.includes('总费用上限'));
  assert.equal(vm.runInContext('taskRetrySummary({status:"queued"},true)',c),'');
});

test('retry summary supports public metadata without exposing arbitrary fields',()=>{
  const c=scope();c.task={status:'in_progress',metadata:{submission_retry:{attempt:2,max_attempts:2,stage:'preparing',next_retry_at:null,reason:'ready_to_retry',cookie:'private-cookie',token:'private-token'}}};
  const output=vm.runInContext('taskRetrySummary(task,true)',c);
  assert.ok(output.includes('新会话准备中')&&output.includes('第 2 / 2 次尝试'));
  assert.ok(!output.includes('计划 ')&&!output.includes('private-cookie')&&!output.includes('private-token'));
});

test('ended or uncertain tasks never advertise a stale scheduled resubmission',()=>{
  const c=scope();
  for(const [status,label] of [['completed','任务已完成'],['failed','重试已结束'],['cancelled','重试已结束'],['uncertain','原任务待核对，停止重新提交']]){
    c.task={status,request_detail:{submission_retry:{attempt:2,max_attempts:2,stage:'waiting',next_retry_at:3000000000}}};
    const output=vm.runInContext('taskRetrySummary(task,true)',c);
    assert.ok(output.includes(label));assert.ok(!output.includes('等待新会话重试')&&!output.includes('计划 '));
  }
  c.task={status:'in_progress',metadata:{verification_required:true,submission_retry:{attempt:2,max_attempts:2,stage:'preparing'}}};
  const output=vm.runInContext('taskRetrySummary(task)',c);assert.ok(output.includes('停止重新提交')&&!output.includes('新会话准备中'));
});

test('retry history retains failed attempts and known zero quote but omits credentials',()=>{
  const c=scope();c.task={status:'in_progress',request_detail:{submission_retry:{attempt:2,max_attempts:2,stage:'preparing'},submission_attempts:[{
    attempt:1,account_id:'account-one',submit_id:'upstream-one',conversation_id:'session-one',bridge_job_id:'job-one',quote_credits:0,started_at:10,ended_at:20,
    error:{code:'server_busy',message:'Temporary rejection',cookie:'must-not-show'},token:'secret-token',cookie:'secret-cookie',request_headers:{Authorization:'secret-auth'},
  }]}};
  const output=vm.runInContext('taskRetrySummary(task,true)',c);
  for(const text of ['第 1 次尝试','account-one','upstream-one','session-one','0 积分','server_busy','Temporary rejection'])assert.ok(output.includes(text));
  for(const text of ['must-not-show','secret-token','secret-cookie','secret-auth'])assert.ok(!output.includes(text));
  assert.ok(output.includes('报价不是实际扣费'));
});

test('retry history and reason escape markup and invalid quote data remains unknown',()=>{
  const c=scope();c.task={status:'failed',request_detail:{submission_retry:{attempt:2,max_attempts:2,stage:'exhausted',reason:'<img src=x onerror=alert(1)>'},submission_attempts:[null,[],{
    attempt:1,account_id:'<script>account</script>',submit_id:'<img src=x>',conversation_id:'<svg onload=alert(1)>',quote_credits:'20',started_at:NaN,ended_at:Infinity,error:{code:'<b>busy</b>',message:'<script>error</script>'},
  }]}};
  const output=vm.runInContext('taskRetrySummary(task,true)',c);
  assert.ok(output.includes('&lt;script&gt;account&lt;/script&gt;')&&output.includes('&lt;svg onload=alert(1)&gt;'));
  assert.ok(output.includes('&lt;script&gt;error&lt;/script&gt;')&&!output.includes('<img src=x')&&!output.includes('<script>'));
  assert.ok(output.includes('未确认')&&!output.includes('20 积分')&&!output.includes('Invalid Date'));
});

test('malformed retry metadata does not invent attempt counts or timestamps',()=>{
  const c=scope();
  for(const retry of [null,[],false,'waiting']){
    c.task={status:'queued',request_detail:{submission_retry:retry}};assert.equal(vm.runInContext('taskRetrySummary(task,true)',c),'');
  }
  c.task={status:'queued',request_detail:{submission_retry:{attempt:0,max_attempts:Infinity,stage:'waiting',next_retry_at:'3000000000'}}};
  const output=vm.runInContext('taskRetrySummary(task,true)',c);
  assert.ok(output.includes('第 未确认 / 未确认 次尝试')&&!output.includes('计划 ')&&!output.includes('Infinity'));
});

test('waiting retries discard stale progress and appear in task rows',()=>{
  const c=scope();c.task={id:'video_same',kind:'video',model:'mini',status:'queued',created_at:1,progress:88,progress_details:{stage:'generating'},request_detail:{submission_retry:{attempt:2,max_attempts:2,stage:'waiting',next_retry_at:3000000000}}};
  let output=vm.runInContext('taskProgress(task)',c);
  assert.ok(output.includes('等待新会话重试')&&!output.includes('aria-valuenow="88"')&&output.includes('indeterminate'));
  output=vm.runInContext('tasksTable([task])',c);assert.ok(output.includes('第 2 / 2 次尝试')&&output.includes('video_same'));
  c.task.status='completed';output=vm.runInContext('taskProgress(task)',c);assert.ok(output.includes('aria-valuenow="100"')&&!output.includes('indeterminate'));
});

test('task detail includes retry history and labels new retry events',async()=>{
  const c=scope(async()=>({ok:true,status:200,json:async()=>({id:'video_same',status:'queued',events:[{stage:'submission_retry_scheduled',created_at:1,detail:{}}],request_detail:{submission_retry:{attempt:2,max_attempts:2,stage:'waiting'}}})}));
  await vm.runInContext("taskDialog('video_same')",c);
  const output=vm.runInContext('modal.innerHTML',c);assert.ok(output.includes('新会话重试')&&output.includes('第 2 / 2 次尝试')&&output.includes('计划新会话重试'));
  assert.equal(vm.runInContext("taskEventLabel('submission_retry_exhausted')",c),'提交重试次数已用完');
  assert.equal(vm.runInContext("taskEventLabel('submission_retry_stopped')",c),'停止新会话重试');
});

test('video API docs explain bounded retries and the unchanged query ID',()=>{
  const c=scope();const output=vm.runInContext('renderDocs()',c);
  assert.ok(output.includes('metadata.submission_retry')&&output.includes('最多再提交一次')&&output.includes('继续查询同一 ID'));
  assert.ok(output.includes('ret=-6')&&output.includes('结果不明不自动重新生成'));
});

const loginSource=fs.readFileSync(path.join(__dirname,'login-panel.js'),'utf8');
test('proxy navigation and direct-mode policy are explicit',()=>{
  const c=scope();vm.runInContext("state.page='proxies';state.proxySupported=true;state.settings.account_proxy_required=true;state.cache.proxies={items:[]}",c);
  assert.equal(vm.runInContext('pageTitle()',c),'代理 IP');assert.ok(vm.runInContext('shell()',c).includes('#/proxies'));
  let text=vm.runInContext('renderProxies()',c);assert.ok(text.includes('绝不回退直连')&&text.includes('添加代理'));
  vm.runInContext('state.settings.account_proxy_required=false',c);text=vm.runInContext('renderProxies()',c);assert.ok(text.includes('显式允许未绑定账号直连'));
});

test('proxy lists and editors never echo stored credentials or raw proxy URLs',()=>{
  const c=scope();c.fixture={id:'proxy-1',name:'<img src=x>',protocol:'http',host:'proxy.example',port:8080,status:'active',has_username:true,has_password:true,username:'private-user',password:'private-password',password_encrypted:'private-cipher',url:'http://private-user:private-password@proxy.example:8080',bound_accounts_count:1,max_accounts:3};
  vm.runInContext('state.cache.proxies={items:[fixture]};globalThis.result=proxiesTable([fixture]);proxyDialog(fixture.id)',c);
  const output=c.result+vm.runInContext('modal.innerHTML',c);
  assert.ok(output.includes('&lt;img src=x&gt;')&&!output.includes('<img src=x>'));
  for(const secret of ['private-user','private-password','private-cipher'])assert.ok(!output.includes(secret));
  assert.ok(output.includes('认证已配置')&&output.includes('type="password"')&&output.includes('留空保留原凭据'));
  assert.ok(output.includes('disabled title="仍有账号绑定，不能删除"'));
});

test('proxy addresses and error text do not turn malformed host fields into credential output',()=>{
  const c=scope();c.proxy={host:'user:secret@proxy.example',port:80};
  assert.equal(vm.runInContext('proxyAuthority(proxy)',c),'地址未提供');
  c.error={message:'Cannot connect to socks5://name:secret@proxy.example:1080 password=secret'};
  const message=vm.runInContext('proxyErrorText(error)',c);assert.ok(!message.includes('name:secret')&&!message.includes('password=secret'));assert.ok(message.includes('proxy.example'));
  c.proxy={host:'2001:db8::1',port:1080};assert.equal(vm.runInContext('proxyAuthority(proxy)',c),'[2001:db8::1]:1080');
});

test('proxy state distinguishes untested, failed, disabled and expired endpoints',()=>{
  const c=scope();
  for(const [p,label] of [[{status:'active'},'待检测'],[{status:'failing'},'探测失败'],[{status:'disabled',exit_ip:'203.0.113.1'},'已停用'],[{status:'active',expires_at:1},'已过期'],[{status:'active',exit_ip:'203.0.113.1',last_checked_at:1},'可用']]){
    c.proxy=p;assert.ok(vm.runInContext('proxyStatus(proxy)',c).includes(label));
  }
});

test('invalid migrated proxy is never presented as an explicitly allowed direct account',()=>{
  const c=scope();vm.runInContext('state.proxySupported=true;state.settings.account_proxy_required=false',c);
  c.account={proxy_scheduling_error:'proxy_migration_invalid'};
  let output=vm.runInContext('accountEgressSummary(account)',c);
  assert.ok(output.includes('出口待处理，暂停调度')&&!output.includes('直连'));
  c.account={proxy_id:'broken',proxy_scheduling_error:'proxy_unverified'};
  output=vm.runInContext('accountEgressSummary(account)',c);assert.ok(output.includes('broken')&&output.includes('出口待处理，暂停调度'));
});

test('proxy form sends new credentials once but leaves existing credentials untouched on blank edit',()=>{
  const c=scope();vm.runInContext("FormData=class{constructor(form){this.v=form}get(n){return this.v[n]??null}}",c);
  c.form={proxy_name:'A',url:'',max_accounts:'3',notes:'memo',expiry_warn_days:'7',fallback_mode:'none',backup_proxy_id:'',expires_at:''};
  let result=vm.runInContext('proxyFormBody(form,true)',c);assert.equal(result.url,undefined);assert.equal(result.max_accounts,3);assert.equal(result.backup_proxy_id,null);
  assert.throws(()=>vm.runInContext('proxyFormBody(form,false)',c),/连接信息/);
  c.form.url='http://u:p@proxy.example:8080';result=vm.runInContext('proxyFormBody(form,false)',c);assert.equal(result.url,c.form.url);
  c.form.url='file:///etc/passwd';assert.throws(()=>vm.runInContext('proxyFormBody(form,false)',c),/格式/);
  c.form.url='http://x:80';c.form.fallback_mode='direct';assert.throws(()=>vm.runInContext('proxyFormBody(form,true)',c),/备用代理/);
});

test('required proxy account creation validates before network and offers proxy before login',async()=>{
  const calls=[],c=scope(async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>({id:'account-new'})}});
  vm.runInContext("state.proxySupported=true;state.settings.account_proxy_required=true;FormData=class{constructor(form){this.v=form.values}get(n){return this.v[n]??null}getAll(n){return this.v[n]||[]}has(n){return !!this.v[n]}};globalThis.errorNode={textContent:''};globalThis.form={isConnected:true,values:{id:'account-new',name:'New',region:'US',enabled:true,priority:'0',max_concurrent:'1',max_inflight:'1',min_credit_balance:'0',models:['seedance-2.0-mini']},querySelector(){return errorNode}}",c);
  await vm.runInContext('saveAccount({preventDefault(){},currentTarget:form})',c);assert.equal(calls.length,0);assert.match(c.errorNode.textContent,/先选择/);
  const html=vm.runInContext('accountProxyField({},false)',c);assert.ok(html.includes('name="proxy_id" required')&&html.includes('登录与后续任务使用同一出口'));
});

test('editing account changes proxy through the binding endpoint rather than ordinary PATCH',async()=>{
  const calls=[],c=scope(async(path,options={})=>{calls.push({path,options});return {ok:true,status:200,json:async()=>path.endsWith('/proxies')?{items:[]}:{id:'one'}}});
  vm.runInContext("state.proxySupported=true;state.settings.account_proxy_required=true;state.csrf='csrf';state.cache.accounts={items:[{id:'one',proxy_id:'old'}]};navigate=async()=>{};toast=()=>{};FormData=class{constructor(f){this.v=f.values}get(n){return this.v[n]??null}getAll(n){return this.v[n]||[]}has(n){return !!this.v[n]}};globalThis.form={isConnected:true,values:{name:'One',region:'US',enabled:true,priority:'0',max_concurrent:'1',max_inflight:'1',min_credit_balance:'0',models:['seedance-2.0-mini'],proxy_id:'new'},querySelector(){return {textContent:''}}}",c);
  await vm.runInContext("saveAccount({preventDefault(){},currentTarget:form},'one')",c);
  assert.equal(calls.length,2);assert.equal(calls[0].options.method,'PATCH');assert.equal(JSON.parse(calls[0].options.body).proxy_id,undefined);
  assert.ok(calls[1].path.endsWith('/accounts/one/proxy'));assert.deepEqual(JSON.parse(calls[1].options.body),{proxy_id:'new',confirm_country_change:false,confirm_capacity_override:false});
});

test('proxy binding confirmation is never automatically approved and preserves only the confirmed flag',async()=>{
  const calls=[];let attempts=0;
  const c=scope(async(path,options={})=>{calls.push({path,options});if(options.method==='POST')return {ok:false,status:409,json:async()=>({error:{code:attempts++===0?'proxy_country_confirmation_required':'proxy_capacity_confirmation_required',message:'Confirm'}})};return {ok:true,status:200,json:async()=>({items:[]})}});
  vm.runInContext("state.csrf='csrf';globalThis.control={disabled:false,isConnected:true};toast=()=>{}",c);
  assert.equal(await vm.runInContext("bindAccountProxy('account/a','proxy-new')",c),false);assert.equal(calls.length,1);
  assert.equal(vm.runInContext('state.dialog.confirmation',c),'confirm_country_change');
  await vm.runInContext('confirmPendingProxyBinding(control)',c);
  assert.equal(calls.length,2);assert.deepEqual(JSON.parse(calls[1].options.body),{proxy_id:'proxy-new',confirm_country_change:true,confirm_capacity_override:false});
  assert.equal(vm.runInContext('state.dialog.confirmation',c),'confirm_capacity_override');
  assert.equal(vm.runInContext('state.dialog.flags.confirm_capacity_override',c),false);
});

test('proxy creation with an over-capacity confirmation retains the same account ID',async()=>{
  const calls=[],c=scope(async(path,options)=>{calls.push({path,options});return {ok:true,status:201,json:async()=>({id:'stable-account'})}});
  vm.runInContext("state.csrf='csrf';navigate=async()=>{};toast=()=>{};openAccountLogin=async id=>{globalThis.loginId=id};globalThis.control={disabled:false,isConnected:true};confirmCreateWithProxy({id:'stable-account',name:'Name',proxy_id:'p',confirm_capacity_override:false})",c);
  assert.equal(calls.length,0);await vm.runInContext('doConfirmCreateWithProxy(control)',c);
  assert.equal(calls.length,1);const body=JSON.parse(calls[0].options.body);assert.equal(body.id,'stable-account');assert.equal(body.confirm_capacity_override,true);assert.equal(c.loginId,'stable-account');
});

test('account risk cooldown is separate from a model permission block',()=>{
  const c=scope();c.account={id:'a',risk_cooldown_until:Date.now()/1000+300,model_blocks:[{model:'seedance-2.0',reason:'upstream_risk_cooldown',blocked_until:Date.now()/1000+300},{model:'seedance-2.5',reason:'account_plan_required',blocked_until:Date.now()/1000+900}]};
  const output=vm.runInContext('accountCooldowns(account)',c);assert.ok(output.includes('账号风控冷却（全部模型）')&&output.includes('data-action="account-risk-unblock"'));
  assert.equal((output.match(/account-model-unblock/g)||[]).length,1);assert.ok(output.includes('seedance-2.5'));
});

test('risk cooldown release requires a note and uses the dedicated account endpoint',async()=>{
  const calls=[],c=scope(async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>({ok:true})}});
  vm.runInContext("state.csrf='csrf';navigate=async()=>{};toast=()=>{};globalThis.buttonNode={disabled:false,isConnected:true};globalThis.errorNode={textContent:''};globalThis.form={elements:{note:{value:''}},querySelector(s){return s==='[type=submit]'?buttonNode:errorNode}}",c);
  await vm.runInContext("submitRiskCooldown({preventDefault(){},currentTarget:form},'a/b')",c);assert.equal(calls.length,0);
  c.form.elements.note.value='人工核对后恢复本地调度';await vm.runInContext("submitRiskCooldown({preventDefault(){},currentTarget:form},'a/b')",c);
  assert.equal(calls.length,1);assert.ok(calls[0].path.endsWith('/accounts/a%2Fb/risk-cooldown'));assert.equal(calls[0].options.method,'DELETE');assert.equal(calls[0].options.headers['X-CSRF-Token'],'csrf');
});

test('submission gate release stays disabled unless the server confirms safe release',()=>{
  const c=scope();
  for(const gate of [{awaiting_receipt:true},{can_release:false},{can_release:'true'},{release_allowed:true}]){c.gate=gate;assert.equal(vm.runInContext('gateReleaseAllowed(gate)',c),false);}
  c.gate={can_release:true};assert.equal(vm.runInContext('gateReleaseAllowed(gate)',c),true);
  c.monitor={metrics:{},pacing:[{scope:'account:a',job_id:'job',awaiting_receipt:true,can_release:false},{scope:'egress:proxy-one',job_id:'job2',can_release:true}],alerts:[]};
  const output=vm.runInContext('renderMonitoring(monitor)',c);assert.ok(output.includes('data-scope="account:a" disabled'));assert.ok(output.includes('data-scope="egress:proxy-one"  title='));
});

test('manual gate release sends only note and scope, never task or credit override fields',async()=>{
  const calls=[],c=scope(async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>({released:true})}});
  vm.runInContext("state.csrf='csrf';navigate=async()=>{};toast=()=>{};globalThis.buttonNode={disabled:false,isConnected:true};globalThis.form={elements:{note:{value:'已核对旧页面关闭证明'}},querySelector(s){return s==='[type=submit]'?buttonNode:{textContent:''}}}",c);
  await vm.runInContext("submitGateRelease({preventDefault(){},currentTarget:form},'egress:proxy-one')",c);
  assert.equal(calls.length,1);assert.ok(calls[0].path.endsWith('/submission-gates/egress%3Aproxy-one/release'));
  assert.deepEqual(JSON.parse(calls[0].options.body),{note:'已核对旧页面关闭证明'});assert.equal(calls[0].options.headers['X-CSRF-Token'],'csrf');
});

test('monitor proxy health and account performance render optional fields without secrets',()=>{
  const c=scope();c.monitor={metrics:{event_loop_lag_ms:12,process_memory_bytes:104857600,disk_used_percent:81},proxies:[{id:'p',name:'Proxy',status:'active',exit_ip:'203.0.113.1',country:'US',last_checked_at:1,latency_ms:20,password:'secret-should-not-render'}],account_metrics:[{account_id:'<b>x</b>',completed:8,terminal:10,success_rate_percent:80,preparation_p50_seconds:3,preparation_p95_seconds:6,generation_p50_seconds:120,generation_p95_seconds:200,risk_rejections:1,audio_rejections:1}]};
  const output=vm.runInContext('renderMonitoring(monitor)',c);assert.ok(output.includes('事件循环延迟 12 ms')&&output.includes('100.0 MiB')&&output.includes('磁盘使用 81%'));
  assert.ok(output.includes('203.0.113.1')&&output.includes('&lt;b&gt;x&lt;/b&gt;')&&!output.includes('secret-should-not-render'));
});

test('monitoring separates API memory from browser RSS and labels shared-page overcount',()=>{
  const c=scope();c.monitor={metrics:{process_memory_bytes:104857600,browser_memory_bytes:536870912,browser_processes:13,process_cmdline:'must-not-render'}};
  const output=vm.runInContext('renderMonitoring(monitor)',c);
  assert.ok(output.includes('API 进程内存 100.0 MiB')&&output.includes('托管浏览器 RSS 合计 512.0 MiB')&&output.includes('浏览器进程 13'));
  assert.ok(output.includes('共享内存页可能重复计数')&&output.includes('上界估计')&&output.includes('不代表整台服务器'));
  assert.ok(!output.includes('must-not-render')&&!output.includes('612.0 MiB'));
});

test('browser memory unknown, absent and zero observations remain distinct',()=>{
  const c=scope();let output=vm.runInContext('renderMonitoring({metrics:{browser_memory_bytes:null,browser_processes:null}})',c);
  assert.ok(output.includes('托管浏览器 RSS 合计 未知')&&output.includes('浏览器进程 未知'));
  output=vm.runInContext('renderMonitoring({metrics:{browser_memory_bytes:0,browser_processes:0}})',c);
  assert.ok(output.includes('托管浏览器 RSS 合计 0.0 MiB')&&output.includes('浏览器进程 0'));
  output=vm.runInContext('renderMonitoring({metrics:{}})',c);assert.ok(!output.includes('浏览器 RSS'));
  output=vm.runInContext('renderMonitoring({metrics:{browser_memory_bytes:-1,browser_processes:1.2}})',c);
  assert.ok(output.includes('托管浏览器 RSS 合计 未知')&&output.includes('浏览器进程 未知'));
});

function fakeElement(){
  const listeners={},classes=new Set();
  return {listeners,children:[],dataset:{},style:{},hidden:false,disabled:false,value:'',textContent:'',innerHTML:'',
    addEventListener(event,fn){listeners[event]=fn},
    append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},
    removeAttribute(key){delete this[key]},focus(){listeners.focus?.({})},blur(){listeners.blur?.({})},
    getBoundingClientRect(){return {left:10,top:20,width:550,height:380}},
    classList:{add(value){classes.add(value)},remove(value){classes.delete(value)},contains(value){return classes.has(value)},toggle(value){classes.has(value)?classes.delete(value):classes.add(value)}}};
}
function remoteScope({api=async()=>({}),fetchImpl=async()=>({ok:true,headers:{get(){return 'image/jpeg'}},blob:async()=>({})}),onSaved=()=>{}}={}){
  const parts={},revoked=[],created=[];
  const container=fakeElement();
  container.querySelector=selector=>{const name=selector.match(/data-part="([^"]+)"/)[1];return parts[name]??=fakeElement()};
  container.querySelectorAll=()=>[];
  const context=vm.createContext({console,Date,Math,Number,String,Object,Array,Set,Promise,Error,JSON,AbortController,
    URL:{createObjectURL(blob){const id='blob:frame-'+created.length;created.push({id,blob});return id},revokeObjectURL(id){revoked.push(id)}},
    document:{createElement:fakeElement},fetch:fetchImpl,setTimeout(){return 1},clearTimeout(){},container,api,onSaved});
  vm.runInContext(loginSource.replace(/\bexport /g,''),context);
  const panel=vm.runInContext("new RemoteLoginPanel({container,accountId:'account-1',api,onSaved})",context);
  parts.state.parentElement={parentElement:{dataset:{}}};
  panel.session={id:'session-1',state:'awaiting_login'};panel.frameURL='blob:initial';
  return {context,panel,parts,revoked,created};
}

test('Google guidance appears only for the active official login page and never navigates automatically',async()=>{
  const calls=[];const {panel,parts}=remoteScope({api:async(path,options)=>calls.push(options.body)});
  panel.update({id:'session-1',state:'awaiting_login',tabs:[{id:'home',origin:'https://dreamina.capcut.com',active:false},{id:'google',origin:'https://accounts.google.com',active:true}]});
  assert.equal(parts.provider.hidden,false);assert.equal(calls.length,0);
  assert.equal(parts['return-dreamina'].textContent,'切回 Dreamina 页面');
  parts['return-dreamina'].listeners.click();await panel.queue;
  assert.equal(calls.length,1);assert.equal(calls[0].action,'tab');assert.equal(calls[0].tab_id,'home');
  panel.update({id:'session-1',state:'awaiting_login',tabs:[{id:'evil',origin:'https://accounts.google.com.example',active:true}]});
  assert.equal(parts.provider.hidden,true);
  panel.update({id:'session-1',state:'verifying',tabs:[{id:'google',origin:'https://accounts.google.com',active:true}]});
  assert.equal(parts.provider.hidden,true);
});

test('Google same-tab fallback only goes back when the user requests it',async()=>{
  const calls=[];const {panel,parts}=remoteScope({api:async(path,options)=>calls.push(options.body)});
  panel.update({id:'session-1',state:'awaiting_login',tabs:[{id:'google',origin:'https://accounts.google.com',active:true}]});
  assert.equal(parts['return-dreamina'].textContent,'返回上一页');assert.equal(calls.length,0);
  parts['return-dreamina'].listeners.click();await panel.queue;
  assert.equal(calls.length,1);assert.equal(calls[0].action,'back');
});

test('remote clicks scale to the actual viewport and reject out of image input',()=>{
  const {context}=remoteScope();
  assert.equal(JSON.stringify(vm.runInContext('frameCoordinates({left:10,top:20,width:550,height:380},{width:1100,height:760},285,210)',context)),JSON.stringify({x:550,y:380}));
  for(const point of ['9,30','560,30','20,400','NaN,30'])assert.equal(vm.runInContext('frameCoordinates({left:10,top:20,width:550,height:380},{width:1100,height:760},'+point+')',context),null);
  assert.equal(vm.runInContext('frameCoordinates({left:0,top:0,width:0,height:380},{width:1100,height:760},0,0)',context),null);
});

test('remote keyboard accepts bounded editing keys without browser shortcuts',()=>{
  const {context}=remoteScope();
  assert.equal(vm.runInContext("keyAction({key:'l',ctrlKey:true})",context),null);
  assert.equal(vm.runInContext("keyAction({key:'F12'})",context),null);
  assert.equal(vm.runInContext("keyAction({key:'Enter',isComposing:true})",context),null);
  assert.equal(vm.runInContext("keyAction({key:'a',ctrlKey:true}).key",context),'Control+A');
  assert.equal(vm.runInContext("keyAction({key:'Tab',shiftKey:true}).key",context),'Shift+Tab');
  assert.equal(vm.runInContext("cleanText('  secret with spaces  ')",context),'  secret with spaces  ');
  assert.throws(()=>vm.runInContext("cleanText('x'.repeat(4097))",context),/4096/);
});

test('queued remote inputs stay ordered and release text after transmission',async()=>{
  const calls=[];const {panel}=remoteScope({api:async(path,options)=>{calls.push({path,body:{...options.body}})}});
  const click={action:'click',x:100,y:200,button:'left'},text={action:'text',text:'private input'};
  await Promise.all([panel.send(click),panel.send(text)]);
  assert.deepEqual(calls.map(c=>c.body.action),['click','text']);
  assert.equal(calls[1].body.text,'private input');
  assert.equal(text.text,undefined);assert.equal(panel.queuedInputs,0);
});

test('only completed pointer gestures submit a click or a bounded user drag',async()=>{
  const calls=[];const {panel,parts}=remoteScope({api:async(path,options)=>calls.push({...options.body})});
  const event=(x,y)=>({button:0,pointerId:1,clientX:x,clientY:y,preventDefault(){}});
  parts.frame.listeners.pointerdown(event(50,50));assert.equal(calls.length,0);
  parts.frame.listeners.pointerup(event(52,51));await panel.queue;
  assert.equal(calls[0].action,'click');assert.equal(calls[0].x,80);assert.equal(calls[0].y,60);
  parts.frame.listeners.pointerdown(event(50,50));parts.frame.listeners.pointerup(event(150,70));await panel.queue;
  assert.equal(calls[1].action,'drag');assert.equal(calls[1].from_x,80);assert.equal(calls[1].to_x,280);assert.equal(calls[1].to_y,100);
  assert.ok(calls[1].duration_ms>=100&&calls[1].duration_ms<=3000);
  for(const cancellation of ['pointercancel','pointerleave','lostpointercapture']){parts.frame.listeners.pointerdown(event(50,50));parts.frame.listeners[cancellation]();parts.frame.listeners.pointerup(event(150,70));}
  parts.frame.listeners.pointerdown(event(50,50));parts.frame.listeners.pointerup(event(900,900));await panel.queue;
  assert.equal(calls.length,2);
});

test('an uncertain input is never retried and prevents stale queued input',async()=>{
  let calls=0;const {panel}=remoteScope({api:async()=>{calls++;throw new Error('network')}});
  const first={action:'text',text:'first'},second={action:'text',text:'second'};
  await Promise.all([panel.send(first),panel.send(second)]);
  assert.equal(calls,1);assert.equal(panel.inputSuspended,true);assert.equal(panel.queuedInputs,0);
  assert.equal(first.text,undefined);assert.equal(second.text,undefined);
});

test('IME composition and paste forward text once without keeping local values',async()=>{
  const calls=[];const {panel,parts}=remoteScope({api:async(path,options)=>calls.push({...options.body})});
  const key=parts.keyboard;
  key.listeners.compositionstart();key.value='中文';key.listeners.input();assert.equal(calls.length,0);
  key.listeners.compositionend({data:'中文'});key.listeners.input();await panel.queue;
  assert.equal(calls.length,1);assert.equal(calls[0].text,'中文');assert.equal(key.value,'');
  key.listeners.paste({preventDefault(){},clipboardData:{getData(){return ' one time code '}}});await panel.queue;
  assert.equal(calls[1].text,' one time code ');assert.equal(key.value,'');
});

test('login screenshots request same-origin cookies without caching and revoke replaced URLs',async()=>{
  const calls=[];const {panel,revoked}=remoteScope({fetchImpl:async(url,options)=>{calls.push({url,options});return {ok:true,headers:{get(){return 'image/jpeg'}},blob:async()=>({})}}});
  await panel.frame();await panel.frame();
  assert.ok(calls[0].url.endsWith('/session-1/frame'));
  assert.equal(calls[0].options.credentials,'same-origin');assert.equal(calls[0].options.cache,'no-store');
  assert.deepEqual(revoked,['blob:initial','blob:frame-0']);
  panel.clearFrame();assert.deepEqual(revoked,['blob:initial','blob:frame-0','blob:frame-1']);assert.equal(panel.frameURL,null);
});

test('closing during session creation cancels the late session instead of orphaning it',async()=>{
  let resolveCreate;const calls=[];
  const {panel}=remoteScope({api:async(path,options)=>{calls.push({path,method:options.method});if(options.method==='POST')return new Promise(resolve=>{resolveCreate=resolve});return {}}});
  panel.session=null;const starting=panel.start();await panel.stop();resolveCreate({id:'late-session',state:'starting'});await starting;
  assert.deepEqual(calls.map(c=>c.method),['POST','DELETE']);assert.equal(calls[1].path,'/login-sessions/late-session');assert.equal(panel.frameURL,null);
});

test('success stops frame input and reports persistence and capacity wait honestly',()=>{
  let saved=0;const {panel,parts}=remoteScope({onSaved(){saved++}});
  const session={id:'session-1',state:'succeeded',profile_saved:true,credentials_synced:true,worker_state:'saved_capacity_wait'};
  panel.update(session);panel.update(session);
  assert.equal(saved,1);assert.equal(panel.acceptsInput(),false);assert.equal(panel.frameURL,null);
  assert.equal(parts.result.children.at(-1).children[1].textContent,'状态已保存，等待执行节点容量');
});

test('verification pauses frames and all inputs while clearing local composition',async()=>{
  let inputs=0,frames=0;const {panel,parts}=remoteScope({api:async(path,options)=>{if(options)inputs++;return {id:'session-1',state:'verifying'}},fetchImpl:async()=>{frames++;throw new Error('must not request')}});
  parts.keyboard.value='temporary text';await panel.poll();
  assert.equal(parts.keyboard.value,'');assert.equal(parts.verifying.hidden,false);assert.equal(panel.acceptsInput(),false);
  await panel.send({action:'key',key:'Enter'});await panel.send({action:'dialog',accept:true});
  assert.equal(inputs,0);assert.equal(frames,0);
});

test('remote login module has no persistent credential or frame storage',()=>{
  assert.ok(!/localStorage|sessionStorage|indexedDB|document\.cookie|console\./.test(loginSource));
  assert.ok(!/action:'(?:navigate|evaluate|script)'/.test(loginSource));
  assert.ok(source.includes("else await openAccountLogin(accountId)"));
});

test('proxy expiry and creation use the backend three-day reminder default',()=>{
  const c=scope();c.proxy={expires_at:Date.now()/1000+5*86400};
  assert.ok(!vm.runInContext('proxyExpiry(proxy)',c).includes('即将到期'));
  c.proxy.expires_at=Date.now()/1000+2*86400;assert.ok(vm.runInContext('proxyExpiry(proxy)',c).includes('即将到期'));
  vm.runInContext('proxyDialog()',c);
  const html=vm.runInContext("document.querySelector('#modal').innerHTML",c);
  assert.match(html,/name="expiry_warn_days"[^>]*value="3"/);
  assert.ok(html.includes('name="ip_change_policy"')&&html.includes('默认仅告警'));
});

test('proxy IP-change strategy is explicit and rejects unknown policies',()=>{
  const c=scope();vm.runInContext("FormData=class{constructor(form){this.v=form}get(n){return this.v[n]??null}}",c);
  c.form={proxy_name:'Proxy',max_accounts:'3',expiry_warn_days:'3',url:'http://proxy.example:8080'};
  assert.equal(vm.runInContext('proxyFormBody(form,false)',c).ip_change_policy,'warn');
  c.form.ip_change_policy='pause';assert.equal(vm.runInContext('proxyFormBody(form,false)',c).ip_change_policy,'pause');
  c.form.ip_change_policy='direct';assert.throws(()=>vm.runInContext('proxyFormBody(form,false)',c),/IP变化策略/);
});

test('IP probe warning does not falsely show proxy failure or current-IP verification',()=>{
  const c=scope();c.proxy={status:'active',exit_ip:'203.0.113.10',last_checked_at:Date.now()/1000,ip_known:false,warning_code:'proxy_ip_unavailable'};
  const html=vm.runInContext('proxyStatus(proxy)',c);
  assert.ok(html.includes('网站可达，出口IP待确认')&&!html.includes('探测失败')&&!html.includes('>可用<'));
  assert.ok(vm.runInContext("proxyErrorText({message:'proxy_ip_unavailable'})",c).includes('新绑定需等待IP探测恢复'));
});

test('account table displays bound proxy and IP warning without imposing pause in warn mode',()=>{
  const c=scope();c.account={id:'a',name:'Account',enabled:true,online:true,ready:true,secret_configured:true,region:'US',max_concurrent:1,max_inflight:1,proxy_id:'proxy-safe',proxy_name:'Exit A',proxy_exit_ip_at_bind:'203.0.113.1',proxy_ip_changed:true,proxy_ip_change_policy:'warn'};
  const html=vm.runInContext('accountsTable([account])',c);
  assert.ok(html.includes('Exit A')&&html.includes('出口IP已变化（仅告警）')&&html.includes('>就绪<'));
  assert.ok(!html.includes('出口待处理，暂停调度')&&!html.includes('代理暂停'));
  c.account.proxy_ip_change_policy='pause';c.account.proxy_scheduling_error='proxy_exit_changed';
  assert.ok(vm.runInContext('accountsTable([account])',c).includes('出口IP已变化，等待确认'));
});

test('checking and restart-pending proxy changes are not labelled as login failure or manual stop',()=>{
  const c=scope();c.account={id:'a',enabled:false,status:'unverified',proxy_id:'proxy-safe',proxy_change_state:'checking',proxy_retry_at:Date.now()/1000+60,proxy_retry_count:2,browser_profile_saved:true,managed_worker:{state:'stopped'}};
  let html=vm.runInContext('accountStatusBadge(account)+accountEgressSummary(account)',c);
  assert.ok(html.includes('出口核验待重试')&&html.includes('下次尝试')&&html.includes('已安排重试 2 次'));
  assert.ok(!html.includes('登录过期')&&!html.includes('需要登录')&&!html.includes('已停用'));
  assert.ok(vm.runInContext('accountLoginButton(account)',c).includes('<button disabled '));
  assert.ok(vm.runInContext('managedWorkerButton(account)',c).includes('<button disabled '));
  c.account.proxy_change_state='restarting';html=vm.runInContext('accountStatusBadge(account)',c);
  assert.ok(html.includes('出口已核验，节点恢复中'));
});

test('HTTP 200 pending binding is acknowledged without reporting a completed proxy change',async()=>{
  const c=scope(async()=>({ok:true,status:200,json:async()=>({pending:true,state:'checking',retry_at:Date.now()/1000+5,retry_count:1})}));
  vm.runInContext('globalThis.messages=[];toast=message=>messages.push(message);navigate=async()=>{}',c);
  const complete=await vm.runInContext("bindAccountProxy('a','proxy-one')",c);
  assert.equal(complete,false);assert.equal(c.messages.length,1);
  assert.ok(c.messages[0].includes('出口核验等待自动重试')&&!c.messages[0].includes('代理已切换'));
});

function batchFormScope(fetchImpl){
  const c=scope(fetchImpl);
  vm.runInContext("globalThis.errorNode={textContent:''};globalThis.form={elements:{lines:{value:''}},querySelector(){return errorNode}};loadProxyChoices=async()=>{};navigate=async()=>{};globalThis.messages=[];toast=message=>messages.push(message)",c);
  return c;
}

test('batch import matches 200-line and 200000-character limits and explains atomic behavior',async()=>{
  const calls=[],c=batchFormScope(async(url,options)=>{calls.push({url,options});return {ok:true,status:200,json:async()=>({items:[]})};});
  vm.runInContext('proxyBatchDialog()',c);
  const html=vm.runInContext("document.querySelector('#modal').innerHTML",c);
  assert.ok(html.includes('maxlength="200000"')&&html.includes('最多200个非空行')&&html.includes('整批回滚'));
  assert.ok(!html.includes('最多100行')&&!html.includes('部分成功'));
  c.form.elements.lines.value=Array(200).fill('http://proxy.example:8080').join('\r\n');
  await vm.runInContext('saveProxyBatch({preventDefault(){},currentTarget:form})',c);
  assert.equal(calls.length,1);assert.equal(c.form.elements.lines.value,'');assert.ok(c.messages[0].includes('整批代理导入完成'));
  c.form.elements.lines.value=Array(201).fill('http://proxy.example:8080').join('\r');
  await vm.runInContext('saveProxyBatch({preventDefault(){},currentTarget:form})',c);
  assert.equal(calls.length,1);assert.ok(c.errorNode.textContent.includes('1–200个非空行'));
  c.form.elements.lines.value='x'.repeat(200001);await vm.runInContext('saveProxyBatch({preventDefault(){},currentTarget:form})',c);
  assert.equal(calls.length,1);
});

test('rejected batch retains input and never announces partial imports or echoes proxy credentials',async()=>{
  const c=batchFormScope(async()=>({ok:false,status:400,json:async()=>({error:{code:'invalid_proxy',message:'Invalid line http://private:password@host:80'}})}));
  c.form.elements.lines.value='http://proxy.example:8080\ninvalid';
  await vm.runInContext('saveProxyBatch({preventDefault(){},currentTarget:form})',c);
  assert.equal(c.form.elements.lines.value,'http://proxy.example:8080\ninvalid');assert.equal(c.messages.length,0);
  assert.ok(!c.errorNode.textContent.includes('private:password')&&!c.errorNode.textContent.includes('部分成功'));
});

test('unexpected partial-success shape is treated as unconfirmed rather than promised rollback',async()=>{
  const c=batchFormScope(async()=>({ok:true,status:200,json:async()=>({items:[],errors:[{line:2,message:'unknown'}]})}));
  c.form.elements.lines.value='http://proxy.example:8080';
  await vm.runInContext('saveProxyBatch({preventDefault(){},currentTarget:form})',c);
  assert.equal(c.form.elements.lines.value,'http://proxy.example:8080');assert.equal(c.messages.length,0);
  assert.ok(c.errorNode.textContent.includes('未确认整批导入完成')&&!c.errorNode.textContent.includes('已导入条目保留'));
});
