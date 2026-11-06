/* Remote login UI. It stores no credentials, screenshots, or input history. */
const TERMINAL = new Set(['succeeded', 'failed', 'expired', 'cancelled']);
const STATE_LABELS = {starting:'正在启动登录环境',awaiting_login:'请在下方网页完成登录',verifying:'正在验证并保存登录状态',succeeded:'账号登录成功',failed:'登录未完成',expired:'登录会话已过期',cancelled:'登录已取消'};
const SAFE_KEYS = new Set(['Enter','Tab','Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown','Escape']);

export function frameCoordinates(rect, viewport, clientX, clientY) {
  if (!rect || !viewport || ![rect.left,rect.top,rect.width,rect.height,viewport.width,viewport.height,clientX,clientY].every(Number.isFinite)) return null;
  if (rect.width<=0 || rect.height<=0 || viewport.width<=0 || viewport.height<=0 || viewport.width>4096 || viewport.height>4096) return null;
  const x=(clientX-rect.left)/rect.width,y=(clientY-rect.top)/rect.height;
  if(x<0||x>=1||y<0||y>=1)return null;
  return {x:Math.min(viewport.width-1,Math.floor((clientX-rect.left)*viewport.width/rect.width)),y:Math.min(viewport.height-1,Math.floor((clientY-rect.top)*viewport.height/rect.height))};
}

export function keyAction(event) {
  if(event.isComposing)return null;
  if((event.ctrlKey||event.metaKey)&&!event.altKey&&event.key.toLowerCase()==='a')return {action:'key',key:'Control+A'};
  if(event.ctrlKey||event.metaKey||event.altKey)return null;
  if(SAFE_KEYS.has(event.key))return {action:'key',key:event.shiftKey&&event.key==='Tab'?'Shift+Tab':event.key};
  return null;
}

export function cleanText(value) {
  if(typeof value!=='string'||!value)return null;
  if(value.length>4096)throw new Error('单次粘贴最多 4096 个字符。');
  return value;
}

export function terminalState(value){return TERMINAL.has(value);}
export function stateLabel(value){return STATE_LABELS[value]||'正在连接登录会话';}
export function googleLoginHint(session){
  const tabs=Array.isArray(session?.tabs)?session.tabs:[];
  if(session?.state!=='awaiting_login'||!tabs.some(tab=>tab.active&&tab.origin==='https://accounts.google.com'))return null;
  const dreamina=tabs.find(tab=>tab.origin==='https://dreamina.capcut.com');
  return {action:dreamina?{action:'tab',tab_id:dreamina.id}:{action:'back'},label:dreamina?'切回 Dreamina 页面':'返回上一页'};
}
export function workerLabel(value){const current=typeof value==='string'?value:value?.state;return ({running:'正在运行',starting:'正在启动',stopped:'已停止',saved_capacity_wait:'状态已保存，等待执行节点容量',running_unverified:'正在运行，等待验证',disabled:'账号已停用',start_failed:'启动失败，可在账号列表重试',not_started:'未启动'})[current]||'未启动';}

export class RemoteLoginPanel {
  constructor({container,accountId,api,onSaved=()=>{},onError=()=>{},fetchImpl=fetch}) {
    this.container=container;this.accountId=accountId;this.api=api;this.onSaved=onSaved;this.onError=onError;this.fetchImpl=(...args)=>fetchImpl(...args);
    this.session=null;this.closed=false;this.terminal=false;this.savedNotified=false;this.frameURL=null;this.frameController=null;
    this.timer=null;this.queue=Promise.resolve();this.queuedInputs=0;this.composing=false;this.inputSuspended=false;this.failures=0;this.pointerGesture=null;
    this.viewport={width:1100,height:760};this.mount();
  }

  mount() {
    this.container.innerHTML=`<div class="remote-login-panel">
      <div class="remote-login-status" role="status" aria-live="polite"><span class="spinner"></span><div><strong data-part="state">正在启动登录环境</strong><small data-part="message">登录页面会显示在此处，无需另开浏览器或复制 Cookie。</small></div><span data-part="expiry" class="remote-login-expiry"></span></div>
      <div class="remote-login-provider" data-part="provider" role="note" hidden><div><strong>Google 登录兼容性</strong><p>若提示 “This browser or app may not be secure”，表示 Google 拒绝了当前登录环境，反复重试不一定有效。只有原账号已绑定邮箱登录时才切换到邮箱方式；同一邮箱可能对应不同账号。</p><a href="https://support.google.com/accounts/answer/7675428?hl=zh-Hans" target="_blank" rel="noopener noreferrer">查看 Google 官方说明 ↗</a></div><button type="button" class="btn small" data-part="return-dreamina">切回 Dreamina 页面</button></div>
      <div class="remote-login-toolbar"><div class="remote-login-nav"><button type="button" data-command="back" aria-label="远程页面后退" title="后退">←</button><button type="button" data-command="reload" aria-label="刷新远程登录页" title="刷新">↻</button></div><select data-part="tabs" aria-label="登录页面窗口"><option>正在连接…</option></select><button type="button" data-command="zoom">原始大小</button></div>
      <div class="remote-login-error" data-part="error" role="alert" hidden></div>
      <div class="remote-login-dialog" data-part="dialog" hidden><p data-part="dialog-message"></p><div><button type="button" data-dialog="false" class="btn">取消</button><button type="button" data-dialog="true" class="btn primary">确定</button></div></div>
      <div class="remote-login-stage" data-part="stage"><div data-part="placeholder" class="remote-login-placeholder"><span class="spinner"></span><p>正在连接专用登录页面…</p></div><img data-part="frame" alt="远程登录网页实时画面，点击网页中的输入框后可输入" draggable="false" hidden><div data-part="verifying" class="remote-login-verifying" hidden><span class="spinner"></span><strong>正在保存账号登录状态</strong><span>保存期间暂停网页操作，请稍候。</span></div><textarea data-part="keyboard" class="remote-login-keyboard" aria-label="远程网页键盘输入" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea></div>
      <div class="remote-login-hint"><span data-part="focus">可点击、拖动网页；选中输入框后可输入、粘贴或使用中文输入法。</span><span>验证码由你在网页完成；输入不会写入日志。</span></div>
      <div class="remote-login-result" data-part="result" hidden></div>
    </div>`;
    const part=name=>this.container.querySelector(`[data-part="${name}"]`);
    this.parts=Object.fromEntries(['state','message','expiry','tabs','error','dialog','dialog-message','stage','placeholder','frame','verifying','keyboard','focus','result','provider','return-dreamina'].map(name=>[name,part(name)]));
    this.parts['return-dreamina'].addEventListener('click',()=>{const hint=googleLoginHint(this.session);if(hint)this.send(hint.action);});
    this.parts.tabs.addEventListener('change',()=>this.send({action:'tab',tab_id:this.parts.tabs.value}));
    this.container.querySelectorAll('[data-command]').forEach(button=>button.addEventListener('click',()=>{
      if(button.dataset.command==='zoom'){
        this.parts.stage.classList.toggle('original-size');button.textContent=this.parts.stage.classList.contains('original-size')?'适合宽度':'原始大小';return;
      }
      this.send({action:button.dataset.command});
    }));
    this.container.querySelectorAll('[data-dialog]').forEach(button=>button.addEventListener('click',()=>this.send({action:'dialog',accept:button.dataset.dialog==='true'})));
    this.parts.frame.addEventListener('pointerdown',event=>{
      if(!this.acceptsInput()||event.button!==0)return;
      const point=frameCoordinates(this.parts.frame.getBoundingClientRect(),this.viewport,event.clientX,event.clientY);
      if(!point)return;
      event.preventDefault();
      this.pointerGesture={point,clientX:event.clientX,clientY:event.clientY,id:event.pointerId,started:Date.now()};
      this.parts.frame.setPointerCapture?.(event.pointerId);
    });
    this.parts.frame.addEventListener('pointerup',event=>{
      const gesture=this.pointerGesture;
      if(!gesture||gesture.id!==event.pointerId)return;
      this.cancelPointer();
      const point=frameCoordinates(this.parts.frame.getBoundingClientRect(),this.viewport,event.clientX,event.clientY);
      if(!point||!this.acceptsInput())return;
      event.preventDefault();this.parts.keyboard.focus({preventScroll:true});
      const distance=Math.hypot(event.clientX-gesture.clientX,event.clientY-gesture.clientY);
      if(distance<5)this.send({action:'click',...gesture.point,button:'left'});
      else this.send({action:'drag',from_x:gesture.point.x,from_y:gesture.point.y,to_x:point.x,to_y:point.y,duration_ms:Math.max(100,Math.min(3000,Date.now()-gesture.started))});
    });
    for(const type of ['pointercancel','pointerleave','lostpointercapture'])this.parts.frame.addEventListener(type,()=>this.cancelPointer());
    this.parts.frame.addEventListener('contextmenu',event=>event.preventDefault());
    this.parts.stage.addEventListener('wheel',event=>{
      if(!this.acceptsInput())return;
      if(this.parts.stage.classList.contains('original-size')&&event.shiftKey)return;
      event.preventDefault();
      const scale=event.deltaMode===1?16:event.deltaMode===2?this.viewport.height:1;
      this.send({action:'scroll',delta_x:Math.max(-1000,Math.min(1000,Math.round(event.deltaX*scale))),delta_y:Math.max(-1000,Math.min(1000,Math.round(event.deltaY*scale)))});
    },{passive:false});
    const keyboard=this.parts.keyboard;
    keyboard.addEventListener('focus',()=>{this.parts.stage.classList.add('keyboard-active');this.parts.focus.textContent='远程键盘已连接 · Tab 切换输入框，Enter 确认';});
    keyboard.addEventListener('blur',()=>{this.parts.stage.classList.remove('keyboard-active');this.parts.focus.textContent='可点击、拖动网页；选中输入框后可输入、粘贴或使用中文输入法。';keyboard.value='';});
    keyboard.addEventListener('keydown',event=>{
      const action=keyAction(event);
      if(action){event.preventDefault();event.stopPropagation();this.send(action);return;}
      if((event.ctrlKey||event.metaKey)&&!['v','V'].includes(event.key)){event.preventDefault();event.stopPropagation();}
    });
    keyboard.addEventListener('compositionstart',()=>{this.composing=true;});
    keyboard.addEventListener('compositionend',event=>{this.composing=false;const value=keyboard.value||event.data||'';keyboard.value='';this.sendText(value);});
    keyboard.addEventListener('input',()=>{if(this.composing)return;const value=keyboard.value;keyboard.value='';this.sendText(value);});
    keyboard.addEventListener('paste',event=>{event.preventDefault();const value=event.clipboardData?.getData('text/plain')||'';keyboard.value='';this.sendText(value);});
  }

  acceptsInput(){return !this.closed&&!this.terminal&&!this.inputSuspended&&this.session?.state==='awaiting_login'&&!!this.frameURL;}
  cancelPointer(){const gesture=this.pointerGesture;this.pointerGesture=null;if(gesture&&this.parts.frame.hasPointerCapture?.(gesture.id))this.parts.frame.releasePointerCapture(gesture.id);}
  sendText(value){try{const text=cleanText(value);if(text)this.send({action:'text',text});}catch(error){this.showError(error.message);}}
  showError(message){if(this.closed)return;this.parts.error.textContent=String(message||'登录会话连接中断。');this.parts.error.hidden=false;}

  send(input) {
    if(!this.acceptsInput()&&!(input.action==='dialog'&&this.session?.state==='awaiting_login'&&!this.inputSuspended))return Promise.resolve(false);
    if(this.closed||this.terminal||!this.session)return Promise.resolve(false);
    if(this.queuedInputs>=100){this.showError('输入队列繁忙，请稍候再操作。');return Promise.resolve(false);}
    const sessionId=this.session.id;this.queuedInputs++;
    const run=async()=>{
      try{
        if(this.closed||this.terminal||this.inputSuspended||this.session?.state!=='awaiting_login')return false;
        await this.api(`/login-sessions/${encodeURIComponent(sessionId)}/input`,{method:'POST',body:input});
        return true;
      }catch(error){
        this.inputSuspended=true;this.showError('操作结果暂时无法确认，已暂停输入；请等待会话状态恢复。');
        return false;
      }finally{input.text=undefined;this.queuedInputs--;}
    };
    this.queue=this.queue.then(run,run);
    return this.queue;
  }

  async start() {
    try{
      const response=await this.api(`/accounts/${encodeURIComponent(this.accountId)}/login-sessions`,{method:'POST',body:{auto_start_worker:true}});
      this.session=response.session||response;
      if(!this.session?.id||!/^[A-Za-z0-9_-]{1,128}$/.test(this.session.id))throw new Error('登录服务返回了无效的会话标识。');
      if(this.closed){await this.cancelSession();return;}
      this.update(this.session);await this.poll();
    }catch(error){if(!this.closed){this.terminal=true;this.showError(error.message||'无法启动登录会话。');this.parts.state.textContent='无法启动登录环境';this.parts.state.parentElement.parentElement.dataset.state='failed';this.parts.placeholder.hidden=true;this.onError(error);}}
  }

  update(session) {
    this.session=session;
    if(session.viewport&&Number.isFinite(session.viewport.width)&&Number.isFinite(session.viewport.height))this.viewport=session.viewport;
    this.parts.state.textContent=stateLabel(session.state);
    this.parts.state.parentElement.parentElement.dataset.state=session.state;
    this.parts.verifying.hidden=session.state!=='verifying';
    if(session.state==='verifying'){this.cancelPointer();this.parts.keyboard.value='';this.parts.keyboard.blur();}
    this.parts.message.textContent=session.message||'在远程网页完成正常登录，系统会自动保存状态并同步凭据。';
    const left=Number(session.expires_at)?Math.max(0,Math.ceil(session.expires_at-Date.now()/1000)):0;
    this.parts.expiry.textContent=left&&!terminalState(session.state)?`${Math.floor(left/60)}:${String(left%60).padStart(2,'0')}`:'';
    const tabs=Array.isArray(session.tabs)?session.tabs:[];
    const signature=JSON.stringify(tabs.map(tab=>[tab.id,tab.title,tab.origin,tab.active]));
    if(signature!==this.tabsSignature){
      this.tabsSignature=signature;this.parts.tabs.replaceChildren();
      for(const tab of tabs){const option=document.createElement('option');option.value=String(tab.id);option.textContent=String(tab.title||tab.origin||'登录页面').slice(0,100);option.selected=!!tab.active;this.parts.tabs.append(option);}
    }
    this.parts.tabs.disabled=tabs.length<2||session.state!=='awaiting_login';
    const providerHint=googleLoginHint(session);this.parts.provider.hidden=!providerHint;
    if(providerHint)this.parts['return-dreamina'].textContent=providerHint.label;
    const dialog=session.dialog;
    this.parts.dialog.hidden=!dialog;
    this.parts['dialog-message'].textContent=dialog?.message||'';
    if(terminalState(session.state)){
      this.terminal=true;this.clearFrame();this.parts.placeholder.hidden=true;
      this.parts.keyboard.value='';this.parts.keyboard.blur();
      this.parts.result.hidden=false;this.parts.result.replaceChildren();
      const heading=document.createElement('h3');heading.textContent=stateLabel(session.state);this.parts.result.append(heading);
      for(const [label,value] of [['浏览器状态',session.profile_saved?'已保存':'未确认保存'],['查询凭据',session.credentials_synced?'已自动同步':'未同步'],['执行节点',workerLabel(session.worker_state)]]){
        const row=document.createElement('p');const strong=document.createElement('strong');strong.textContent=label;const span=document.createElement('span');span.textContent=String(value);row.append(strong,span);this.parts.result.append(row);
      }
      if(session.state==='succeeded'&&!this.savedNotified){this.savedNotified=true;Promise.resolve(this.onSaved(session)).catch(()=>{});}
    }
    this.container.querySelectorAll('[data-command]').forEach(button=>{if(button.dataset.command!=='zoom')button.disabled=this.terminal||session.state!=='awaiting_login';});
  }

  async poll() {
    if(this.closed||this.terminal||!this.session)return;
    try{
      const data=await this.api(`/login-sessions/${encodeURIComponent(this.session.id)}`);
      if(this.closed)return;
      this.update(data.session||data);this.failures=0;
      if(this.inputSuspended){this.inputSuspended=false;this.parts.error.hidden=true;}
      if(!this.terminal&&this.session.state==='awaiting_login')await this.frame();
    }catch(error){
      if(this.closed)return;
      this.failures++;this.inputSuspended=true;
      this.showError(error.message||'无法读取登录会话状态。');
      if(error.status===401||error.status===403||error.status===404||error.status===410){this.terminal=true;this.clearFrame();this.parts.placeholder.hidden=true;}
    }finally{
      if(!this.closed&&!this.terminal)this.timer=setTimeout(()=>this.poll(),Math.min(5000,800*Math.max(1,this.failures)));
    }
  }

  async frame() {
    this.frameController=new AbortController();
    try{
      const response=await this.fetchImpl(`/api/admin/login-sessions/${encodeURIComponent(this.session.id)}/frame`,{credentials:'same-origin',cache:'no-store',signal:this.frameController.signal,headers:{Accept:'image/jpeg'}});
      if(!response.ok){if([409,425].includes(response.status))return;const error=new Error('登录画面暂不可用。');error.status=response.status;throw error;}
      if(!(response.headers.get('content-type')||'').startsWith('image/'))throw new Error('登录画面格式无效。');
      const blob=await response.blob();
      if(this.closed||this.terminal)return;
      const next=URL.createObjectURL(blob),previous=this.frameURL;this.frameURL=next;
      this.parts.frame.src=next;this.parts.frame.hidden=false;this.parts.placeholder.hidden=true;
      if(previous)URL.revokeObjectURL(previous);
    }finally{this.frameController=null;}
  }

  clearFrame(){this.cancelPointer();this.frameController?.abort();this.parts.frame.removeAttribute('src');this.parts.frame.hidden=true;if(this.frameURL){URL.revokeObjectURL(this.frameURL);this.frameURL=null;}}
  async cancelSession(){if(!this.session?.id||terminalState(this.session.state))return;try{await this.api(`/login-sessions/${encodeURIComponent(this.session.id)}`,{method:'DELETE'});}catch{/* Server expiry remains the fallback after a disconnected admin session. */}}
  async stop({cancel=true}={}){if(this.closed)return;this.closed=true;clearTimeout(this.timer);this.clearFrame();this.parts.keyboard.value='';this.parts.keyboard.blur();if(cancel)await this.cancelSession();}
}
