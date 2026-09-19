'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('expman_token') || '', state = {jobs:[],nodes:[]}, selected = null, metricData = [];
const startupToken = new URLSearchParams(location.hash.slice(1)).get('token');
if(startupToken){token=startupToken;sessionStorage.setItem('expman_token',token);history.replaceState(null,'',location.pathname+location.search);}
const names = {queued:'排队中',assigned:'已分配',preparing:'准备资源',staging:'准备资源',ready:'离线就绪',starting:'启动中',running:'运行中',succeeded:'已完成',failed:'失败',interrupted:'已中断',paused:'已保存停止',canceled:'已取消'};
const terminal = ['succeeded','failed','interrupted','paused','canceled'];
const projectSelections = new Map(), projectUploads = new Map();
let projectPresets = [];
let currentView='overview', localImportAvailable=null, importJob=null, importDraft=null, importExperimentIndex=0, importJsonDirty=false, importPolling=false;
const viewLabels={overview:['总览','实验、算力和算法项目，都在这里管理。'],experiments:['实验记录','查看进度、指标和结果，管理每一次运行。'],matrices:['实验矩阵','组合数据集与参数，自动分配算力，统一整理结果。'],compute:['算力管理','连接 Windows 与 Ubuntu 算力机，查看资源和接单状态。'],projects:['算法项目','从原始目录导入，检查配置，再分发到算力机。'],settings:['设置与帮助','管理连接与可选的 AI 辅助服务。']};
const requestId = () => Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('');
function node(tag, text, cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=String(text); if(cls)e.className=cls;return e; }
function notify(message) { $('notice').textContent=message; $('notice').hidden=!message; }
async function writeClipboardText(text) {
  if(navigator.clipboard?.writeText){
    try{await navigator.clipboard.writeText(text);return;}catch{}
  }
  // LAN HTTP pages may not expose the Clipboard API. Keep a user-click fallback.
  const previous=document.activeElement,selection=document.getSelection();
  const ranges=selection?Array.from({length:selection.rangeCount},(_,i)=>selection.getRangeAt(i).cloneRange()):[];
  const input=document.createElement('textarea');input.value=text;input.readOnly=true;
  input.style.cssText='position:fixed;left:-9999px;top:0;';document.body.append(input);
  try{
    input.focus({preventScroll:true});input.select();
    if(!document.execCommand('copy'))throw new Error('Clipboard unavailable');
  }finally{
    input.remove();previous?.focus({preventScroll:true});
    if(selection){selection.removeAllRanges();for(const range of ranges)selection.addRange(range);}
  }
}
async function copyDetailText(button,sourceId,label) {
  const text=$(sourceId).textContent;
  if(!text.trim()){notify('暂无可复制的'+label+'。');return;}
  const original=button.dataset.copyLabel||button.textContent;button.dataset.copyLabel=original;button.disabled=true;
  try{
    await writeClipboardText(text);
    button.textContent='已复制';
    notify(label+'已复制。'+(sourceId==='live-log'?'内容为当前页面显示的最近日志；完整日志请下载结果文件。':''));
    clearTimeout(button.copyFeedbackTimer);
    button.copyFeedbackTimer=setTimeout(()=>{button.textContent=original;},1500);
  }catch{notify('复制失败，请选中文本后按 Ctrl+C，或使用浏览器的复制菜单。');}
  finally{button.disabled=button.dataset.copyAvailable==='false';}
}
$('copy-detail-state').onclick=()=>copyDetailText($('copy-detail-state'),'detail-state','状态与错误信息');
$('copy-live-log').onclick=()=>copyDetailText($('copy-live-log'),'live-log','最近日志');
let timingClock = {server: Date.now()/1000, local: performance.now()};
function setTimingClock(timestamp) {
  if(Number.isFinite(timestamp))timingClock={server:timestamp,local:performance.now()};
}
function timingNow() { return timingClock.server+(performance.now()-timingClock.local)/1000; }
function updateTimer(element) {
  const job=element.experimentJob, worker=(state.nodes||[]).find(item=>item.id===job.node_id);
  const online=!!worker&&timingNow()-worker.last_seen<=45;
  const display=ExperimentTiming.describe(job,timingNow(),online);
  element.querySelector('.timer-value').textContent=display.text;
  element.querySelector('.timer-note').textContent=display.note;
  element.classList.toggle('timer-live',display.live);
  element.title=display.note;
}
function jobTimer(job) {
  const element=node('span',undefined,'experiment-timer');element.experimentJob=job;
  element.append(node('span',undefined,'timer-value'),node('small',undefined,'timer-note'));
  updateTimer(element);return element;
}
function updateTimers() { if(token)document.querySelectorAll('.experiment-timer').forEach(updateTimer); }
function formatTimestamp(value) {
  if(!Number.isFinite(value))return '—';
  return new Date(value*1000).toLocaleString('zh-CN',{hour12:false});
}
function renderTimingDetail(job) {
  $('detail-elapsed').replaceChildren(jobTimer(job));
  $('detail-created').textContent=formatTimestamp(job.created);
  $('detail-started').textContent=formatTimestamp(job.timing?.started_at);
  $('detail-finished').textContent=formatTimestamp(job.timing?.finished_at);
  $('detail-timing-note').textContent='只累计实验运行时间，不含排队、资源准备和停止期间。运行中时长为估算，停止后以算力端记录为准。'
    +(job.timing?.complete===false?' 此实验的历史计时不完整。':!job.timing?' 旧实验或尚未更新的算力端可能没有计时记录。':'');
}
async function api(path, body) { const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(!response.ok){let msg=await response.text();try{const data=JSON.parse(msg);msg=data.error||data.message||msg;}catch{}throw new Error(msg);}return response.json(); }
function showView(view){
  if(!viewLabels[view])view='overview';currentView=view;
  document.querySelectorAll('[data-page]').forEach(section=>section.hidden=section.dataset.page!==view);
  document.querySelectorAll('[data-view]').forEach(button=>{button.classList.toggle('active',button.dataset.view===view);if(button.dataset.view===view)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
  $('page-title').textContent=viewLabels[view][0];$('page-description').textContent=viewLabels[view][1];
  history.replaceState(null,'',location.pathname+location.search+'#'+view);
  if(view==='projects'&&token)void loadImportHistory();
  if(view==='matrices'&&token)void loadMatrices();
  if(view==='settings'&&token)void loadAISettings();
  if(view==='experiments')requestAnimationFrame(drawChart);
}
document.querySelectorAll('[data-view]').forEach(button=>{button.onclick=()=>showView(button.dataset.view);button.title=viewLabels[button.dataset.view][0];button.setAttribute('aria-label',button.title);});
let sidebarCollapsed=false;
try{sidebarCollapsed=localStorage.getItem('expman_sidebar_collapsed')==='true';}catch{}
function setSidebarCollapsed(collapsed,persist=true){
  sidebarCollapsed=collapsed;document.body.classList.toggle('sidebar-collapsed',collapsed);
  const label=collapsed?'展开侧栏':'收起侧栏';
  $('sidebar-toggle').setAttribute('aria-expanded',String(!collapsed));$('sidebar-toggle').setAttribute('aria-label',label);$('sidebar-toggle').title=label;
  $('sidebar-toggle-label').textContent=label;
  if(persist)try{localStorage.setItem('expman_sidebar_collapsed',String(collapsed));}catch{}
}
$('sidebar-toggle').onclick=()=>setSidebarCollapsed(!sidebarCollapsed);
setSidebarCollapsed(sidebarCollapsed,false);
document.querySelectorAll('[data-go]').forEach(button=>button.onclick=()=>showView(button.dataset.go));
document.querySelector('.brand').onclick=event=>{event.preventDefault();showView('overview');};
$('settings-address').textContent=location.origin;
$('quick-worker').onclick=()=>{showView('compute');$('add-worker').click();};
$('quick-run').onclick=()=>showView('projects');
$('quick-import').onclick=()=>{showView('projects');$('import-local').click();};
function objectFields(container,values,onChange,definitions={},labels={}){
  container.replaceChildren();
  for(const key of Object.keys(values)){
    const value=values[key],definition=definitions[key]||{},label=node('label',labels[key]||key);let input;
    if(Array.isArray(definition.choices)){input=node('select');for(const choice of definition.choices){const option=node('option',choice);option.value=JSON.stringify(choice);option.selected=Object.is(choice,value);input.append(option);}input.onchange=()=>onChange(key,JSON.parse(input.value));}
    else if(typeof value==='boolean'||definition.type==='boolean'){input=node('input');input.type='checkbox';input.checked=value===true;input.onchange=()=>onChange(key,input.checked);}
    else if(typeof value==='number'||['number','integer'].includes(definition.type)){input=node('input');input.type='number';input.step=definition.type==='integer'?'1':'any';input.value=value??'';input.required=!!definition.required;input.oninput=()=>{input.setCustomValidity('');if(input.value===''||!Number.isFinite(Number(input.value))){input.setCustomValidity('请输入有效数字');return;}onChange(key,Number(input.value));};}
    else if(value!==null&&typeof value==='object'){input=node('textarea');input.rows=3;input.value=JSON.stringify(value);input.oninput=()=>{try{onChange(key,JSON.parse(input.value));input.setCustomValidity('');}catch{input.setCustomValidity('请输入有效 JSON');}};}
    else{input=node('input');input.type='text';input.value=value??'';input.required=!!definition.required;input.oninput=()=>onChange(key,input.value);}
    input.setAttribute('aria-label',labels[key]||key);label.append(input);if(definition.description)label.append(node('small',definition.description));container.append(label);
  }
  if(!container.children.length)container.append(node('p','没有需要填写的参数。','muted'));
}
function renderProjectParameters(){let values;try{values=JSON.parse($('project-params').value);}catch{return;}if(!values||typeof values!=='object'||Array.isArray(values))return;objectFields($('project-param-fields'),values,(key,value)=>{values[key]=value;$('project-params').value=JSON.stringify(values,null,2);});}
$('project-params').onchange=renderProjectParameters;
function demoTemplate(){ $('spec').value=JSON.stringify({name:'第一次演示',algorithm:'demo',group:'学习使用',metric_protocol:'demo-v1',backend:'demo',params:{steps:30,delay:0.3,seed:42},resources:{gpu_memory_mb:0,cpu:1,ram_mb:256,exclusive:false}},null,2); }
$('upload-project').onclick=()=>$('project-file').click();
$('project-file').onchange=async event=>{
  const file=event.target.files[0];if(!file)return;
  const key=JSON.stringify([file.name,file.size,file.lastModified]);
  const upload_id=projectUploads.get(key)||requestId();projectUploads.set(key,upload_id);
  $('upload-project').disabled=true;$('project-progress').hidden=false;
  try{
    if(!file.size||file.size>32*1024**3)throw new Error('项目包必须为 1 byte–32 GiB。');
    let reply=await api('/api/projects/upload',{upload_id,size:file.size,offset:0,data:''});
    while(!reply.complete){
      const offset=reply.offset;
      if(!Number.isSafeInteger(offset)||offset<0||offset>=file.size)throw new Error('主控返回了无效上传进度。');
      $('project-progress').textContent=`正在上传 ${file.name} · ${(100*offset/file.size).toFixed(1)}%`;
      const bytes=new Uint8Array(await file.slice(offset,offset+512*1024).arrayBuffer());
      let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
      reply=await api('/api/projects/upload',{upload_id,size:file.size,offset,data:btoa(binary)});
    }
    $('project-progress').textContent=`已上传 ${reply.project.name}。请在下方勾选算力机，再点击“分发到所选节点”。`;
    await refresh();
  }catch(error){$('project-progress').textContent='上传失败：'+error.message+' 重新选择同一文件可继续上传。';}
  finally{$('upload-project').disabled=false;event.target.value='';}
};
function renderProjects(){
  $('projects').replaceChildren();
  for(const project of state.projects||[]){
    const box=node('div',undefined,'node');
    box.append(node('strong',project.name),node('p',`版本 ${project.digest.slice(0,12)} · ${(project.size/1024/1024).toFixed(1)} MiB`,'project-meta'));
    const chosen=projectSelections.get(project.digest)||new Set();projectSelections.set(project.digest,chosen);
    if(!(state.nodes||[]).length){box.append(node('p','还没有接入算力机。先添加一台机器，再分发此项目。','muted'));const add=node('button','添加算力机','subtle');add.onclick=()=>{showView('compute');$('add-worker').click();};box.append(add);}
    for(const worker of state.nodes||[]){
      const supported=(worker.snapshot?.capabilities||[]).includes('project-bundle-v1');
      const deployment=(project.deployments||[]).find(item=>item.node_id===worker.id);
      const label=node('label',undefined,'project-worker'),check=node('input');check.type='checkbox';check.value=worker.id;check.checked=chosen.has(worker.id);check.disabled=!supported;
      check.onchange=()=>check.checked?chosen.add(worker.id):chosen.delete(worker.id);
      const statuses={queued:'等待节点领取',downloading:'下载中',installing:'安装中',installed:'已安装',failed:'安装失败'};
      label.append(check,node('span',`${worker.id} · ${worker.online?'在线':'离线'} · ${supported?(statuses[deployment?.status]||'未分发'):'需更新算力代理'}`));box.append(label);
      if(deployment?.detail)box.append(node('p',deployment.detail,'project-deployment-detail'));
    }
    const deploy=node('button','分发到所选节点','subtle');
    deploy.onclick=async()=>{try{if(!chosen.size)throw new Error('先勾选目标算力机。');await api('/api/projects/deploy',{digest:project.digest,node_ids:[...chosen]});notify('分发已排队，节点会自动下载和安装。安装完成后点击“创建实验”。');await refresh();}catch(error){notify(error.message);}};
    const create=node('button','创建实验');create.onclick=()=>openProjectRun(project);
    const remove=node('button','删除项目','text-button danger-text');remove.onclick=()=>void deleteProject(project,remove);
    box.append(deploy,create,remove);$('projects').append(box);
  }
  if(!(state.projects||[]).length)$('projects').append(node('p','项目库还是空的。点击“从文件夹导入”，或上传已有的 ZIP 项目包。','muted'));
  renderProjectCleanups();
}
function openProjectRun(project){
  showView('projects');
  $('project-run-form').hidden=true;projectPresets=[];
  if(!(state.nodes||[]).length){notify('当前没有接入的算力机。先到“算力管理”添加机器并导入配对凭证，再分发项目、创建实验。');return;}
  for(const worker of state.nodes||[]){
    if(!(project.deployments||[]).some(item=>item.node_id===worker.id&&item.status==='installed'))continue;
    for(const template of worker.snapshot?.task_templates||[]){
      if(template.project_id===project.project_id&&(!project.bundle_id||template.project_bundle_id===project.bundle_id))projectPresets.push({worker:worker.id,template});
    }
  }
  if(!projectPresets.length){notify('尚无已安装且回报实验配置的节点。请先分发，等待节点显示“已安装”。');return;}
  const seen=new Set();projectPresets=projectPresets.filter(item=>{const key=JSON.stringify([item.template.project_id,item.template.project_bundle_id,item.template.experiment_id||item.template.name,item.template.params]);if(seen.has(key))return false;seen.add(key);return true;});
  $('project-preset').replaceChildren(...projectPresets.map((item,i)=>{const option=node('option',item.template.name);option.value=String(i);return option;}));
  renderScheduling('project-scheduling',{mode:'auto'},0);
  $('project-run-title').textContent='创建实验 / New experiment · '+project.name;$('project-run-form').hidden=false;fillProjectPreset();$('project-run-form').scrollIntoView({behavior:'smooth'});
}
function fillProjectPreset(){const selectedPreset=projectPresets[Number($('project-preset').value)];if(!selectedPreset)return;$('project-run-name').value=selectedPreset.template.name;$('project-params').value=JSON.stringify(selectedPreset.template.params,null,2);renderProjectParameters();}
$('project-preset').onchange=fillProjectPreset;
$('project-run-close').onclick=()=>{$('project-run-form').hidden=true;};
$('project-run-form').onsubmit=async event=>{
  event.preventDefault();$('project-run-submit').disabled=true;
  try{
    const preset=projectPresets[Number($('project-preset').value)];if(!preset)throw new Error('请选择节点和实验配置。');
    const spec=JSON.parse(JSON.stringify(preset.template));spec.name=$('project-run-name').value.trim();spec.params=JSON.parse($('project-params').value);
    applyScheduling(spec,readScheduling('project-scheduling'));
    if(!spec.params||typeof spec.params!=='object'||Array.isArray(spec.params))throw new Error('参数必须是 JSON 对象。');
    const result=await api('/api/jobs',{request_id:requestId(),spec});notify(`已提交 ${result.ids.length} 个实验。系统会按所选算力策略匹配节点。`);showView('experiments');await refresh();
  }catch(error){notify('提交失败：'+error.message);}finally{$('project-run-submit').disabled=false;}
};
$('spec').value='';
$('demo-template').onclick=demoTemplate;
$('import-task').onclick=()=>$('task-file').click();
$('task-file').onchange=async event=>{
  const file=event.target.files[0];if(!file)return;
  try{
    if(file.size>2*1024*1024)throw new Error('任务文件不能超过 2 MiB。');
    const spec=JSON.parse((await file.text()).replace(/^\uFEFF/,''));
    if(!spec||typeof spec!=='object'||Array.isArray(spec))throw new Error('任务文件必须是 JSON 对象。');
    if('token' in spec||'admin_token' in spec)throw new Error('这是含凭证的配置文件，请选择 .task.json 实验任务文件。');
    if(!['docker','demo'].includes(spec.backend)||!spec.params||typeof spec.params!=='object'||Array.isArray(spec.params))throw new Error('文件缺少有效的 backend 或 params，请选择完整任务文件。');
    $('spec').value=JSON.stringify(spec,null,2);$('grid').value='{}';
    notify(`已导入 ${file.name}，参数组合设为单次运行。核对后点击“提交到队列”启动。`);
  }catch(error){notify('导入失败：'+error.message);}finally{event.target.value='';}
};
$('login-form').onsubmit=async event=>{event.preventDefault();token=$('token').value.trim();sessionStorage.setItem('expman_token',token);await refresh();};
$('logout').onclick=()=>{sessionStorage.removeItem('expman_token');token='';$('login').hidden=false;$('workspace').hidden=true;$('sidebar').hidden=true;document.body.classList.remove('authenticated');$('connection').textContent='未连接';};
$('add-worker').onclick=async()=>{
  $('pair-form').hidden=false;
  try{const info=await api('/api/setup-info');$('pair-addresses').replaceChildren(...info.addresses.map(url=>{const option=node('option');option.value=url;return option;}));if(!$('pair-url').value)$('pair-url').value=info.addresses[0]||location.origin;}
  catch(error){notify('请确认主控端版本支持配对 / Check controller version: '+error.message);}
};
$('pair-close').onclick=()=>{$('pair-form').hidden=true;};
$('pair-form').onsubmit=async event=>{
  event.preventDefault();
  try{
    const pairing=await api('/api/enroll',{node_id:$('pair-name').value.trim(),hub_url:$('pair-url').value.trim()});
    const url=URL.createObjectURL(new Blob([JSON.stringify(pairing,null,2)+'\n'],{type:'application/json'}));
    const link=node('a');link.href=url;link.download=pairing.node_id+'.pairing.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    notify('配对文件已下载。放到算力端解压目录，再启动算力端。/ Put the pairing file beside Start-Worker and launch it.');
    $('pair-form').hidden=true;await refresh();
  }catch(error){notify('配对失败 / Pairing failed: '+error.message);}
};
async function refresh(){if(!token)return;try{state=await api('/api/state');setTimingClock(state.time);$('login').hidden=true;$('workspace').hidden=false;$('sidebar').hidden=false;document.body.classList.add('authenticated');$('connection').textContent='● 管理中心在线';render();if(localImportAvailable===null)void loadImportHistory();if(selected)await detail(selected,false);}catch(error){$('connection').textContent='○ 无法连接';notify('暂时无法获取管理中心状态，请检查程序是否运行、网络和令牌。已经准备好的节点任务不依赖此页面继续运行。 '+error.message.slice(0,150));}}
function render(){const jobs=state.jobs||[],nodes=state.nodes||[];const online=n=>Date.now()/1000-n.last_seen<45;$('count-queue').textContent=jobs.filter(j=>['queued','assigned','preparing','ready'].includes(j.state)).length;$('count-running').textContent=jobs.filter(j=>['starting','running'].includes(j.state)).length;$('count-done').textContent=jobs.filter(j=>j.state==='succeeded').length;$('count-nodes').textContent=nodes.filter(online).length;
  $('nodes').replaceChildren();for(const n of nodes){const snap=n.snapshot||{},box=node('div',undefined,'node'),heading=node('div',undefined,'node-name');heading.append(node('span',n.id),node('span',online(n)?'在线':'离线','badge'));box.append(heading,node('p',n.mode==='drain'?'已暂停接单和启动新任务':online(n)?'允许运行 · 本地策略仍需满足':'已有任务保持归属，等待重新连接'));
    if(!(snap.gpus||[]).length)box.append(node('p',snap.allow_demo?'CPU 演示节点':'GPU 尚未就绪或未授权'));
    for(const g of snap.gpus||[]){const gpu=node('div',`${g.name} · 空闲 ${Number.isFinite(g.free_mb)?(g.free_mb/1024).toFixed(1):'?'} / ${(g.total_mb/1024).toFixed(1)} GiB`,'gpu'),bar=node('div',undefined,'bar'),fill=node('i');fill.style.width=Math.max(0,Math.min(100,100*(1-g.free_mb/g.total_mb)))+'%';bar.append(fill);gpu.append(bar);box.append(gpu);}
    if(Number.isFinite(snap.pending_uploads))box.append(node('p',`待回传文件：${snap.pending_uploads}`));
    const templateDetails=node('details',undefined,'worker-templates');templateDetails.append(node('summary','高级：节点任务模板'));
    for(const template of (Array.isArray(snap.task_templates)?snap.task_templates:[])){
      const use=node('button','填入任务 / Use: '+String(template.name||'template'),'subtle');
      use.onclick=()=>{$('spec').value=JSON.stringify(template,null,2);$('grid').value='{}';showView('experiments');$('advanced-submit').open=true;notify('已填入节点任务，核对后点击“提交到队列”。');$('submit-form').scrollIntoView({behavior:'smooth'});};templateDetails.append(use);
    }
    if(templateDetails.children.length>1)box.append(templateDetails);const button=node('button',n.mode==='drain'?'恢复接单':'暂停接单','subtle');button.onclick=async()=>{try{await api('/api/node-mode',{node_id:n.id,mode:n.mode==='drain'?'run':'drain'});notify('策略已记录，节点下次连接后生效。暂停接单不会终止正在运行的实验。');await refresh();}catch(e){notify(e.message);}};box.append(button);$('nodes').append(box);}
  if(!nodes.length)$('nodes').append(node('p','尚无节点。点击“添加算力机”，再启动算力端。','muted'));
  renderProjects();renderJobs();renderOverview();
}
function renderJobs(){const filter=$('filter').value.toLowerCase();const jobs=(state.jobs||[]).filter(j=>JSON.stringify([j.spec.name,j.spec.algorithm,j.spec.group]).toLowerCase().includes(filter));$('jobs').replaceChildren();$('empty').hidden=jobs.length>0;for(const j of jobs){const row=node('tr'),title=node('td');title.append(node('strong',j.spec.name),node('small',`${j.spec.algorithm} / ${j.spec.group}`));const status=node('td');status.append(node('span',names[j.state]||j.state,'badge '+j.state));const met=j.metrics||{};row.append(title,status,node('td',j.node_id||'等待匹配'),(()=>{const cell=node('td');cell.append(jobTimer(j));return cell;})(),node('td',Object.entries(met).filter(([k])=>!['step','time','attempt'].includes(k)).slice(0,2).map(([k,v])=>`${k}: ${typeof v==='number'?v.toPrecision(4):v}`).join(' · ')||'—'));row.onclick=()=>detail(j.id,true);$('jobs').append(row);}}
$('filter').oninput=renderJobs;
$('submit-form').onsubmit=async event=>{event.preventDefault();$('submit-button').disabled=true;try{const spec=JSON.parse($('spec').value),grid=JSON.parse($('grid').value||'{}'),request_id=globalThis.crypto?.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random();const result=await api('/api/jobs',{spec,grid,request_id});notify(`已提交 ${result.ids.length} 个实验。没有符合环境、数据和节点策略的机器时，实验会保留在队列。`);await refresh();}catch(error){notify('提交失败：'+error.message);}finally{$('submit-button').disabled=false;}};
async function download(path,name){try{const response=await fetch(path,{headers:{Authorization:'Bearer '+token}});if(!response.ok)throw new Error(await response.text());const objectURL=URL.createObjectURL(await response.blob()),a=node('a');a.href=objectURL;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(objectURL),1000);}catch(e){notify(e.message);}}
$('export').onclick=()=>download('/api/results.csv','实验结果.csv');
async function detail(id,scroll){selected=id;try{const data=await api('/api/job?id='+encodeURIComponent(id)),j=data.job||data;setTimingClock(data.time);renderTimingDetail(j);$('detail').hidden=false;$('detail-name').textContent=j.spec.name;$('detail-state').textContent=`${names[j.state]||j.state} · 节点 ${j.node_id||'未分配'} · 尝试 ${j.attempt||1} · ${typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||'')}`;$('detail-spec').textContent=JSON.stringify({id:j.id,source:j.spec.source,params:j.spec.params,resources:j.spec.resources,environments:j.spec.environments,metric_protocol:j.spec.metric_protocol},null,2);
  const events=data.events||j.events||[];$('events').textContent=events.slice(-30).map(e=>JSON.stringify(e)).join('\n');$('actions').replaceChildren();const options=terminal.includes(j.state)?(['paused','interrupted','failed'].includes(j.state)&&j.spec.resume_supported!==false?[['resume','从检查点恢复']]:[]):[[j.spec.resume_supported===false?'cancel':'stop',j.spec.resume_supported===false?'停止实验（无续训）':'请求保存并停止'],...(j.spec.resume_supported===false?[]:[['cancel','取消实验']])];for(const [action,label] of options){const b=node('button',label,'subtle');b.onclick=async()=>{try{await api('/api/action',{job_id:id,action});notify('请求已记录；节点收到并执行后才会更新状态。离线节点不会立即响应。');await detail(id,false);}catch(e){notify(e.message);}};$('actions').append(b);}
  $('artifacts').replaceChildren();for(const a of data.artifacts||j.artifacts||[]){const b=node('button',`${a.name} · ${(a.size/1024).toFixed(1)} KiB`,'subtle');b.onclick=()=>download('/api/artifact?job_id='+encodeURIComponent(id)+'&sha256='+encodeURIComponent(a.sha256),a.name.split('/').pop());$('artifacts').append(b);}if(!$('artifacts').children.length)$('artifacts').append(node('p','还没有完整回传的文件。运行状态与文件归档分别同步。','muted'));
  document.getElementById("live-log").textContent=j.log_tail||'等待节点回传日志';$('copy-live-log').dataset.copyAvailable=String(Boolean(j.log_tail?.trim()));$('copy-live-log').disabled=!j.log_tail?.trim();metricData=events.map(e=>e.metrics||e.data?.metrics||e.payload?.metrics||{}).filter(m=>Number.isFinite(m.step));if(j.metrics&&Number.isFinite(j.metrics.step))metricData.push(j.metrics);const unique=new Map();for(const sample of metricData)unique.set(sample.step,{...unique.get(sample.step),...sample});metricData=[...unique.values()].sort((a,b)=>a.step-b.step);const old=$('metric-select').value,keys=[...new Set(metricData.flatMap(m=>Object.keys(m)))].filter(k=>!['step','time','attempt'].includes(k));$('metric-select').replaceChildren(...keys.map(k=>{const o=node('option',k);o.value=k;return o;}));if(keys.includes(old))$('metric-select').value=old;drawChart();if(scroll)$('detail').scrollIntoView({behavior:'smooth',block:'start'});
}catch(e){notify('读取详情失败：'+e.message);}}
function drawChart(){
  const canvas=$('chart'),width=canvas.clientWidth,height=canvas.clientHeight;
  if(!width||!height)return;
  const ratio=Math.max(1,window.devicePixelRatio||1),ctx=canvas.getContext('2d');
  const targetWidth=Math.round(width*ratio),targetHeight=Math.round(height*ratio);
  if(canvas.width!==targetWidth||canvas.height!==targetHeight){canvas.width=targetWidth;canvas.height=targetHeight;}
  ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);
  ctx.font='12px "Segoe UI", "Microsoft YaHei", sans-serif';ctx.fillStyle='#71827d';
  const key=$('metric-select').value,points=metricData.filter(m=>Number.isFinite(m[key])&&Number.isFinite(m.step));
  if(!points.length){ctx.textAlign='center';ctx.fillText('等待节点回传指标',width/2,height/2);$('chart-summary').textContent='尚无可绘制的数值指标';return;}
  const values=points.map(p=>p[key]),min=Math.min(...values),max=Math.max(...values),padding=(max-min)*0.08||Math.max(Math.abs(max)*0.05,0.01),low=min-padding,high=max+padding;
  const first=points[0].step,last=points.at(-1).step,left=72,right=22,top=22,bottom=38,plotWidth=Math.max(1,width-left-right),plotHeight=height-top-bottom;
  const px=p=>left+(last===first?0.5:(p.step-first)/(last-first))*plotWidth,py=p=>top+(high-p[key])/(high-low)*plotHeight;
  ctx.lineWidth=1;ctx.textAlign='right';ctx.textBaseline='middle';
  for(let i=0;i<=4;i++){const y=top+i*plotHeight/4,value=high-i*(high-low)/4;ctx.strokeStyle='#e7eeea';ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(width-right,y);ctx.stroke();ctx.fillStyle='#71827d';ctx.fillText(Number(value.toPrecision(4)).toString(),left-12,y);}
  const gradient=ctx.createLinearGradient(0,top,0,height-bottom);gradient.addColorStop(0,'#5f9d812e');gradient.addColorStop(1,'#5f9d8104');ctx.beginPath();ctx.moveTo(px(points[0]),height-bottom);for(const p of points)ctx.lineTo(px(p),py(p));ctx.lineTo(px(points.at(-1)),height-bottom);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();
  ctx.strokeStyle='#246657';ctx.lineWidth=2;ctx.lineJoin='round';ctx.lineCap='round';ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(px(p),py(p)):ctx.moveTo(px(p),py(p)));ctx.stroke();
  for(const p of points.length<35?points:[points.at(-1)]){ctx.beginPath();ctx.arc(px(p),py(p),3,0,Math.PI*2);ctx.fillStyle='#246657';ctx.fill();ctx.strokeStyle='#fff';ctx.lineWidth=1.5;ctx.stroke();}
  ctx.fillStyle='#71827d';ctx.textAlign='left';ctx.fillText('step '+first,left,height-15);if(last!==first){ctx.textAlign='right';ctx.fillText(String(last),width-right,height-15);}
  $('chart-summary').textContent=`${key} · ${points.length} 个记录点 · 最新 ${Number(points.at(-1)[key].toPrecision(6))} · 范围 ${Number(min.toPrecision(4))}–${Number(max.toPrecision(4))}`;
}
if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>requestAnimationFrame(drawChart)).observe($('chart'));
window.addEventListener('resize',()=>requestAnimationFrame(drawChart));
$('metric-select').onchange=drawChart;$('close-detail').onclick=()=>{selected=null;$('detail').hidden=true;};

function renderOverview(){
  $('overview-workers').replaceChildren();
  for(const worker of (state.nodes||[]).slice(0,5)){
    const row=node('div',undefined,'list-row'),label=node('div');label.append(node('strong',worker.id));
    const gpus=worker.snapshot?.gpus||[];label.append(node('small',gpus.length?gpus.map(g=>g.name.replace(/^NVIDIA (GeForce )?/, '')).join(' / '):'等待设备信息'));
    const online=Date.now()/1000-worker.last_seen<45;row.append(label,node('span',online?(worker.mode==='drain'?'暂停接单':'在线'):'离线','badge '+(online?'succeeded':'')));$('overview-workers').append(row);
  }
  if(!$('overview-workers').children.length)$('overview-workers').append(node('p','还没有算力机。添加一台机器，开始准备实验。','muted'));
  $('recent-jobs').replaceChildren();
  for(const job of (state.jobs||[]).slice(0,5)){
    const row=node('div',undefined,'list-row clickable'),label=node('div');label.append(node('strong',job.spec.name),node('small',`${job.spec.algorithm} · ${job.node_id||'等待分配节点'}`));
    const summary=node('div',undefined,'recent-job-status');summary.append(jobTimer(job),node('span',names[job.state]||job.state,'badge '+job.state));row.append(label,summary);row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label','查看实验 '+job.spec.name);row.onclick=()=>{showView('experiments');void detail(job.id,true);};row.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();row.click();}};$('recent-jobs').append(row);
  }
  if(!$('recent-jobs').children.length)$('recent-jobs').append(node('p','这里会显示最近的实验。先导入算法，或从项目库创建一次运行。','muted'));
}

const importStatuses={scanning:'正在读取入口和参数',ready:'配置草稿已准备好',saving:'正在保存配置并检查文件',building:'正在打包并加入项目库',published:'已加入项目库',failed:'操作失败',interrupted:'操作已中断，可重新保存或发布',browsing:'请选择算法文件夹',selected:'已选择文件夹',canceled:'已取消选择'};
const importPhases={'Discovering entry and parameters without running source':'静态提取原入口和参数','Counting selected code and dataset files':'统计代码与数据文件','Review parameters, dependencies and selected files':'请检查参数、依赖与分发文件','Checking selected files':'正在检查选中的文件','Configuration saved; review and publish':'配置已保存','Packaging an immutable snapshot; original source remains unchanged':'正在创建项目版本快照','Adding the verified snapshot to the algorithm library':'正在写入算法项目库','Available in the algorithm library; select workers to deploy':'接下来选择算力机并分发','Folder selected':'点击“读取入口和参数”继续','Folder selection canceled':'可重新选择文件夹'};
const busyImports=new Set(['scanning','saving','building','browsing']);
const copy=value=>JSON.parse(JSON.stringify(value));
function importMessage(message){$('import-status').textContent=message;$('import-status').hidden=!message;}
function importBusy(busy){importPolling=busy;for(const form of [$('import-source-form'),$('import-review-form')])for(const input of form.querySelectorAll('input,select,textarea,button'))input.disabled=busy;if(!busy&&importDraft)$('import-delete-experiment').disabled=importDraft.experiments.length<=1;}
function setImportStep(step){['source','review','publish'].forEach(name=>$('import-step-'+name).classList.toggle('active',name===step));}
function updateImportJSON(){if(!importDraft||importJsonDirty)return;$('import-project-json').value=JSON.stringify(importDraft.project,null,2);$('import-harness-json').value=JSON.stringify(importDraft.harness,null,2);$('import-experiments-json').value=JSON.stringify(importDraft.experiments,null,2);}
function draftChanged(){aiSuggestion=null;$('import-ai-preview').hidden=true;if(!$('import-reviewed').disabled)$('import-reviewed').checked=false;updateImportJSON();}
async function loadImportHistory(){
  if(!token)return;
  try{
    const result=await api('/api/local/imports');localImportAvailable=result.available===true;
    $('import-local').hidden=!localImportAvailable;$('local-import-unavailable').hidden=localImportAvailable;$('import-browse').hidden=!result.picker_available;
    $('settings-local').textContent=localImportAvailable?'可从此管理端的文件夹导入算法项目':'当前访问方式仅支持上传项目包';
    $('import-history-card').hidden=!localImportAvailable||!result.imports?.length;$('import-history').replaceChildren();
    for(const item of result.imports||[]){const row=node('div',undefined,'list-row'),label=node('div');label.append(node('strong',item.name||item.source||'选择文件夹'),node('small',`${importStatuses[item.status]||item.status}${item.error?' · '+item.error:''}`));const edit=node('button',busyImports.has(item.status)?'查看进度':'打开','subtle');edit.onclick=()=>void openImport(item.id);row.append(label,edit);$('import-history').append(row);}
  }catch(error){localImportAvailable=false;$('import-local').hidden=true;$('local-import-unavailable').hidden=false;$('settings-local').textContent='请在管理端本机打开实验台以使用文件夹导入；也可上传 ZIP 项目包。';}
}
async function openImport(id){
  if(importPolling){notify('当前导入操作仍在进行，完成后可打开另一份草稿。');return;}
  showView('projects');$('import-wizard').hidden=false;$('import-review-form').hidden=true;if(importJob?.id!==id){$('import-sample-log').value='';$('import-ai-instructions').value='';$('import-ai-source').checked=false;}importBusy(true);
  try{let job=await api('/api/local/imports/item?id='+encodeURIComponent(id));importJob=job;importDraft=null;job=await waitImportJob(job);if(job.draft)renderImportDraft(job.draft);if(job.status==='selected')$('import-source').value=job.source;}
  catch(error){importMessage('读取导入记录失败：'+error.message);}finally{importBusy(false);}
  $('import-wizard').scrollIntoView({behavior:'smooth',block:'start'});
}
async function waitImportJob(job){
  const id=job.id;importJob=job;
  while(true){
    if(importJob?.id!==id)throw new Error('已切换到另一份导入配置。');
    const status=importStatuses[job.status]||job.status,phase=importPhases[job.phase]||importStatuses[job.phase]||job.phase;importMessage(status+(phase&&phase!==status?' · '+phase:'')+(job.error?'：'+job.error:''));
    if(job.status==='building')setImportStep('publish');
    if(!busyImports.has(job.status))break;
    await new Promise(resolve=>setTimeout(resolve,900));if(!token)throw new Error('管理令牌已清除，请重新连接。');
    job=await api('/api/local/imports/item?id='+encodeURIComponent(id));importJob=job;
  }
  if(job.status==='published'){setImportStep('publish');notify('项目已加入算法库。勾选目标算力机并分发，节点安装完成后即可创建实验。');await refresh();}
  await loadImportHistory();return job;
}
function renderImportExperiment(){
  const experiment=importDraft.experiments[importExperimentIndex];if(!experiment)return;
  $('import-experiment').replaceChildren(...importDraft.experiments.map((item,index)=>{const option=node('option',item.name||item.id);option.value=String(index);return option;}));$('import-experiment').value=String(importExperimentIndex);
  $('import-experiment-name').value=experiment.name||experiment.id;$('import-experiment-id').value=experiment.id;
  const definitions=importDraft.harness.parameters||{},values={...experiment.params};
  for(const [key,definition] of Object.entries(definitions)){if(!(key in values)&&definition.required)values[key]=definition.default??'';}
  objectFields($('import-param-fields'),values,(key,value)=>{experiment.params[key]=value;draftChanged();},definitions);
  $('import-delete-experiment').disabled=importDraft.experiments.length<=1;
}
function discoveryNote(note){
  const translations={
    'Entry invokes training at module scope; discovery did not import it or run --help.':'这个入口一加载就会开始训练。本次仅阅读源码来识别参数，还没有启动训练。',
    'Dependency hints are import names, not verified package names or versions; review the runtime environment.':'下方依赖来自源码里的 import。请核对哪些模块需要安装，以及对应的安装包名称和版本；同名模块不一定属于同名安装包。',
    'Evaluation flags (for example --do_eval) are not evidence of training resume support.':'do_eval 这类选项用于测试模型，不能据此认定支持断点续训。当前配置不会自动开启续训。',
    'Multiple entry candidates found; the selected entry is a filename-based suggestion.':'找到了多个可能的入口。当前选择是按文件名推测的，请在“识别到的原入口”中确认实际训练入口。',
    'Resume/checkpoint flags are review candidates only; native training resume remains disabled until explicitly configured.':'发现了可能与检查点有关的参数，请先确认它们确实能继续训练。未配置原生续训命令前，续训保持关闭。',
    'Windows batch entry requires Windows; Linux GPU containers need an equivalent Python/bash entry chosen explicitly.':'当前入口是 Windows 批处理文件。算力端使用 Linux GPU 容器，请选择批处理实际调用的 Python 或 Bash 入口。',
    'Shell entry arguments were not inferred; configure its command and bindings manually.':'已找到脚本入口，但还不能自动提取它的参数。请在高级 Harness 配置中检查调用命令和参数绑定。'
  };
  if(translations[note])return translations[note];
  let match=note.match(/^Entry overwrites CUDA_VISIBLE_DEVICES from argument (.+);/);
  if(match)return `原入口用 ${match[1]} 参数选择显卡。请保留该固定参数中的 {env.CUDA_VISIBLE_DEVICES}，让它使用节点分配的 GPU。`;
  match=note.match(/^Line (\d+): directory is joined by string addition; preserve a trailing separator\.$/);
  if(match)return `源码第 ${match[1]} 行直接把目录和文件名拼在一起，因此对应目录参数末尾需要保留 /。自动生成的数据目录已按这个要求填写。`;
  match=note.match(/^Supply required parameter: (.+)$/);if(match)return `必填参数 ${match[1]} 没有默认值，请在实验配置中填写。`;
  match=note.match(/^Set the dataset path for: (.+)$/);if(match)return `没有找到 ${match[1]} 指向的数据。请检查数据目录并填写实际路径。`;
  match=note.match(/^Configure argument manually: (.+)$/);if(match)return `参数 ${match[1]} 的写法暂时无法自动提取，请在高级 Harness 配置中补充它的定义和绑定。`;
  return note;
}
function renderImportDraft(draft){
  importDraft=copy(draft);importJsonDirty=false;importExperimentIndex=0;
  const {project,harness,discovery,preview}=importDraft;
  $('import-review-form').hidden=false;$('import-source').value=project.source;$('import-name').value=project.name;$('import-project-id').value=project.project_id;$('import-cwd').value=harness.cwd;
  $('import-entry').replaceChildren(...(discovery?.entries||[]).map(entry=>{const option=node('option',`${entry.path} · ${entry.kind}`);option.value=entry.path;option.selected=entry.path===discovery.selected_entry;return option;}));
  $('import-command-preview').textContent=(harness.command||[]).join(' ');
  $('import-warnings').replaceChildren(...(project.review_notes||[]).map(note=>{const item=node('li',discoveryNote(note));item.title=note;return item;}));$('import-warnings-box').hidden=!$('import-warnings').children.length;
  objectFields($('import-fixed-fields'),harness.fixed_params||{},(key,value)=>{harness.fixed_params[key]=value;draftChanged();});
  renderImportExperiment();$('import-assets').replaceChildren();
  for(const [alias,asset] of Object.entries(project.assets||{})){
    const group=node('div',undefined,'asset-group');group.append(node('h4','数据资源 · '+alias));const fields=node('div',undefined,'form-grid'),pathLabel=node('label','本机数据文件夹'),path=node('input');path.value=asset.path;path.required=true;path.oninput=()=>{asset.path=path.value;draftChanged();};pathLabel.append(path);
    const patternsLabel=node('label','包含文件（每行一个文件名或通配符）'),patterns=node('textarea');patterns.rows=2;patterns.value=(asset.include||['**/*']).join('\n');patterns.oninput=()=>{asset.include=patterns.value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);draftChanged();};patternsLabel.append(patterns);fields.append(pathLabel,patternsLabel);group.append(fields);$('import-assets').append(group);
  }
  if(!$('import-assets').children.length)$('import-assets').append(node('p','未自动识别数据目录。若算法需要外部数据，请在高级项目配置的 assets 中添加路径。','muted'));
  $('import-modules').value=(project.runtime?.imports||[]).join('\n');$('import-requirements').value=(project.runtime?.requirements||[]).join('\n');
  objectFields($('import-resource-fields'),project.resources||{},(key,value)=>{project.resources[key]=value;draftChanged();},{cpu:{type:'integer'},ram_mb:{type:'integer'},gpu_memory_mb:{type:'integer'}},{cpu:'CPU 核心预算',ram_mb:'内存预算 (MiB)',gpu_memory_mb:'显存预算 (MiB)',exclusive:'独占 GPU'});
  $('import-resume-status').textContent=harness.resume?.supported?'续训：使用配置中的原生续训命令':'续训：未启用，原算法没有配置原生续训入口';
  $('import-file-summary').textContent=preview?`代码 ${preview.source_files} 个文件 · 数据 ${preview.asset_files} 个文件 · 共 ${((preview.source_bytes+preview.asset_bytes)/1024/1024).toFixed(1)} MiB${preview.truncated?'（下方显示部分文件）':''}`:'保存草稿后检查分发文件。';
  $('import-file-preview').replaceChildren(...(preview?.files||[]).map(file=>{const row=node('div');row.append(node('span',file.path),node('span',(file.bytes/1024).toFixed(1)+' KiB'));return row;}));
  $('import-reviewed').checked=false;setImportStep('review');updateImportJSON();renderImportEditors();
}
async function startImport(entry){
  importDraft=null;importBusy(true);$('import-sample-log').value='';$('import-ai-instructions').value='';$('import-ai-source').checked=false;$('import-review-form').hidden=true;setImportStep('source');importMessage('正在读取项目，不会执行算法源码…');
  try{const body={source:$('import-source').value.trim()};if(entry)body.entry=entry;const job=await waitImportJob(await api('/api/local/imports/start',body));if(job.draft)renderImportDraft(job.draft);}
  catch(error){importMessage('读取失败：'+error.message);}finally{importBusy(false);}
}
$('import-local').onclick=async()=>{showView('projects');if(localImportAvailable===null)await loadImportHistory();if(!localImportAvailable){notify('请在管理端本机打开页面以选择源代码文件夹，当前浏览器可上传已有项目包。');return;}$('import-wizard').hidden=false;$('import-wizard').scrollIntoView({behavior:'smooth'});};
$('import-close').onclick=()=>{$('import-wizard').hidden=true;};
$('import-source-form').onsubmit=event=>{event.preventDefault();void startImport();};
$('import-entry').onchange=()=>void startImport($('import-entry').value);
$('import-browse').onclick=async()=>{importDraft=null;$('import-review-form').hidden=true;importBusy(true);setImportStep('source');try{const job=await waitImportJob(await api('/api/local/imports/browse',{}));if(job.status==='selected')$('import-source').value=job.source;}catch(error){importMessage('选择文件夹失败：'+error.message);}finally{importBusy(false);}};
$('import-history-refresh').onclick=()=>void loadImportHistory();
$('import-name').oninput=()=>{importDraft.project.name=$('import-name').value;importDraft.harness.name=$('import-name').value;draftChanged();};
$('import-project-id').oninput=()=>{importDraft.project.project_id=$('import-project-id').value;draftChanged();};
$('import-cwd').oninput=()=>{importDraft.harness.cwd=$('import-cwd').value;draftChanged();};
$('import-experiment').onchange=()=>{importExperimentIndex=Number($('import-experiment').value);renderImportExperiment();};
$('import-experiment-name').oninput=()=>{importDraft.experiments[importExperimentIndex].name=$('import-experiment-name').value;$('import-experiment').options[importExperimentIndex].textContent=$('import-experiment-name').value;draftChanged();};
$('import-experiment-id').oninput=()=>{importDraft.experiments[importExperimentIndex].id=$('import-experiment-id').value;draftChanged();};
$('import-add-experiment').onclick=()=>{const preset=copy(importDraft.experiments[importExperimentIndex]);let count=2;const stem=(preset.id||'experiment').slice(0,50);while(importDraft.experiments.some(item=>item.id===stem+'-'+count))count++;preset.id=stem+'-'+count;preset.name=(preset.name||stem)+' · 副本';importDraft.experiments.push(preset);importExperimentIndex=importDraft.experiments.length-1;renderImportExperiment();draftChanged();};
$('import-delete-experiment').onclick=()=>{if(importDraft.experiments.length<=1)return;importDraft.experiments.splice(importExperimentIndex,1);importExperimentIndex=0;renderImportExperiment();draftChanged();};
for(const [id,key] of [['import-modules','imports'],['import-requirements','requirements']])$(id).oninput=()=>{importDraft.project.runtime=importDraft.project.runtime||{};importDraft.project.runtime[key]=$(id).value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);draftChanged();};
for(const id of ['import-project-json','import-harness-json','import-experiments-json'])$(id).oninput=()=>{importJsonDirty=true;$('import-reviewed').checked=false;};
$('import-apply-json').onclick=()=>{
  try{const project=JSON.parse($('import-project-json').value),harness=JSON.parse($('import-harness-json').value),experiments=JSON.parse($('import-experiments-json').value);if(!project||typeof project!=='object'||Array.isArray(project)||!harness||typeof harness!=='object'||Array.isArray(harness)||!Array.isArray(experiments)||!experiments.length||experiments.some(item=>!item||!item.params||typeof item.params!=='object'||Array.isArray(item.params)))throw new Error('项目和 harness 需要对象，实验配置需要非空数组，每项包含 params 对象。');renderImportDraft({...importDraft,project,harness,experiments});inlineFeedback('import-json-feedback','JSON 修改已应用到表单。保存草稿时会完整验证配置。');}
  catch(error){inlineFeedback('import-json-feedback','JSON 修改未应用：'+error.message,true);}
};
async function saveImport(publish){
  if(!importDraft||!importJob)return;
  if($('import-source').value.trim()!==importDraft.project.source){importMessage('根目录已改变。请先点击“读取入口和参数”，生成这个目录的配置，再保存。');return;}
  for(const input of $('import-review-form').querySelectorAll('input,select,textarea')){if(input.id!=='import-reviewed'&&!input.checkValidity()){input.reportValidity();return;}}
  if(importJsonDirty){importMessage('请先点击“应用 JSON 修改”，确认高级配置与表单一致。');return;}
  if(publish&&!$('import-reviewed').checked){importMessage('请核对配置并勾选确认，再加入项目库。');return;}
  importBusy(true);
  try{
    let job=await waitImportJob(await api('/api/local/imports/save',{id:importJob.id,project:importDraft.project,harness:importDraft.harness,experiments:importDraft.experiments}));
    if(job.status!=='ready')throw new Error(job.error||'配置尚未保存成功，请检查上方信息。');
    if(job.draft)renderImportDraft(job.draft);
    if(publish){job=await waitImportJob(await api('/api/local/imports/publish',{id:job.id,reviewed:true}));if(job.status==='published'){$('import-review-form').hidden=true;setImportStep('publish');$('project-library').scrollIntoView({behavior:'smooth'});}}
    else importMessage('草稿已保存，文件列表已重新检查。可稍后从“导入草稿与进度”继续。');
  }catch(error){importMessage('保存或发布失败：'+error.message);}finally{importBusy(false);}
}
$('import-save').onclick=()=>void saveImport(false);
$('import-review-form').onsubmit=event=>{event.preventDefault();void saveImport(true);};
function inlineFeedback(id,message,error=false){const target=$(id);target.textContent=message;target.hidden=!message;target.classList.toggle('error',error);}
function lines(value){return value.split(/\r?\n/).map(item=>item.trim()).filter(Boolean);}
function valueText(value){return typeof value==='string'?value:JSON.stringify(value);}
function parseValue(text){try{return JSON.parse(text);}catch{return text;}}
function editField(labelText,value,onChange,options={}){
  const label=node('label',labelText),input=node(options.rows?'textarea':options.choices?'select':'input');
  if(options.choices)for(const [value,text] of options.choices){const option=node('option',text);option.value=value;input.append(option);}
  if(options.rows)input.rows=options.rows;else if(!options.choices)input.type=options.type||'text';
  input.value=value??'';input.setAttribute('aria-label',labelText);if(options.placeholder)input.placeholder=options.placeholder;
  const update=report=>{try{onChange(input.value,input);input.setCustomValidity('');}catch(error){input.setCustomValidity(error.message);if(report)input.reportValidity();}};
  input.onchange=()=>update(true);
  if(!options.choices)input.oninput=()=>update(false);
  label.append(input);return label;
}
function smallButton(text,action,danger=false){const button=node('button',text,'subtle'+(danger?' danger-text':''));button.type='button';button.onclick=action;return button;}
async function deleteProject(project,button){
  if(!confirm(`删除“${project.name}”？\n\n项目将从管理界面移除，并清理本机及已部署算力机上的导入副本、该项目容器和缓存，释放磁盘空间。\n导入前的原始文件、实验历史与结果保留。离线机器会在重新连接后清理。正在运行的关联实验需要先停止。`))return;
  button.disabled=true;
  try{const result=await api('/api/projects/delete',{digest:project.digest});notify(result.cleanup_pending?'项目已移除。部署副本正在清理，可在下方“部署清理进度”查看。':'项目已移除，部署副本已清理。');$('project-run-form').hidden=true;await refresh();$('project-cleanups').open=true;}
  catch(error){notify('删除失败：'+error.message);}finally{button.disabled=false;}
}
function renderProjectCleanups(){
  const items=state.project_deletions||[];$('project-cleanups').hidden=!items.length;$('project-cleanup-list').replaceChildren();
  const labels={delete_pending:'等待节点连接并清理',deleting:'正在清理部署副本',deleted:'已清理',delete_failed:'清理失败'};
  for(const project of items){const row=node('div',undefined,'cleanup-row');row.append(node('strong',project.name));
    for(const deployment of project.deployments||[])row.append(node('p',`${deployment.node_id} · ${labels[deployment.status]||deployment.status}${deployment.detail?' · '+deployment.detail:''}`,'muted'));
    const cleanupError=project.controller_cleanup_error||project.cleanup_error;
    if(cleanupError)row.append(node('p','管理端清理失败：'+cleanupError,'inline-feedback error'));
    else if(!(project.deployments||[]).length)row.append(node('p','管理端项目包已清理，无节点部署。','muted'));
    if(cleanupError||project.deployments?.some(item=>item.status==='delete_failed'))row.append(smallButton('重试清理',async()=>{try{await api('/api/projects/delete',{digest:project.digest,retry:true});await refresh();}catch(error){notify(error.message);}}));
    $('project-cleanup-list').append(row);
  }
}
function renderScheduling(id,scheduling={mode:'auto'},priority=0){
  const container=$(id);container.replaceChildren(node('h3','算力分配'));
  const controls=node('div',undefined,'form-grid'),modeLabel=editField('分配方式',scheduling.mode||'auto',()=>updateMode(),{choices:[['auto','自动 · 根据环境、资源与负载分配'],['assisted','半自动 · 限定候选与节点偏好'],['manual','手动 · 指定一台算力机']]}),mode=modeLabel.querySelector('select');mode.dataset.control='mode';
  const priorityLabel=editField('队列优先级（数字越大越优先）',priority,()=>{},{type:'number'}),priorityInput=priorityLabel.querySelector('input');priorityInput.min=-100;priorityInput.max=100;priorityInput.step=1;priorityInput.dataset.control='priority';controls.append(modeLabel,priorityLabel);container.append(controls);
  const manualLabel=editField('指定算力机',scheduling.node_ids?.[0]||'',()=>{},{choices:[['','请选择节点'],...(state.nodes||[]).map(worker=>[worker.id,worker.id+(timingNow()-worker.last_seen<=45?' · 在线':' · 离线')])]}),manual=manualLabel.querySelector('select');manual.dataset.control='manual';
  const choices=node('div',undefined,'node-choices');choices.append(node('p','候选算力机（不勾选表示所有兼容节点）','muted'));
  for(const worker of state.nodes||[]){const label=node('label',undefined,'checkbox-label'),check=node('input');check.type='checkbox';check.value=worker.id;check.checked=(scheduling.node_ids||[]).includes(worker.id);check.dataset.control='candidate';label.append(check,node('span',worker.id+' · '+((worker.snapshot?.gpus||[]).map(g=>g.name).join(' / ')||'CPU')));choices.append(label);}
  const preferredLabel=editField('优先节点顺序（每行一个节点名称）',(scheduling.preferred_node_ids||[]).join('\n'),()=>{},{rows:2}),preferred=preferredLabel.querySelector('textarea');preferred.dataset.control='preferred';
  const gpuDetails=node('details',undefined,'scheduling-gpus');gpuDetails.append(node('summary','可选：限定 GPU'));
  for(const worker of state.nodes||[])for(const gpu of worker.snapshot?.gpus||[]){const label=node('label',undefined,'checkbox-label'),check=node('input');check.type='checkbox';check.value=gpu.uuid;check.checked=(scheduling.gpu_uuids||[]).includes(gpu.uuid);check.dataset.control='gpu';label.append(check,node('span',`${worker.id} · ${gpu.name} · ${gpu.uuid}`));gpuDetails.append(label);}
  if(gpuDetails.children.length===1)gpuDetails.append(node('p','节点暂未报告可用 GPU。','muted'));
  container.append(manualLabel,choices,preferredLabel,gpuDetails,node('p','调度始终检查项目环境、数据、空闲显存和节点策略；暂时没有合适资源时自动排队。优先级不会抢占已运行实验。','muted'));
  function updateMode(){manualLabel.hidden=mode.value!=='manual';manual.disabled=manualLabel.hidden;choices.hidden=mode.value!=='assisted';preferredLabel.hidden=mode.value!=='assisted';}
  updateMode();
}
function readScheduling(id){
  const container=$(id),get=name=>container.querySelector('[data-control="'+name+'"]'),mode=get('mode').value,priority=Number(get('priority').value);
  const scheduling={mode,node_ids:mode==='manual'?[get('manual').value]:mode==='assisted'?[...container.querySelectorAll('[data-control="candidate"]:checked')].map(input=>input.value):[],preferred_node_ids:mode==='assisted'?lines(get('preferred').value):[],gpu_uuids:[...container.querySelectorAll('[data-control="gpu"]:checked')].map(input=>input.value)};
  if(mode==='manual'&&!scheduling.node_ids[0])throw new Error('手动分配需要选择一台算力机。');
  if(!Number.isInteger(priority)||priority<-100||priority>100)throw new Error('优先级应为 -100 到 100 的整数。');
  return {scheduling,priority};
}
function applyScheduling(spec,options){spec.scheduling=options.scheduling;spec.priority=options.priority;}

function renderImportEditors(){
  $('import-command').value=(importDraft.harness.command||[]).join('\n');renderParameterDefinitions();renderImportAssets();renderMetricRules();
  aiSuggestion=null;$('import-ai-preview').hidden=true;
}
$('import-command').oninput=()=>{importDraft.harness.command=lines($('import-command').value);$('import-command-preview').textContent=importDraft.harness.command.join(' ');draftChanged();};
function renderImportAssets(){
  const assets=importDraft.project.assets||{};$('import-assets').replaceChildren();
  for(const [alias,asset] of Object.entries(assets)){const box=node('div',undefined,'asset-group'),heading=node('div',undefined,'section-title');heading.append(node('h4','数据资源 · '+alias),smallButton('移除目录',()=>{delete assets[alias];renderImportAssets();draftChanged();},true));box.append(heading);
    const fields=node('div',undefined,'form-grid');fields.append(editField('本机数据目录',asset.path,value=>{asset.path=value;draftChanged();}),editField('包含文件（每行一个文件名或通配符）',(asset.include||['**/*']).join('\n'),value=>{asset.include=lines(value);draftChanged();},{rows:2}));
    box.append(fields,node('p',`参数中可填写 {assets.${alias}}，运行时替换为部署后的数据路径。`,'muted'));$('import-assets').append(box);
  }
  if(!Object.keys(assets).length)$('import-assets').append(node('p','点击“添加数据目录”，填写需要一同分发的数据路径。项目内部已包含的数据可以直接使用。','muted'));
}
$('import-add-asset').onclick=()=>{const assets=importDraft.project.assets||={};let name='dataset',index=2;while(name in assets)name='dataset'+index++;assets[name]={path:'',include:['**/*']};renderImportAssets();draftChanged();};
function renderParameterDefinitions(){
  const harness=importDraft.harness;harness.parameters||={};harness.fixed_params||={};harness.bindings||=[];
  $('import-parameter-definitions').replaceChildren();
  for(const originalKey of new Set([...Object.keys(harness.parameters),...Object.keys(harness.fixed_params)])){
    let key=originalKey;const fixed=Object.hasOwn(harness.fixed_params,key),definition=harness.parameters[key]||{},binding=harness.bindings.find(item=>item.param===key),box=node('div',undefined,'editor-row'),fields=node('div',undefined,'form-grid parameter-definition-grid');
    fields.append(editField('参数名',key,value=>{if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(value))throw new Error('参数名使用字母、数字与下划线');if(value===key)return;if(value in harness.parameters||value in harness.fixed_params)throw new Error('参数名已经存在');const map=fixed?harness.fixed_params:harness.parameters;map[value]=map[key];delete map[key];for(const item of harness.bindings)if(item.param===key)item.param=value;for(const experiment of importDraft.experiments)if(key in experiment.params){experiment.params[value]=experiment.params[key];delete experiment.params[key];}key=value;renderImportExperiment();renderFixedParameters();draftChanged();}),
      editField('命令行选项',binding?.flag||'',value=>{if(value&&!value.startsWith('-'))throw new Error('命令行选项应以 - 或 -- 开头');const existing=harness.bindings.find(item=>item.param===key);harness.bindings=harness.bindings.filter(item=>item.param!==key);if(value)harness.bindings.push({...existing,param:key,flag:value,mode:existing?.mode||'value'});draftChanged();}),
      editField('参数类型',definition.type||(typeof harness.fixed_params[key]==='number'?'number':'string'),value=>{if(!fixed)harness.parameters[key].type=value;const current=fixed?harness.fixed_params[key]:importDraft.experiments[importExperimentIndex].params[key];let converted=value==='boolean'?(current===true||current==='true'):['integer','number'].includes(value)?Number(current):String(current??'');if(typeof converted==='number'&&!Number.isFinite(converted))converted=0;if(fixed)harness.fixed_params[key]=converted;else{harness.parameters[key].default=converted;for(const experiment of importDraft.experiments)experiment.params[key]=converted;}renderImportExperiment();renderFixedParameters();draftChanged();},{choices:[['string','文本'],['integer','整数'],['number','小数'],['boolean','布尔值']]}),
      editField('传参方式',binding?.mode||'value',value=>{const current=harness.bindings.find(item=>item.param===key);if(current)current.mode=value;draftChanged();},{choices:[['value','选项 + 值'],['store_true','为真时传入开关'],['store_false','为假时传入开关']]}));
    const actions=node('div',undefined,'actions compact');actions.append(node('span',fixed?'固定参数':'每次实验可修改','badge'),smallButton(fixed?'改为实验参数':'设为固定参数',()=>{
      const value=fixed?harness.fixed_params[key]:importDraft.experiments[importExperimentIndex].params[key]??definition.default??'';
      if(fixed){if(typeof value==='string'&&/\{(?:assets|env|output)/.test(value)){inlineFeedback('import-json-feedback','此固定参数包含运行时路径或 GPU 占位符。请保留固定参数；可在路径中使用 {params.dataset}，再添加 dataset 实验参数来切换数据集。',true);$('import-json-feedback').closest('details').open=true;return;}harness.parameters[key]={type:typeof value==='number'?'number':typeof value==='boolean'?'boolean':'string',default:value};delete harness.fixed_params[key];for(const experiment of importDraft.experiments)experiment.params[key]=value;}
      else{harness.fixed_params[key]=value;delete harness.parameters[key];for(const experiment of importDraft.experiments)delete experiment.params[key];}
      renderParameterDefinitions();renderImportExperiment();renderFixedParameters();draftChanged();
    }),smallButton('删除参数',()=>{delete harness.parameters[key];delete harness.fixed_params[key];harness.bindings=harness.bindings.filter(item=>item.param!==key);for(const experiment of importDraft.experiments)delete experiment.params[key];renderParameterDefinitions();renderImportExperiment();renderFixedParameters();draftChanged();},true));
    box.append(fields,actions);$('import-parameter-definitions').append(box);
  }
}
function renderFixedParameters(){const harness=importDraft.harness;objectFields($('import-fixed-fields'),harness.fixed_params,(key,value)=>{harness.fixed_params[key]=value;draftChanged();});}
$('import-add-parameter').onclick=()=>{const harness=importDraft.harness;let key='new_parameter',count=2;while(key in harness.parameters||key in harness.fixed_params)key='new_parameter_'+count++;harness.parameters[key]={type:'string',default:''};harness.bindings.push({param:key,flag:'--'+key,mode:'value'});for(const experiment of importDraft.experiments)experiment.params[key]='';renderParameterDefinitions();renderImportExperiment();draftChanged();};
function metricTemplate(template){
  const expression=/\{([A-Za-z_][A-Za-z0-9_]*)\}/g,groups=[],chunks=[];let start=0,match;
  const escape=text=>text.replace(/[.*+?^${}()|[\]\\]/g,'\\$&').replace(/\s+/g,'\\s*');
  while((match=expression.exec(template))){const key=match[1];if(groups.includes(key))throw new Error('模板中的指标名不能重复：'+key);groups.push(key);chunks.push(escape(template.slice(start,match.index)),key==='step'?'(?P<step>\\d+)':`(?P<${key}>[-+]?(?:\\d*\\.\\d+|\\d+\\.?\\d*)(?:[eE][-+]?\\d+)?)`);start=match.index+match[0].length;}
  chunks.push(escape(template.slice(start)));if(!groups.includes('step')||groups.length<2)throw new Error('模板至少需要 {step} 和一个指标，例如 {loss}。');
  return {pattern:chunks.join(''),step:'step',values:Object.fromEntries(groups.filter(key=>key!=='step').map(key=>[key,key]))};
}
function renderMetricRules(){
  const rules=importDraft.harness.metrics||=[];$('import-metric-rules').replaceChildren();
  rules.forEach((rule,index)=>{const box=node('div',undefined,'editor-row'),header=node('div',undefined,'section-title');header.append(node('strong','规则 '+(index+1)),smallButton('删除规则',()=>{rules.splice(index,1);renderMetricRules();draftChanged();},true));box.append(header);
    const sourceFields=node('div',undefined,'form-grid');sourceFields.append(editField('日志来源',rule.stream||'stdout',value=>{rule.stream=value;if(value==='file')rule.path||='*.log';renderMetricRules();draftChanged();},{choices:[['stdout','标准输出'],['stderr','错误输出'],['file','结果目录中的日志文件']]}));
    if(rule.stream==='file')sourceFields.append(editField('日志相对路径 / 通配符',rule.path||'*.log',value=>{rule.path=value;draftChanged();}));box.append(sourceFields);
    if(rule.template!==undefined)box.append(editField('日志模板',rule.template,value=>{const compiled=metricTemplate(value);Object.assign(rule,compiled,{template:value});draftChanged();},{rows:2}));
    const advanced=node('details',undefined,'metric-regex');advanced.open=rule.template===undefined;advanced.append(node('summary','正则表达式与指标映射'));
    advanced.append(editField('Python 正则表达式',rule.pattern,value=>{rule.pattern=value;delete rule.template;draftChanged();},{rows:3}),editField('轮次捕获组',rule.step||'step',value=>{rule.step=value;draftChanged();}),editField('指标名 = 捕获组（每行一个）',Object.entries(rule.values||{}).map(([key,value])=>key+' = '+value).join('\n'),value=>{const entries=lines(value).map(line=>{const split=line.indexOf('=');if(split<1)throw new Error('格式应为 loss = loss');return [line.slice(0,split).trim(),line.slice(split+1).trim()];});rule.values=Object.fromEntries(entries);draftChanged();},{rows:2}));box.append(advanced);$('import-metric-rules').append(box);
  });
  if(!rules.length)$('import-metric-rules').append(node('p','还没有指标规则。添加与算法输出相符的模板后，即可显示监控曲线。','muted'));
}
function addMetricRule(template){const rule={stream:'stdout',template,...metricTemplate(template)};(importDraft.harness.metrics||=[]).push(rule);renderMetricRules();draftChanged();}
$('import-metric-simple').onclick=()=>addMetricRule('epoch: {step}, loss: {loss}');
$('import-metric-sasrec').onclick=()=>addMetricRule('epoch:{step}, time: {seconds}(s), valid (NDCG: {valid_ndcg}, HR: {valid_hr}), test (NDCG: {test_ndcg}, HR: {test_hr})');
$('import-metric-add').onclick=()=>addMetricRule('step={step} loss={loss}');
$('import-metric-test').onclick=async()=>{const button=$('import-metric-test');button.disabled=true;try{const data=await api('/api/local/imports/test-metrics',{metrics:importDraft.harness.metrics||[],sample_log:$('import-sample-log').value});inlineFeedback('import-metric-feedback',(data.matches||[]).length?'匹配成功：'+data.matches.map(item=>`规则 ${item.rule+1} · step ${item.step} · ${Object.entries(item.values).map(([k,v])=>k+' = '+v).join('，')}`).join('\n'):'没有匹配到指标，请调整模板，使标点和文本与日志一致。');if(data.warnings?.length)$('import-metric-feedback').textContent+='\n'+data.warnings.join('\n');}catch(error){inlineFeedback('import-metric-feedback',error.message,true);}finally{button.disabled=false;}};

let aiSuggestion=null;
async function loadAISettings(){try{const settings=await api('/api/ai/settings');$('ai-enabled').checked=!!settings.enabled;$('ai-base-url').value=settings.base_url;$('ai-model').value=settings.model;$('ai-api-key').value='';$('ai-clear-key').checked=false;$('ai-key-status').textContent=settings.has_api_key?'已有密钥，留空保留。密钥只保存在管理端，不会回显。':'尚未配置密钥。';}catch(error){inlineFeedback('ai-settings-feedback',error.message,true);}}
async function saveAISettings(test=false){if(!$('ai-settings-form').reportValidity())return;$('ai-save').disabled=true;$('ai-test').disabled=true;try{await api('/api/ai/settings',{enabled:$('ai-enabled').checked,base_url:$('ai-base-url').value.trim(),model:$('ai-model').value.trim(),api_key:$('ai-api-key').value.trim(),clear_api_key:$('ai-clear-key').checked});await loadAISettings();inlineFeedback('ai-settings-feedback',test?'正在测试连接…':'设置已保存。');if(test){const result=await api('/api/ai/test',{});inlineFeedback('ai-settings-feedback',result.message||'AI 服务连接成功。');}}catch(error){inlineFeedback('ai-settings-feedback',error.message,true);}finally{$('ai-save').disabled=false;$('ai-test').disabled=false;}}
$('ai-settings-form').onsubmit=event=>{event.preventDefault();void saveAISettings();};$('ai-test').onclick=()=>void saveAISettings(true);
$('import-ai-assist').onclick=async()=>{
  if(importJsonDirty){inlineFeedback('import-ai-feedback','请先应用 JSON 修改，让模型使用当前配置。',true);return;}
  importBusy(true);aiSuggestion=null;$('import-ai-preview').hidden=true;inlineFeedback('import-ai-feedback','正在分析入口与配置，通常需要数十秒…');
  try{const result=await api('/api/local/imports/assist',{id:importJob.id,project:importDraft.project,harness:importDraft.harness,experiments:importDraft.experiments,instructions:$('import-ai-instructions').value,sample_log:$('import-sample-log').value,include_source:$('import-ai-source').checked});aiSuggestion=result;$('import-ai-summary').textContent=result.summary||'建议已生成，请检查修改内容。';$('import-ai-warnings').replaceChildren(...(result.warnings||[]).map(text=>node('li',text)));$('import-ai-changes').replaceChildren(...(result.changes||[]).map(change=>{const item=node('details'),summary=node('summary',change.path);item.append(summary,node('pre','修改前\n'+JSON.stringify(change.before,null,2)+'\n\n修改后\n'+JSON.stringify(change.after,null,2)));return item;}));$('import-ai-preview').hidden=false;inlineFeedback('import-ai-feedback','建议已生成，尚未应用。');}
  catch(error){inlineFeedback('import-ai-feedback','分析失败：'+error.message,true);}finally{importBusy(false);}
};
$('import-ai-apply').onclick=()=>{if(!aiSuggestion)return;const suggestion=aiSuggestion;renderImportDraft({...importDraft,...suggestion.draft});inlineFeedback('import-ai-feedback','建议已应用到草稿。请核对参数、数据和指标，保存后再发布。');};

let matrixDraft=null,matrixTemplates=[],matrixAxes=[],matrixResultId=null,matrixLoading=false,matrixEditorBusy=false;
const matrixStartRequests=new Map(),matrixPendingLaunches=new Map(),matrixStartsInFlight=new Set();
function availableTemplates(){
  const result=[],seen=new Set();for(const worker of state.nodes||[])for(const template of worker.snapshot?.task_templates||[]){
    if(!template.project_id)continue;
    const project=(state.projects||[]).find(item=>item.project_id===template.project_id&&(!template.project_bundle_id||item.bundle_id===template.project_bundle_id));if(!project)continue;
    const key=JSON.stringify([template.project_id,template.project_bundle_id,template.name,template.params]);if(seen.has(key))continue;seen.add(key);result.push({label:project.name+' · '+template.name,spec:copy(template)});
  }return result;
}
async function loadMatrices(){
  if(matrixLoading)return;matrixLoading=true;
  try{const result=await api('/api/matrices');$('matrix-list').replaceChildren();
    for(const matrix of result.matrices||[]){const row=node('div',undefined,'matrix-card'),heading=node('div',undefined,'section-title'),label=node('div');label.append(node('h3',matrix.name),node('p',`${matrix.count} 个组合 · 已提交 ${matrix.job_count} 次实验 · 修订 ${matrix.revision}`,'muted'));heading.append(label);row.append(heading);
      if(matrix.description)row.append(node('p',matrix.description,'muted'));const badges=node('div',undefined,'actions');for(const [status,count] of Object.entries(matrix.states||{}))badges.append(node('span',`${names[status]||status} ${count}`,'badge '+status));row.append(badges);
      const actions=node('div',undefined,'actions');actions.append(smallButton('编辑',async()=>{try{openMatrix(await api('/api/matrices/item?id='+encodeURIComponent(matrix.id)));}catch(error){notify(error.message);}}),smallButton('一键启动',event=>void startMatrix(matrix,event.currentTarget)),smallButton('查看结果',()=>void showMatrixResults(matrix.id)),smallButton('导出 Markdown',()=>download('/api/matrices/report.md?id='+encodeURIComponent(matrix.id),matrix.name+'.md')),smallButton('删除矩阵',async()=>{if(!confirm(`删除矩阵“${matrix.name}”？历史实验与结果会保留。`))return;try{await api('/api/matrices/delete',{id:matrix.id});if(matrixDraft?.id===matrix.id){matrixDraft=null;$('matrix-form').hidden=true;}if(matrixResultId===matrix.id)$('matrix-result').hidden=true;await loadMatrices();}catch(error){notify('删除失败：'+error.message);}},true));row.append(actions);$('matrix-list').append(row);
    }
    if(!$('matrix-list').children.length)$('matrix-list').append(node('p','尚无实验矩阵。导入并部署算法后，新建一个矩阵，添加数据集和参数即可批量运行。','empty-state'));
  }catch(error){notify('读取矩阵失败：'+error.message);}finally{matrixLoading=false;}
}
function matrixPayload(){
  if(!matrixDraft?.spec)throw new Error('请选择已部署项目的实验配置，或导入一份矩阵配置文件。');
  const name=$('matrix-name').value.trim();if(!name)throw new Error('请填写矩阵名称。');
  const grid={};for(const axis of matrixAxes){if(!axis.key.trim())throw new Error('请填写每个参数维度的名称。');if(axis.key in grid)throw new Error('参数维度重复：'+axis.key);const values=lines(axis.text);if(!values.length)throw new Error('参数 '+axis.key+' 至少需要一个候选值。');grid[axis.key]=values.map(value=>axis.type==='string'?value:parseValue(value));}
  const payload={name,description:$('matrix-description').value.trim(),spec:copy(matrixDraft.spec),datasets:copy(matrixDraft.datasets||[]),grid,...readScheduling('matrix-scheduling')};
  if(matrixDraft.id){payload.id=matrixDraft.id;payload.revision=matrixDraft.revision;}
  return payload;
}
function openMatrix(definition){
  matrixTemplates=availableTemplates();matrixDraft=definition?copy(definition):{name:'',description:'',spec:matrixTemplates[0]?copy(matrixTemplates[0].spec):null,datasets:[],grid:{}};
  matrixDraft.datasets=(matrixDraft.datasets||[]).map(dataset=>({...dataset,params:dataset.params||{}}));matrixDraft.grid||={};
  if(!definition&&matrixDraft.spec)matrixDraft.spec.scheduling={mode:'auto'};
  $('matrix-editor-title').textContent=matrixDraft.id?'编辑实验矩阵':'新建实验矩阵';$('matrix-name').value=matrixDraft.name||'';$('matrix-description').value=matrixDraft.description||'';$('matrix-name').maxLength=160;
  const select=$('matrix-template');select.replaceChildren();if(matrixDraft.spec){const current=node('option','当前配置 · '+matrixDraft.spec.name);current.value='current';select.append(current);}
  for(const [index,item] of matrixTemplates.entries()){const option=node('option',item.label);option.value=String(index);select.append(option);}
  if(!select.children.length){const option=node('option','先导入并部署算法项目，等待节点报告实验配置');option.value='';select.append(option);}
  matrixAxes=Object.entries(matrixDraft.grid||{}).map(([key,values])=>({key,text:values.map(valueText).join('\n'),type:values.every(value=>typeof value==='string')?'string':'auto'}));
  renderMatrixParameters();renderMatrixDatasets();renderMatrixAxes();renderScheduling('matrix-scheduling',matrixDraft.scheduling||matrixDraft.spec?.scheduling||{mode:'auto'},matrixDraft.priority??matrixDraft.spec?.priority??0);
  $('matrix-preview-result').hidden=true;inlineFeedback('matrix-feedback','');$('matrix-form').hidden=false;showView('matrices');$('matrix-form').scrollIntoView({behavior:'smooth'});
}
function renderMatrixParameters(){if(matrixDraft.spec)objectFields($('matrix-base-params'),matrixDraft.spec.params||{},(key,value)=>{matrixDraft.spec.params[key]=value;});else $('matrix-base-params').replaceChildren(node('p','尚未选择基础配置。','muted'));}
$('matrix-template').onchange=()=>{if($('matrix-template').value==='current')return;const item=matrixTemplates[Number($('matrix-template').value)];if(!item)return;matrixDraft.spec=copy(item.spec);renderMatrixParameters();};
function renderDatasetParameters(container,dataset){
  container.replaceChildren();for(const [originalKey,value] of Object.entries(dataset.params||{})){let key=originalKey;const row=node('div',undefined,'dataset-parameter-row');row.append(editField('覆盖参数',key,next=>{if(!next.trim())throw new Error('参数名不能为空');if(next===key)return;if(next in dataset.params)throw new Error('参数已经存在');dataset.params[next]=dataset.params[key];delete dataset.params[key];key=next;}),editField('参数值',valueText(value),(next,input)=>{const base=matrixDraft.spec?.params?.[key];dataset.params[key]=typeof base==='string'?next:parseValue(next);}),smallButton('移除',()=>{delete dataset.params[key];renderDatasetParameters(container,dataset);},true));container.append(row);}
}
function renderMatrixDatasets(){
  $('matrix-datasets').replaceChildren();(matrixDraft.datasets||[]).forEach((dataset,index)=>{const box=node('div',undefined,'editor-row'),heading=node('div',undefined,'section-title');heading.append(node('strong','数据集 '+(index+1)),smallButton('删除数据集',()=>{matrixDraft.datasets.splice(index,1);renderMatrixDatasets();},true));box.append(heading,editField('数据集名称',dataset.name,value=>{dataset.name=value;}));const fields=node('div');renderDatasetParameters(fields,dataset);box.append(fields,smallButton('添加覆盖参数',()=>{let key='dataset',count=2;while(key in dataset.params)key='parameter_'+count++;dataset.params[key]='';renderDatasetParameters(fields,dataset);}));
    const advanced=node('details',undefined,'dataset-assets');advanced.append(node('summary','可选：数据资源标识'),editField('覆盖任务的数据资源列表（每行一个标识；留空保留基础配置）',(dataset.assets||[]).join('\n'),value=>{const values=lines(value);if(values.length)dataset.assets=values;else delete dataset.assets;},{rows:2}));box.append(advanced);$('matrix-datasets').append(box);
  });if(!matrixDraft.datasets?.length)$('matrix-datasets').append(node('p','使用基础配置的数据集。添加多份数据集后，将分别运行全部参数组合。','muted'));
}
function renderMatrixAxes(){
  $('matrix-axes').replaceChildren();matrixAxes.forEach((axis,index)=>{const row=node('div',undefined,'editor-row'),fields=node('div',undefined,'form-grid');fields.append(editField('参数名（如 seed、lr）',axis.key,value=>{axis.key=value.trim();}),editField('候选值类型',axis.type||'auto',value=>{axis.type=value;},{choices:[['auto','自动识别数字 / 布尔 / 文本'],['string','全部按文本处理']]}),editField('候选值（每行一个）',axis.text,value=>{axis.text=value;},{rows:3}));row.append(fields,smallButton('删除维度',()=>{matrixAxes.splice(index,1);renderMatrixAxes();},true));$('matrix-axes').append(row);});
  if(!matrixAxes.length)$('matrix-axes').append(node('p','每个数据集运行一遍基础参数。添加维度可比较不同学习率、随机种子等。','muted'));
}
$('matrix-new').onclick=()=>openMatrix();$('matrix-close').onclick=()=>{$('matrix-form').hidden=true;};
$('matrix-add-dataset').onclick=()=>{const params=matrixDraft.spec?.params||{},key=Object.keys(params).find(name=>['dataset','dataset_name','data_name'].includes(name))||'dataset';matrixDraft.datasets.push({name:'数据集 '+(matrixDraft.datasets.length+1),params:{[key]:params[key]??''}});renderMatrixDatasets();};
$('matrix-add-axis').onclick=()=>{matrixAxes.push({key:'',text:'',type:'auto'});renderMatrixAxes();};
function matrixBusy(busy){matrixEditorBusy=busy;for(const button of $('matrix-form').querySelectorAll('button'))button.disabled=busy;}
async function saveMatrix(start=false){
  if(matrixEditorBusy||!$('matrix-form').reportValidity())return;matrixBusy(true);
  try{const payload=matrixPayload(),fingerprint=JSON.stringify({...payload,id:undefined,revision:undefined}),pending=matrixPendingLaunches.get(matrixDraft.id);let saved;
    if(start&&pending?.fingerprint===fingerprint){saved=pending.matrix;inlineFeedback('matrix-feedback','正在确认上一次启动结果，复用原批次标识…');}
    else{saved=await api('/api/matrices/save',payload);matrixDraft={...matrixDraft,id:saved.id,revision:saved.revision,count:saved.count};$('matrix-editor-title').textContent='编辑实验矩阵';inlineFeedback('matrix-feedback',`矩阵已保存 · ${saved.count} 个组合。`);if(start)matrixPendingLaunches.set(saved.id,{fingerprint,matrix:saved});await loadMatrices();}
    if(start)await startMatrix(saved);}
  catch(error){inlineFeedback('matrix-feedback','保存失败：'+error.message,true);}finally{matrixBusy(false);}
}
$('matrix-form').onsubmit=event=>{event.preventDefault();void saveMatrix();};$('matrix-save-start').onclick=()=>void saveMatrix(true);
$('matrix-preview').onclick=async()=>{if(!$('matrix-form').reportValidity())return;matrixBusy(true);try{const data=await api('/api/matrices/preview',matrixPayload()),container=$('matrix-preview-result');container.replaceChildren(node('h3',`预计启动 ${data.count} 次实验`));for(const warning of data.warnings||[])container.append(node('p',warning,'review-notes'));
  const table=node('table'),head=node('thead'),heading=node('tr');for(const text of ['实验 / 数据集','参数','预计算力'])heading.append(node('th',text));head.append(heading);table.append(head);const body=node('tbody');(data.specs||[]).forEach((spec,index)=>{const row=node('tr'),allocation=data.allocations?.[index];row.append(node('td',spec.name),node('td',Object.entries(spec.params||{}).map(([key,value])=>key+'='+valueText(value)).join(' · ')),node('td',allocation?.node_id||'等待资源'));row.title=allocation?.reason||'';body.append(row);});table.append(body);const wrap=node('div',undefined,'table-wrap');wrap.append(table);container.append(wrap,node('p','这是当前资源快照下的预估；实际启动时会重新检查节点与资源。','muted'));container.hidden=false;inlineFeedback('matrix-feedback','预览完成，确认组合后即可保存并启动。');}
  catch(error){inlineFeedback('matrix-feedback','预览失败：'+error.message,true);}finally{matrixBusy(false);}};
async function startMatrix(matrix,button){
  if(matrixStartsInFlight.has(matrix.id))return;matrixStartsInFlight.add(matrix.id);
  if(button)button.disabled=true;const key=matrix.id+':'+matrix.revision,request=matrixStartRequests.get(key)||requestId();matrixStartRequests.set(key,request);
  try{let result;
    try{result=await api('/api/matrices/start',{id:matrix.id,revision:matrix.revision,request_id:request});}
    catch(error){inlineFeedback('matrix-feedback','启动未确认：'+error.message,true);notify('矩阵启动结果未确认：'+error.message+' 重试将复用本次提交标识，避免重复创建。');return;}
    matrixStartRequests.delete(key);if(matrixPendingLaunches.get(matrix.id)?.matrix.revision===matrix.revision)matrixPendingLaunches.delete(matrix.id);
    notify(`矩阵“${matrix.name}”已启动，${result.ids.length} 次实验已进入队列。`);inlineFeedback('matrix-feedback',`已启动 ${result.ids.length} 次实验，可在矩阵结果中查看。`);
    try{await refresh();await loadMatrices();await showMatrixResults(matrix.id);}
    catch(error){const message=`矩阵已启动，${result.ids.length} 次实验已进入队列；刷新状态失败：${error.message}。请刷新结果查看进度。`;inlineFeedback('matrix-feedback',message,true);notify(message);}
  }finally{matrixStartsInFlight.delete(matrix.id);if(button)button.disabled=false;}
}
async function showMatrixResults(id,scroll=true){
  try{const matrix=await api('/api/matrices/item?id='+encodeURIComponent(id));matrixResultId=id;$('matrix-result-title').textContent=matrix.name+' · 结果';$('matrix-result-summary').textContent=`${matrix.runs?.length||0} 个批次 · ${matrix.job_count} 次实验 · `+Object.entries(matrix.states||{}).map(([status,count])=>`${names[status]||status} ${count}`).join(' · ');$('matrix-result-jobs').replaceChildren();for(const job of matrix.jobs||[]){const row=node('tr'),label=node('td');label.append(node('strong',job.spec.name),node('small',job.spec.dataset_name||''));const status=node('td');status.append(node('span',names[job.state]||job.state,'badge '+job.state));row.append(label,status,node('td',job.node_id||'等待分配'),node('td',Object.entries(job.metrics||{}).filter(([key])=>!['step','time','attempt'].includes(key)).map(([key,value])=>`${key}: ${value}`).join(' · ')||'—'));row.onclick=()=>{showView('experiments');void detail(job.id,true);};$('matrix-result-jobs').append(row);}$('matrix-result-export').onclick=()=>download('/api/matrices/report.md?id='+encodeURIComponent(id),matrix.name+'.md');$('matrix-result').hidden=false;if(scroll)$('matrix-result').scrollIntoView({behavior:'smooth'});}
  catch(error){notify('读取矩阵结果失败：'+error.message);}
}
$('matrix-result-refresh').onclick=()=>matrixResultId&&void showMatrixResults(matrixResultId,false);$('matrix-result-close').onclick=()=>{$('matrix-result').hidden=true;matrixResultId=null;};
function downloadContent(text,name,type){const url=URL.createObjectURL(new Blob([text],{type})),link=node('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('matrix-export-config').onclick=()=>{try{const payload=matrixPayload();delete payload.id;delete payload.revision;downloadContent(JSON.stringify(payload,null,2)+'\n',payload.name+'.matrix.json','application/json');inlineFeedback('matrix-feedback','矩阵配置已导出，可再次导入或分享。');}catch(error){inlineFeedback('matrix-feedback',error.message,true);}};
$('matrix-import').onclick=()=>$('matrix-file').click();$('matrix-file').onchange=async event=>{const file=event.target.files[0];if(!file)return;try{if(file.size>2*1024*1024)throw new Error('配置文件不能超过 2 MiB');const value=JSON.parse((await file.text()).replace(/^\uFEFF/,''));if(!value?.spec||!Array.isArray(value.datasets||[])||!value.grid||Array.isArray(value.grid))throw new Error('请选择包含 spec、datasets、grid 的矩阵配置。');delete value.id;delete value.revision;openMatrix(value);inlineFeedback('matrix-feedback','已导入配置，请预览资源与组合，再保存启动。');}catch(error){notify('导入矩阵失败：'+error.message);}finally{event.target.value='';}};
function markdownCell(value){return String(value??'—').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\|/g,'\\|').replace(/[\r\n]+/g,' ');}
$('export-markdown').onclick=()=>{const filter=$('filter').value.toLowerCase(),jobs=(state.jobs||[]).filter(job=>JSON.stringify([job.spec.name,job.spec.algorithm,job.spec.group]).toLowerCase().includes(filter)),keys=[...new Set(jobs.flatMap(job=>Object.keys(job.metrics||{})))].filter(key=>!['step','time','attempt'].includes(key));const headers=['实验','算法','状态','节点',...keys],rows=jobs.map(job=>[job.spec.name,job.spec.algorithm,names[job.state]||job.state,job.node_id||'等待分配',...keys.map(key=>job.metrics?.[key])]);const text=['# 实验结果',`\n导出时间：${new Date().toLocaleString('zh-CN')}；共 ${jobs.length} 次实验。指标为最近一次回传值。\n`,'| '+headers.map(markdownCell).join(' | ')+' |','| '+headers.map(()=>'---').join(' | ')+' |',...rows.map(row=>'| '+row.map(markdownCell).join(' | ')+' |'),'\n## 实验参数\n',...jobs.map(job=>'### '+markdownCell(job.spec.name)+'\n\n```json\n'+JSON.stringify(job.spec.params,null,2).replace(/```/g,'\\u0060\\u0060\\u0060')+'\n```\n')].join('\n');downloadContent(text,'实验结果.md','text/markdown;charset=utf-8');};
showView(location.hash.slice(1));refresh();setInterval(()=>{void refresh();if(token&&currentView==='matrices'){void loadMatrices();if(matrixResultId&&!$('matrix-result').hidden)void showMatrixResults(matrixResultId,false);}},5000);setInterval(updateTimers,1000);
