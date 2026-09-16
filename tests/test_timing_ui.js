'use strict';
// Run with: node tests/test_timing_ui.js (also supports node --test).
const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../expman/static/timing.js');
const {formatDuration, describe} = globalThis.ExperimentTiming;
const job = (state, changes = {}) => ({state, attempt:1, timing:{
  started_at:100, finished_at:null, elapsed_seconds:60, observed_at:160,
  received_at:1000, complete:true, ...changes
}});

test('duration formatting keeps days and zero padding', () => {
  assert.equal(formatDuration(0), '00:00:00');
  assert.equal(formatDuration(3599.9), '00:59:59');
  assert.equal(formatDuration(3600), '01:00:00');
  assert.equal(formatDuration(90061), '1 天 01:01:01');
  for (const value of [null, undefined, NaN, Infinity, -1]) assert.equal(formatDuration(value), '—');
});
test('live duration advances from controller receipt regardless of worker clock skew', () => {
  assert.equal(describe(job('running'), 1002).text, '00:01:02');
  assert.equal(describe(job('running', {observed_at:900000}), 1002).text, '00:01:02');
  assert.equal(describe(job('running'), 999).text, '00:01:00');
});
test('stopped jobs stay frozen and resume preparation does not count waiting time', () => {
  for (const state of ['paused', 'failed', 'interrupted', 'canceled', 'succeeded', 'ready', 'starting']) {
    const display = describe(job(state), 100000);
    assert.equal(display.text, '00:01:00');
    assert.equal(display.live, false);
  }
});
test('missing and partial historical records cannot look like complete zero durations', () => {
  assert.equal(describe({state:'queued'}, 1000).text, '尚未开始');
  assert.equal(describe({state:'ready', attempt:2}, 1000).text, '暂无记录');
  assert.equal(describe({state:'succeeded'}, 1000).text, '暂无记录');
  assert.match(describe(job('running', {complete:false}), 1000).note, /记录不完整/);
  assert.equal(describe(job('ready', {started_at:null, elapsed_seconds:0}), 1000).live, false);
});
test('offline running jobs label the estimate while awaiting authoritative results', () => {
  const display = describe(job('running'), 1010, false);
  assert.equal(display.text, '00:01:10');
  assert.match(display.note, /等待同步.*估算/);
});
