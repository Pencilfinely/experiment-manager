'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('expman_token') || '', state = {jobs:[],nodes:[]}, selected = null, metricData = [];
const startupToken = new URLSearchParams(location.hash.slice(1)).get('token');
if(startupToken){token=startupToken;sessionStorage.setItem('expman_token',token);history.replaceState(null,'',location.pathname+location.search);}
const names = {queued:'排队中',assigned:'已分配',preparing:'准备资源',staging:'准备资源',ready:'离线就绪',starting:'启动中',running:'运行中',succeeded:'已完成',failed:'失败',interrupted:'已中断',paused:'已保存停止',canceled:'已取消'};
const terminal = ['succeeded','failed','interrupted','paused','canceled'];
const projectSelections = new Map(), projectUploads = new Map();
let projectPresets = [];
const requestId = () => Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('');
function node(tag, text, cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=String(text); if(cls)e.className=cls;return e; }
function notify(message) { $('notice').textContent=message; $('notice').hidden=!message; }
async function api(path, body) { const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(!response.ok){let msg=await response.text();throw new Error(msg);}return response.json(); }
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
    box.append(node('strong',project.name),node('p',`${project.project_id} · ${(project.size/1024/1024).toFixed(1)} MiB · ${project.digest.slice(0,12)}`));
    const chosen=projectSelections.get(project.digest)||new Set();projectSelections.set(project.digest,chosen);
    for(const worker of state.nodes||[]){
      const supported=(worker.snapshot?.capabilities||[]).includes('project-bundle-v1');
      const deployment=(project.deployments||[]).find(item=>item.node_id===worker.id);
      const label=node('label',undefined,'project-worker'),check=node('input');check.type='checkbox';check.value=worker.id;check.checked=chosen.has(worker.id);check.disabled=!supported;
      check.onchange=()=>check.checked?chosen.add(worker.id):chosen.delete(worker.id);
      const statuses={queued:'等待节点领取',downloading:'下载中',installing:'安装中',installed:'已安装',failed:'安装失败'};
      label.append(check,node('span',`${worker.id} · ${worker.online?'在线':'离线'} · ${supported?(statuses[deployment?.status]||'未分发'):'需更新算力代理'}`));box.append(label);
      if(deployment?.detail)box.append(node('p',deployment.detail,'muted'));
    }
    const deploy=node('button','分发到所选节点 / Deploy','subtle');
    deploy.onclick=async()=>{try{if(!chosen.size)throw new Error('先勾选目标算力机。');await api('/api/projects/deploy',{digest:project.digest,node_ids:[...chosen]});notify('分发已排队，节点会自动下载和安装。安装完成后点击“创建实验”。');await refresh();}catch(error){notify(error.message);}};
    const create=node('button','创建实验 / New experiment','subtle');create.onclick=()=>openProjectRun(project);box.append(deploy,create);$('projects').append(box);
  }
  if(!(state.projects||[]).length)$('projects').append(node('p','尚未上传项目包。/ No project bundles uploaded.','muted'));
}
function openProjectRun(project){
  projectPresets=[];
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
function fillProjectPreset(){const selectedPreset=projectPresets[Number($('project-preset').value)];if(!selectedPreset)return;$('project-run-name').value=selectedPreset.template.name;$('project-params').value=JSON.stringify(selectedPreset.template.params,null,2);}
$('project-preset').onchange=fillProjectPreset;
$('project-run-close').onclick=()=>{$('project-run-form').hidden=true;};
$('project-run-form').onsubmit=async event=>{
  event.preventDefault();$('project-run-submit').disabled=true;
  try{
    const preset=projectPresets[Number($('project-preset').value)];if(!preset)throw new Error('请选择节点和实验配置。');
    const spec=JSON.parse(JSON.stringify(preset.template));spec.name=$('project-run-name').value.trim();spec.params=JSON.parse($('project-params').value);
    if(!spec.params||typeof spec.params!=='object'||Array.isArray(spec.params))throw new Error('参数必须是 JSON 对象。');
    const result=await api('/api/jobs',{request_id:requestId(),spec});notify(`已提交 ${result.ids.length} 个实验到 ${preset.worker}。可以在实验记录中查看日志和结果。`);await refresh();
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
$('logout').onclick=()=>{sessionStorage.removeItem('expman_token');token='';$('login').hidden=false;$('workspace').hidden=true;$('connection').textContent='未连接';};
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
async function refresh(){if(!token)return;try{state=await api('/api/state');$('login').hidden=true;$('workspace').hidden=false;$('connection').textContent='● 管理中心在线';render();if(selected)await detail(selected,false);}catch(error){$('connection').textContent='○ 无法连接';notify('暂时无法获取管理中心状态，请检查程序是否运行、网络和令牌。已经准备好的节点任务不依赖此页面继续运行。 '+error.message.slice(0,150));}}
function render(){const jobs=state.jobs||[],nodes=state.nodes||[];const online=n=>Date.now()/1000-n.last_seen<45;$('count-queue').textContent=jobs.filter(j=>['queued','assigned','preparing','ready'].includes(j.state)).length;$('count-running').textContent=jobs.filter(j=>['starting','running'].includes(j.state)).length;$('count-done').textContent=jobs.filter(j=>j.state==='succeeded').length;$('count-nodes').textContent=nodes.filter(online).length;
  $('nodes').replaceChildren();for(const n of nodes){const snap=n.snapshot||{},box=node('div',undefined,'node'),heading=node('div',undefined,'node-name');heading.append(node('span',n.id),node('span',online(n)?'在线':'离线','badge'));box.append(heading,node('p',n.mode==='drain'?'已暂停接单和启动新任务':online(n)?'允许运行 · 本地策略仍需满足':'已有任务保持归属，等待重新连接'));
    if(!(snap.gpus||[]).length)box.append(node('p',snap.allow_demo?'CPU 演示节点':'GPU 尚未就绪或未授权'));
    for(const g of snap.gpus||[]){const gpu=node('div',`${g.name} · 空闲 ${Number.isFinite(g.free_mb)?(g.free_mb/1024).toFixed(1):'?'} / ${(g.total_mb/1024).toFixed(1)} GiB`,'gpu'),bar=node('div',undefined,'bar'),fill=node('i');fill.style.width=Math.max(0,Math.min(100,100*(1-g.free_mb/g.total_mb)))+'%';bar.append(fill);gpu.append(bar);box.append(gpu);}
    if(Number.isFinite(snap.pending_uploads))box.append(node('p',`待回传文件：${snap.pending_uploads}`));
    for(const template of (Array.isArray(snap.task_templates)?snap.task_templates:[])){
      const use=node('button','填入任务 / Use: '+String(template.name||'template'),'subtle');
      use.onclick=()=>{$('spec').value=JSON.stringify(template,null,2);$('grid').value='{}';notify('已填入节点任务，核对后点击“提交到队列”。/ Review the task, then submit once.');$('submit-form').scrollIntoView({behavior:'smooth'});};box.append(use);
    }
    const button=node('button',n.mode==='drain'?'恢复接单':'暂停接单','subtle');button.onclick=async()=>{try{await api('/api/node-mode',{node_id:n.id,mode:n.mode==='drain'?'run':'drain'});notify('策略已记录，节点下次连接后生效。暂停接单不会终止正在运行的实验。');await refresh();}catch(e){notify(e.message);}};box.append(button);$('nodes').append(box);}
  if(!nodes.length)$('nodes').append(node('p','尚无节点。点击“添加算力机”，再启动算力端。','muted'));
  renderProjects();renderJobs();
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
$('metric-select').onchange=drawChart;$('close-detail').onclick=()=>{selected=null;$('detail').hidden=true;};refresh();setInterval(refresh,5000);
