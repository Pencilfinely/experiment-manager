'use strict';

// Durations come from the worker. Only the live display advances between reports.
globalThis.ExperimentTiming = (() => {
  const beforeRun = new Set(['queued', 'assigned', 'preparing', 'staging', 'ready', 'starting']);
  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const total = Math.floor(seconds), days = Math.floor(total / 86400);
    const clock = [Math.floor(total / 3600) % 24, Math.floor(total / 60) % 60, total % 60]
      .map(value => String(value).padStart(2, '0')).join(':');
    return (days ? days + ' 天 ' : '') + clock;
  }
  function describe(job, serverNow, online = true) {
    const timing = job.timing;
    if (!timing || !Number.isFinite(timing.elapsed_seconds)) {
      const waiting = beforeRun.has(job.state) && (job.attempt || 0) <= 1;
      return {text: waiting ? '尚未开始' : '暂无记录', note: waiting ? '等待实验开始运行' : '此实验尚无算力端计时记录', live: false};
    }
    const live = job.state === 'running' && Number.isFinite(timing.started_at) && Number.isFinite(timing.received_at);
    const elapsed = timing.elapsed_seconds + (live ? Math.max(0, serverNow - timing.received_at) : 0);
    let note = live ? (online ? '运行中 · 实时估算' : '等待同步 · 估算时长') : '累计运行时长';
    if (timing.complete === false) note += ' · 记录不完整';
    if (timing.started_at === null && timing.complete !== false) note = '尚未开始运行';
    return {text: formatDuration(elapsed), note, live};
  }
  return {formatDuration, describe};
})();
