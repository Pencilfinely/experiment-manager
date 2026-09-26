'use strict';
globalThis.MobileModel = (() => {
  const terminal = new Set(['succeeded', 'canceled', 'failed', 'paused', 'interrupted']);
  const names = {queued:'排队中',assigned:'已分配',preparing:'准备中',ready:'等待启动',starting:'启动中',running:'运行中',succeeded:'已完成',canceled:'已取消',failed:'失败',paused:'已暂停',interrupted:'已中断'};
  function actions(job) {
    if (job.command_id > job.command_ack) return [];
    if (terminal.has(job.state)) return ['paused','interrupted','failed'].includes(job.state) && job.spec.resume_supported !== false ? [{value:'resume',label:'从检查点恢复'}] : [];
    return job.spec.resume_supported === false ? [{value:'cancel',label:'停止实验（无续训）'}] : [{value:'stop',label:'请求保存并停止'},{value:'cancel',label:'取消实验'}];
  }
  function canControl(session, connected, sampledAt, busy, current = Date.now()) {
    return session?.permission === 'control' && connected && current - sampledAt < 15000 && !busy;
  }
  function series(events, key, attempt) {
    return (events || []).filter(e => e.kind === 'metrics' && (e.data.attempt === undefined || e.data.attempt === attempt))
      .map(e => ({x:e.data.metrics?.step ?? e.time,y:e.data.metrics?.[key]}))
      .filter(p => Number.isFinite(p.x) && Number.isFinite(p.y));
  }
  function path(points) {
    if (!points.length) return '';
    const xs=points.map(p=>p.x), ys=points.map(p=>p.y), xmin=Math.min(...xs), xmax=Math.max(...xs), ymin=Math.min(...ys), ymax=Math.max(...ys);
    return points.map(p=>`${16+(p.x-xmin)/(xmax-xmin||1)*288},${124-(p.y-ymin)/(ymax-ymin||1)*104}`).join(' ');
  }
  return {terminal,names,actions,canControl,series,path};
})();
