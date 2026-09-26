'use strict';
(() => {
  const $=id=>document.getElementById(id), M=MobileModel;
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
  const storage=(kind,operation,key,value)=>{try{return window[kind][operation](key,value);}catch{return null;}};
  const key='expman_mobile_token';
  let token=storage('sessionStorage','getItem',key)||storage('localStorage','getItem',key)||'';
  let data={jobs:[],nodes:[]}, session=null, connected=false, sampledAt=0, serverTime=0;
  let generation=0, busy=false, polling=false, timer=null, selected=null, detail=null, view='overview', confirmation=null, connectionError=false;
  function notice(message){$('notice').textContent=message;$('notice').hidden=!message;}
  function usable(){return M.canControl(session,connected,sampledAt,busy)&&!document.hidden;}
  function status(){
    const fresh=connected&&Date.now()-sampledAt<15000;
    $('connection').textContent=fresh?'管理端在线':token?'连接已中断':'未连接';
    $('connection').className='badge '+(fresh?'online':'offline');
    $('freshness').textContent=sampledAt?`${fresh?'最近同步':'数据可能已过时 · 最后同步'} ${new Date(sampledAt).toLocaleTimeString()}`:'连接管理端，查看实验进展。';
    document.querySelectorAll('[data-control]').forEach(b=>b.disabled=!usable());
    if(detail){const workerOnline=data.nodes.find(n=>n.id===detail.node_id)?.online===true;const clock=serverTime+(fresh?(Date.now()-sampledAt)/1000:0);const timing=ExperimentTiming.describe(detail,clock,fresh&&workerOnline);$('detail-timer').textContent=timing.text;$('detail-timer').title=timing.note;let note=$('detail-timing-note');if(!note){note=el('p',undefined,'muted small');note.id='detail-timing-note';$('detail-timer').after(note);}note.textContent=timing.note+(fresh?'':' · 管理端连接中断');}
  }
  async function request(path,body){
    const abort=new AbortController(), timeout=setTimeout(()=>abort.abort(),10000);
    try{
      const response=await fetch('/api/mobile/'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',redirect:'error',signal:abort.signal});
      const reply=await response.json();
      if(!response.ok){const error=new Error(reply.error||'请求失败');error.status=response.status;throw error;}
      return reply;
    }finally{clearTimeout(timeout);}
  }
  function clearCredentials(){storage('sessionStorage','removeItem',key);storage('localStorage','removeItem',key);}
  function disconnect(message=''){
    generation++;token='';session=null;connected=false;sampledAt=0;clearTimeout(timer);clearCredentials();selected=null;detail=null;data={jobs:[],nodes:[]};busy=false;
    $('credential').value='';$('login').hidden=false;$('workspace').hidden=true;$('tabs').hidden=true;$('detail').hidden=true;closeConfirm();notice(message);status();
  }
  function failure(error){connected=false;connectionError=true;if(error.status===401)disconnect(error.message);else notice(error.status?error.message:error.name==='AbortError'?'连接超时，请检查管理端和网络。':'无法连接管理端，请检查程序是否运行、网络和地址。');status();}
  async function refresh(){
    if(!token||polling||document.hidden)return;
    polling=true;clearTimeout(timer);const epoch=generation;
    try{
      const next=await request('state');if(epoch!==generation)return;
      data=next;session=next.session;connected=true;sampledAt=Date.now();serverTime=next.time;
      if(connectionError){notice('');connectionError=false;}
      $('login').hidden=true;$('tabs').hidden=false;$('workspace').hidden=Boolean(selected);render();
      if(selected){const identity=selected;const item=await request('job?id='+encodeURIComponent(identity));if(epoch!==generation||selected!==identity)return;detail=item;renderDetail();}
    }catch(error){if(epoch===generation)failure(error);}
    finally{polling=false;if(token&&!document.hidden)timer=setTimeout(refresh,5000);status();}
  }
  const number=value=>typeof value==='number'?(Number.isInteger(value)?String(value):Number(value.toPrecision(5)).toString()):String(value);
  function empty(box,message){if(!box.children.length)box.append(el('p',message,'empty'));}
  function jobCard(job){
    const button=el('button',undefined,'job'),title=el('div',undefined,'job-title');title.append(el('strong',job.spec.name),el('span',M.names[job.state]||job.state,'badge'));
    button.append(title,el('small',`${job.spec.algorithm} · ${job.node_id||'等待分配'}`));
    const metrics=Object.entries(job.metrics||{}).filter(([k])=>!['step','time','attempt'].includes(k)).slice(0,2);
    if(metrics.length)button.append(el('div',metrics.map(([k,v])=>`${k} ${number(v)}`).join(' · '),'job-metric'));
    if(job.command_id>job.command_ack)button.append(el('small','操作已提交 · 等待节点确认'));
    button.onclick=()=>openJob(job.id);return button;
  }
  function renderJobs(){
    const query=$('search').value.toLowerCase(),filter=$('filter').value;
    const jobs=data.jobs.filter(j=>[j.spec.name,j.spec.algorithm,j.spec.group].join(' ').toLowerCase().includes(query)).filter(j=>filter==='all'||(filter==='active'&&!M.terminal.has(j.state))||j.state===filter||(filter==='attention'&&['failed','paused','interrupted'].includes(j.state)));
    $('jobs').replaceChildren(...jobs.map(jobCard));$('job-count').textContent=String(jobs.length);empty($('jobs'),'没有符合条件的实验');
  }
  function render(){
    const active=data.jobs.filter(j=>!M.terminal.has(j.state));
    const counts=[[data.jobs.filter(j=>j.state==='running').length,'运行中'],[active.filter(j=>j.state!=='running').length,'等待 / 准备'],[data.jobs.filter(j=>j.state==='succeeded').length,'已完成'],[data.nodes.filter(n=>n.online).length,'在线节点']];
    $('stats').replaceChildren(...counts.map(([n,label])=>{const box=el('div',undefined,'stat');box.append(el('strong',n),el('span',label));return box;}));
    $('recent').replaceChildren(...active.slice(0,6).map(jobCard));empty($('recent'),'目前没有进行中的实验');renderJobs();
    $('nodes').replaceChildren(...data.nodes.map(worker=>{
      const box=el('article',undefined,'card'),heading=el('div',undefined,'heading');heading.append(el('h3',worker.id),el('span',worker.online?'在线':'离线','badge '+(worker.online?'online':'offline')));box.append(heading);
      box.append(el('p',`${worker.mode==='drain'?'已暂停接单':'允许接单'} · ${data.jobs.filter(j=>j.node_id===worker.id&&!M.terminal.has(j.state)).length} 个活跃任务`,'muted small'));
      const available=[['内存',worker.snapshot.free_ram_mb],['磁盘',worker.snapshot.disk_free_mb]].filter(([,n])=>Number.isFinite(n)).map(([label,n])=>`${label}剩余 ${(n/1024).toFixed(1)} GiB`);if(available.length)box.append(el('p',available.join(' · '),'muted small'));
      for(const gpu of worker.snapshot.gpus){const card=el('div',undefined,'gpu');card.append(el('h3',gpu.name||gpu.uuid));const known=Number.isFinite(gpu.free_mb)&&Number.isFinite(gpu.total_mb)&&gpu.total_mb>0;if(known){card.append(el('p',`显存剩余 ${(gpu.free_mb/1024).toFixed(1)} / ${(gpu.total_mb/1024).toFixed(1)} GiB`,'muted small'));const bar=el('progress');bar.max=gpu.total_mb;bar.value=Math.max(0,gpu.total_mb-gpu.free_mb);bar.setAttribute('aria-label','已用显存');card.append(bar);}else card.append(el('p','暂无显存数据','muted small'));box.append(card);}
      if(session.permission==='control'){const label=worker.mode==='drain'?'恢复接单':'暂停接单';const b=el('button',label);b.dataset.control='true';b.disabled=!usable();b.onclick=()=>confirmAction(label,`节点「${worker.id}」将${label}。`,'node-mode',{node_id:worker.id,mode:worker.mode==='drain'?'run':'drain',expected_mode:worker.mode});box.append(b);}
      return box;
    }));empty($('nodes'),'尚未接入算力节点');
    $('account').replaceChildren();for(const [label,value]of [['管理端',location.origin],['设备名称',session.name],['权限',session.permission==='control'?'监控与有限操作':'只读监控'],['有效期',new Date(session.expires*1000).toLocaleString()]])$('account').append(el('dt',label),el('dd',value));
    status();
  }
  function switchView(next){view=next;selected=null;detail=null;$('detail').hidden=true;$('workspace').hidden=false;document.querySelectorAll('[data-view]').forEach(n=>n.hidden=n.dataset.view!==next);document.querySelectorAll('[data-tab]').forEach(n=>{if(n.dataset.tab===next)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});window.scrollTo(0,0);}
  async function openJob(identity){
    selected=identity;detail=null;$('workspace').hidden=true;$('detail').hidden=true;notice('正在读取实验详情…');const epoch=generation;
    try{const item=await request('job?id='+encodeURIComponent(identity));if(epoch!==generation||selected!==identity)return;detail=item;notice('');renderDetail();window.scrollTo(0,0);}catch(error){if(epoch!==generation||selected!==identity)return;selected=null;$('workspace').hidden=false;failure(error);}
  }
  function renderDetail(){
    $('detail').hidden=false;$('detail-title').textContent=detail.spec.name;$('detail-status').textContent=M.names[detail.state]||detail.state;
    const worker=data.nodes.find(n=>n.id===detail.node_id);
    $('detail-node').textContent=`${detail.node_id||'等待分配'}${worker?' · '+(worker.online?'算力端在线':'算力端离线'):''} · 第 ${detail.attempt||0} 次运行`;
    $('detail-message').textContent=typeof detail.detail==='string'?detail.detail:JSON.stringify(detail.detail);
    $('detail-pending').hidden=!(detail.command_id>detail.command_ack);$('detail-pending').textContent='请求已记录，等待算力端确认；这不表示操作已经完成。';
    $('detail-actions').replaceChildren();if(session?.permission==='control')for(const action of M.actions(detail)){const b=el('button',action.label,action.value==='cancel'?'danger':'quiet');b.dataset.control='true';b.disabled=!usable();b.onclick=()=>confirmAction(action.label,`实验「${detail.spec.name}」：${action.label}。`,'action',{job_id:detail.id,action:action.value,expected_command_id:detail.command_id});$('detail-actions').append(b);}
    $('metrics').replaceChildren(...Object.entries(detail.metrics||{}).map(([key,value])=>{const item=el('div',undefined,'metric');item.append(el('small',key),el('strong',number(value)));return item;}));empty($('metrics'),'尚无指标回传');
    const previous=$('metric-key').value, keys=[...new Set((detail.events||[]).filter(e=>e.kind==='metrics').flatMap(e=>Object.keys(e.data.metrics||{})))].filter(k=>!['step','time','attempt'].includes(k));
    $('metric-key').replaceChildren(...keys.map(k=>{const o=el('option',k);o.value=k;return o;}));if(keys.includes(previous))$('metric-key').value=previous;drawChart();
    const log=$('log'),atEnd=log.scrollHeight-log.scrollTop-log.clientHeight<32;log.textContent=detail.log_tail||'尚无日志回传';if(atEnd)log.scrollTop=log.scrollHeight;
    $('params').textContent=JSON.stringify(detail.spec.params||{},null,2);$('artifacts').replaceChildren(...detail.artifacts.map(a=>el('div',`${a.name} · ${(a.size/1024).toFixed(1)} KiB`,'file')));empty($('artifacts'),'尚无完整回传的文件');status();
  }
  function drawChart(){
    const points=M.series(detail?.events,$('metric-key').value,detail?.attempt),chart=$('chart');chart.replaceChildren();
    if(points.length){const line=document.createElementNS('http://www.w3.org/2000/svg','polyline');line.setAttribute('points',M.path(points));line.setAttribute('fill','none');line.setAttribute('stroke','#397b61');line.setAttribute('stroke-width','2.5');chart.append(line);const last=points[points.length-1];$('chart-note').textContent=`${points.length} 个回传点 · 最近值 ${number(last.y)} · 横轴为 step（未上报时使用回传时间），纵轴为指标值`;}else $('chart-note').textContent='当前运行尚无可绘制的历史指标';chart.hidden=points.length<2;
  }
  function confirmAction(title,message,path,payload){if(!usable())return;confirmation={path,payload,focus:document.activeElement};$('confirm-title').textContent=title;$('confirm-message').textContent=message;$('confirm').hidden=false;$('confirm-no').focus();}
  function closeConfirm(){const focus=confirmation?.focus;confirmation=null;$('confirm').hidden=true;if(focus?.isConnected)focus.focus();}
  async function submitAction(){
    if(!confirmation||!usable()){closeConfirm();notice('连接状态已变化，请刷新后再操作。');return;}
    const action=confirmation,epoch=generation;closeConfirm();busy=true;status();
    try{await request(action.path,action.payload);if(epoch!==generation)return;notice('请求已记录，请等待节点执行并回传结果。');await refresh();}
    catch(error){if(epoch!==generation)return;if(error.status)failure(error);else{connected=false;notice('未能确认提交结果，请恢复连接后查看状态。请勿立即重复提交。');}}
    finally{if(epoch===generation){busy=false;status();}}
  }
  $('login-form').onsubmit=async event=>{event.preventDefault();generation++;token=$('credential').value.trim();clearCredentials();storage($('remember').checked?'localStorage':'sessionStorage','setItem',key,token);notice('');$('login-button').disabled=true;try{await refresh();}finally{$('login-button').disabled=false;}};
  $('logout').onclick=()=>disconnect('已退出，已清除此设备保存的凭证。');$('refresh').onclick=()=>{notice('');void refresh();};$('back').onclick=()=>switchView(view);
  $('search').oninput=renderJobs;$('filter').onchange=renderJobs;$('metric-key').onchange=drawChart;$('confirm-no').onclick=closeConfirm;$('confirm-yes').onclick=submitAction;
  document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>switchView(b.dataset.tab));
  window.ExperimentMobileBack=()=>{if(!$('confirm').hidden){closeConfirm();return true;}if(selected){switchView(view);return true;}if(view!=='overview'&&token){switchView('overview');return true;}return false;};
  document.addEventListener('keydown',event=>{if($('confirm').hidden)return;if(event.key==='Escape')closeConfirm();if(event.key==='Tab'){event.preventDefault();($('confirm-no')===document.activeElement?$('confirm-yes'):$('confirm-no')).focus();}});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(timer);connected=false;closeConfirm();status();}else void refresh();});window.addEventListener('online',()=>void refresh());window.addEventListener('offline',()=>{connected=false;status();});
  $('login-origin').textContent='当前管理端：'+location.origin;$('remember').checked=Boolean(storage('localStorage','getItem',key));setInterval(status,1000);status();if(token)void refresh();
})();
