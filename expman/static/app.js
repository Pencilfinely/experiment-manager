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
const viewLabels={overview:['总览','实验、算力和算法项目，都在这里管理。'],experiments:['实验记录','查看进度、指标和结果，管理每一次运行。'],compute:['算力管理','连接 Windows 与 Ubuntu 算力机，查看资源和接单状态。'],projects:['算法项目','从原始目录导入，检查配置，再分发到算力机。'],settings:['设置与帮助','管理连接，了解实验台与算力端的运行方式。']};
const requestId = () => Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('');
function node(tag, text, cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=String(text); if(cls)e.className=cls;return e; }
function notify(message) { $('notice').textContent=message; $('notice').hidden=!message; }
async function api(path, body) { const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(!response.ok){let msg=await response.text();try{const data=JSON.parse(msg);msg=data.error||data.message||msg;}catch{}throw new Error(msg);}return response.json(); }
function showView(view){
  if(!viewLabels[view])view='overview';currentView=view;
  document.querySelectorAll('[data-page]').forEach(section=>section.hidden=section.dataset.page!==view);
  document.querySelectorAll('[data-view]').forEach(button=>{button.classList.toggle('active',button.dataset.view===view);if(button.dataset.view===view)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
  $('page-title').textContent=viewLabels[view][0];$('page-description').textContent=viewLabels[view][1];
  history.replaceState(null,'',location.pathname+location.search+'#'+view);
  if(view==='projects'&&token)void loadImportHistory();
}
document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>showView(button.dataset.view));
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
    const create=node('button','创建实验');create.onclick=()=>openProjectRun(project);box.append(deploy,create);$('projects').append(box);
  }
  if(!(state.projects||[]).length)$('projects').append(node('p','项目库还是空的。点击“从文件夹导入”，或上传已有的 ZIP 项目包。','muted'));
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
  $('project-preset').replaceChildren(...projectPresets.map((item,i)=>{const option=node('option',item.worker+' · '+item.template.name);option.value=String(i);return option;}));
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
    if(!spec.params||typeof spec.params!=='object'||Array.isArray(spec.params))throw new Error('参数必须是 JSON 对象。');
    const result=await api('/api/jobs',{request_id:requestId(),spec});notify(`已提交 ${result.ids.length} 个实验到 ${preset.worker}。可以在实验记录中查看日志和结果。`);showView('experiments');await refresh();
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
async function refresh(){if(!token)return;try{state=await api('/api/state');$('login').hidden=true;$('workspace').hidden=false;$('sidebar').hidden=false;document.body.classList.add('authenticated');$('connection').textContent='● 管理中心在线';render();if(localImportAvailable===null)void loadImportHistory();if(selected)await detail(selected,false);}catch(error){$('connection').textContent='○ 无法连接';notify('暂时无法获取管理中心状态，请检查程序是否运行、网络和令牌。已经准备好的节点任务不依赖此页面继续运行。 '+error.message.slice(0,150));}}
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
function renderJobs(){const filter=$('filter').value.toLowerCase();const jobs=(state.jobs||[]).filter(j=>JSON.stringify([j.spec.name,j.spec.algorithm,j.spec.group]).toLowerCase().includes(filter));$('jobs').replaceChildren();$('empty').hidden=jobs.length>0;for(const j of jobs){const row=node('tr'),title=node('td');title.append(node('strong',j.spec.name),node('small',`${j.spec.algorithm} / ${j.spec.group}`));const status=node('td');status.append(node('span',names[j.state]||j.state,'badge '+j.state));const met=j.metrics||{};row.append(title,status,node('td',j.node_id||'等待匹配'),node('td',Object.entries(met).filter(([k])=>!['step','time','attempt'].includes(k)).slice(0,2).map(([k,v])=>`${k}: ${typeof v==='number'?v.toPrecision(4):v}`).join(' · ')||'—'));row.onclick=()=>detail(j.id,true);$('jobs').append(row);}}
$('filter').oninput=renderJobs;
$('submit-form').onsubmit=async event=>{event.preventDefault();$('submit-button').disabled=true;try{const spec=JSON.parse($('spec').value),grid=JSON.parse($('grid').value||'{}'),request_id=globalThis.crypto?.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random();const result=await api('/api/jobs',{spec,grid,request_id});notify(`已提交 ${result.ids.length} 个实验。没有符合环境、数据和节点策略的机器时，实验会保留在队列。`);await refresh();}catch(error){notify('提交失败：'+error.message);}finally{$('submit-button').disabled=false;}};
async function download(path,name){try{const response=await fetch(path,{headers:{Authorization:'Bearer '+token}});if(!response.ok)throw new Error(await response.text());const objectURL=URL.createObjectURL(await response.blob()),a=node('a');a.href=objectURL;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(objectURL),1000);}catch(e){notify(e.message);}}
$('export').onclick=()=>download('/api/results.csv','实验结果.csv');
async function detail(id,scroll){selected=id;try{const data=await api('/api/job?id='+encodeURIComponent(id)),j=data.job||data;$('detail').hidden=false;$('detail-name').textContent=j.spec.name;$('detail-state').textContent=`${names[j.state]||j.state} · 节点 ${j.node_id||'未分配'} · 尝试 ${j.attempt||1} · ${typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||'')}`;$('detail-spec').textContent=JSON.stringify({id:j.id,source:j.spec.source,params:j.spec.params,resources:j.spec.resources,environments:j.spec.environments,metric_protocol:j.spec.metric_protocol},null,2);
  const events=data.events||j.events||[];$('events').textContent=events.slice(-30).map(e=>JSON.stringify(e)).join('\n');$('actions').replaceChildren();const options=terminal.includes(j.state)?(['paused','interrupted','failed'].includes(j.state)&&j.spec.resume_supported!==false?[['resume','从检查点恢复']]:[]):[[j.spec.resume_supported===false?'cancel':'stop',j.spec.resume_supported===false?'停止实验（无续训）':'请求保存并停止'],...(j.spec.resume_supported===false?[]:[['cancel','取消实验']])];for(const [action,label] of options){const b=node('button',label,'subtle');b.onclick=async()=>{try{await api('/api/action',{job_id:id,action});notify('请求已记录；节点收到并执行后才会更新状态。离线节点不会立即响应。');await detail(id,false);}catch(e){notify(e.message);}};$('actions').append(b);}
  $('artifacts').replaceChildren();for(const a of data.artifacts||j.artifacts||[]){const b=node('button',`${a.name} · ${(a.size/1024).toFixed(1)} KiB`,'subtle');b.onclick=()=>download('/api/artifact?job_id='+encodeURIComponent(id)+'&sha256='+encodeURIComponent(a.sha256),a.name.split('/').pop());$('artifacts').append(b);}if(!$('artifacts').children.length)$('artifacts').append(node('p','还没有完整回传的文件。运行状态与文件归档分别同步。','muted'));
  document.getElementById("live-log").textContent=j.log_tail||'等待节点回传日志';metricData=events.map(e=>e.metrics||e.data?.metrics||e.payload?.metrics||{}).filter(m=>Number.isFinite(m.step));if(j.metrics&&Number.isFinite(j.metrics.step))metricData.push(j.metrics);const unique=new Map(metricData.map(m=>[m.step,m]));metricData=[...unique.values()].sort((a,b)=>a.step-b.step);const old=$('metric-select').value,keys=[...new Set(metricData.flatMap(m=>Object.keys(m)))].filter(k=>!['step','time','attempt'].includes(k));$('metric-select').replaceChildren(...keys.map(k=>{const o=node('option',k);o.value=k;return o;}));if(keys.includes(old))$('metric-select').value=old;drawChart();if(scroll)$('detail').scrollIntoView({behavior:'smooth',block:'start'});
}catch(e){notify('读取详情失败：'+e.message);}}
function drawChart(){const canvas=$('chart'),ctx=canvas.getContext('2d'),key=$('metric-select').value,points=metricData.filter(m=>Number.isFinite(m[key]));ctx.clearRect(0,0,760,240);ctx.font='12px sans-serif';ctx.fillStyle='#738078';if(!points.length){ctx.fillText('等待节点回传指标',28,120);return;}const min=Math.min(...points.map(p=>p[key])),max=Math.max(...points.map(p=>p[key])),first=points[0].step,last=points.at(-1).step;ctx.strokeStyle='#e0e8df';for(let i=0;i<4;i++){let y=30+i*55;ctx.beginPath();ctx.moveTo(60,y);ctx.lineTo(730,y);ctx.stroke();ctx.fillText((max-(max-min)*i/3).toPrecision(3),8,y+4);}ctx.strokeStyle='#236a55';ctx.lineWidth=2;ctx.beginPath();points.forEach((p,i)=>{const x=60+(p.step-first)/Math.max(1,last-first)*670,y=195-(p[key]-min)/Math.max(1e-10,max-min)*165;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();ctx.fillText('step '+first,60,222);ctx.fillText('step '+last,670,222);}
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
    row.append(label,node('span',names[job.state]||job.state,'badge '+job.state));row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label','查看实验 '+job.spec.name);row.onclick=()=>{showView('experiments');void detail(job.id,true);};row.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();row.click();}};$('recent-jobs').append(row);
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
function draftChanged(){if(!$('import-reviewed').disabled)$('import-reviewed').checked=false;updateImportJSON();}
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
  showView('projects');$('import-wizard').hidden=false;$('import-review-form').hidden=true;importBusy(true);
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
  $('import-reviewed').checked=false;setImportStep('review');updateImportJSON();
}
async function startImport(entry){
  importDraft=null;importBusy(true);$('import-review-form').hidden=true;setImportStep('source');importMessage('正在读取项目，不会执行算法源码…');
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
  try{const project=JSON.parse($('import-project-json').value),harness=JSON.parse($('import-harness-json').value),experiments=JSON.parse($('import-experiments-json').value);if(!project||typeof project!=='object'||Array.isArray(project)||!harness||typeof harness!=='object'||Array.isArray(harness)||!Array.isArray(experiments)||!experiments.length||experiments.some(item=>!item||!item.params||typeof item.params!=='object'||Array.isArray(item.params)))throw new Error('项目和 harness 需要对象，实验配置需要非空数组，每项包含 params 对象。');renderImportDraft({...importDraft,project,harness,experiments});importMessage('JSON 修改已应用到表单，保存时将完整验证配置。');}
  catch(error){importMessage('JSON 修改未应用：'+error.message);}
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
showView(location.hash.slice(1));refresh();setInterval(refresh,5000);
