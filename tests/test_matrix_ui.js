'use strict';
// Run directly with node tests/test_matrix_ui.js; no browser or subprocess needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../expman/static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../expman/static/index.html'), 'utf8');

function fragment(start, end) {
  const offset = source.indexOf(start), limit = source.indexOf(end, offset);
  assert.ok(offset >= 0 && limit > offset, `Missing source boundary: ${start}`);
  return source.slice(offset, limit);
}
function element(tag = 'div', text) {
  return {
    tag, textContent: text === undefined ? '' : String(text), children: [], value: '',
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {}, setCustomValidity(message) { this.validationMessage = message; },
    reportValidity() { this.reports = (this.reports || 0) + 1; return !this.validationMessage; },
    querySelectorAll() { return []; }, scrollIntoView() {},
  };
}
function baseContext(extra = {}) {
  const elements = new Map();
  return vm.createContext({
    node: element,
    $: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    copy: value => JSON.parse(JSON.stringify(value)),
    inlineFeedback() {}, notify() {},
    ...extra,
  });
}
function load(context, start, end) { vm.runInContext(fragment(start, end), context); }
function plain(value) { return JSON.parse(JSON.stringify(value)); }
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; }

function launchContext(api) {
  let request = 0;
  const context = baseContext({
    matrixDraft: {name: 'Matrix', spec: {params: {seed: 42}}, datasets: [], grid: {}},
    matrixAxes: [], readScheduling: () => ({scheduling: {mode: 'auto'}, priority: 0}),
    matrixEditorBusy: false,
    matrixStartRequests: new Map(), matrixPendingLaunches: new Map(), matrixStartsInFlight: new Set(),
    requestId: () => `request-${++request}`,
    api, loadMatrices: async () => {}, refresh: async () => {}, showMatrixResults: async () => {},
  });
  context.$('matrix-name').value = 'Matrix'; context.$('matrix-description').value = '';
  load(context, 'function matrixPayload(', 'function openMatrix(');
  load(context, 'function matrixBusy(', "$('matrix-form').onsubmit");
  load(context, 'async function startMatrix(', 'async function showMatrixResults(');
  return context;
}

const checks = [];
function check(name, callback) { checks.push([name, callback]); }

check('all static element references exist and original clipboard controls remain', () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length, 'HTML IDs must be unique');
  for (const match of source.matchAll(/\$\('([^']+)'\)/g)) {
    if (!match[1].endsWith('-')) assert.ok(ids.includes(match[1]), `Missing element ${match[1]}`);
  }
  for (const text of ['async function writeClipboardText(', 'async function copyDetailText(', "document.execCommand('copy')", "$('copy-live-log').dataset.copyAvailable", "$('copy-detail-state').onclick"]) {
    assert.ok(source.includes(text), `Original clipboard behavior removed: ${text}`);
  }
});

check('the complete application initializes without runtime errors before login', () => {
  const context = baseContext();
  const document = {
    getElementById: context.$, createElement: element,
    querySelector: () => element(), querySelectorAll: () => [],
    body: {classList: {toggle() {}}},
  };
  Object.assign(context, {
    document, sessionStorage: {getItem: () => ''}, localStorage: {getItem: () => ''},
    location: {hash: '', pathname: '/', search: '', origin: 'http://localhost'},
    history: {replaceState() {}}, performance: {now: () => 0},
    navigator: {}, window: {addEventListener() {}}, URLSearchParams,
    setInterval() {}, requestAnimationFrame() {},
  });
  vm.runInContext(source, context);
});

check('text and multiline inputs update immediately with silent input validation', () => {
  const context = baseContext();
  load(context, 'function editField(', 'function smallButton(');
  let value = '';
  const input = context.editField('Parameter', '', next => { if (!next) throw Error('Required'); value = next; }).children[0];
  input.value = 'seed'; input.oninput(); assert.equal(value, 'seed');
  input.value = ''; input.oninput(); assert.equal(input.validationMessage, 'Required'); assert.equal(input.reports, undefined);
  input.onchange(); assert.equal(input.reports, 1);
  const textarea = context.editField('Values', '', next => { value = next; }, {rows: 3}).children[0];
  textarea.value = '42\n43'; textarea.oninput(); assert.equal(value, '42\n43');
});

check('save-and-start retries a lost response without saving a new revision or launching twice', async () => {
  let revision = 0, saves = 0, starts = 0;
  const requests = [], serverRuns = new Map();
  const context = launchContext(async (endpoint, payload) => {
    if (endpoint.endsWith('/save')) { saves++; return {id: 'matrix-1', revision: ++revision, count: 4, name: 'Matrix'}; }
    requests.push({...payload});
    if (!serverRuns.has(payload.request_id)) serverRuns.set(payload.request_id, {ids: ['a', 'b', 'c', 'd']});
    if (starts++ === 0) throw Error('Response lost after server commit');
    return serverRuns.get(payload.request_id);
  });
  await context.saveMatrix(true);
  await context.saveMatrix(true);
  assert.equal(saves, 1, 'Retry must not save a new revision');
  assert.equal(serverRuns.size, 1, 'A retry must identify the existing batch');
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(context.matrixPendingLaunches.size, 0, 'Confirmed launches must clear retry state');
  await context.saveMatrix(true);
  assert.equal(saves, 2, 'An intentional subsequent run may save and launch again');
  assert.equal(serverRuns.size, 2);
});

check('concurrent editor submissions cannot save or start duplicate batches', async () => {
  const saving = deferred(); let saves = 0, starts = 0;
  const context = launchContext(async endpoint => {
    if (endpoint.endsWith('/save')) { saves++; await saving.promise; return {id: 'matrix-1', revision: 1, count: 1, name: 'Matrix'}; }
    starts++; return {ids: ['a']};
  });
  const first = context.saveMatrix(true), second = context.saveMatrix(true);
  assert.equal(saves, 1); saving.resolve(); await Promise.all([first, second]);
  assert.equal(starts, 1);
});

check('list rerenders cannot start another batch while a launch is still refreshing', async () => {
  const starting = deferred(), refreshing = deferred(); let starts = 0;
  const context = launchContext(async () => { starts++; await starting.promise; return {ids: ['a']}; });
  context.refresh = () => refreshing.promise;
  const matrix = {id: 'matrix-1', revision: 1, name: 'Matrix'};
  const first = context.startMatrix(matrix);
  await context.startMatrix(matrix);
  assert.equal(starts, 1);
  starting.resolve(); await Promise.resolve(); await Promise.resolve();
  await context.startMatrix(matrix);
  assert.equal(starts, 1, 'The lock must outlive the API response until UI refresh finishes');
  refreshing.resolve(); await first;
  assert.equal(context.matrixStartsInFlight.size, 0);
});

check('confirmed launches remain successful when refreshing their UI fails', async () => {
  for (const failingStep of ['refresh', 'loadMatrices', 'showMatrixResults']) {
    let starts = 0; const messages = [];
    const context = launchContext(async () => { starts++; return {ids: ['a']}; });
    context.notify = message => messages.push(message);
    context[failingStep] = async () => { throw Error('Refresh unavailable'); };
    const matrix = {id: 'matrix-1', revision: 1, name: 'Matrix'};
    context.matrixPendingLaunches.set(matrix.id, {matrix});
    await context.startMatrix(matrix);
    assert.equal(starts, 1);
    assert.match(messages.at(-1), /矩阵已启动.*刷新状态失败/);
    assert.doesNotMatch(messages.at(-1), /启动未确认|启动结果未确认|重试.*提交标识/);
    assert.equal(context.matrixPendingLaunches.size, 0);
    assert.equal(context.matrixStartRequests.size, 0);
    assert.equal(context.matrixStartsInFlight.size, 0);
  }
});

check('imported matrices default optional datasets and parameter overrides before editing', () => {
  const context = baseContext({
    matrixDraft: null, matrixTemplates: [], matrixAxes: [], availableTemplates: () => [],
    renderMatrixParameters() {}, renderMatrixDatasets() {}, renderMatrixAxes() {}, renderScheduling() {}, showView() {},
    valueText: value => String(value),
  });
  load(context, 'function openMatrix(', 'function renderMatrixParameters(');
  vm.runInContext(fragment("$('matrix-add-dataset').onclick=", "$('matrix-add-axis').onclick="), context);
  context.openMatrix({name: 'Imported', spec: {name: 'Template', params: {dataset: 'Books'}}, grid: {}});
  assert.deepEqual(plain(context.matrixDraft.datasets), []);
  context.$('matrix-add-dataset').onclick();
  assert.equal(context.matrixDraft.datasets[0].params.dataset, 'Books');
  context.openMatrix({name: 'Imported', spec: {name: 'Template', params: {}}, datasets: [{name: 'Books'}], grid: {}});
  assert.deepEqual(plain(context.matrixDraft.datasets[0].params), {});
});

check('controller cleanup failures remain visible with a retry action', () => {
  const context = baseContext({state: {project_deletions: [{name: 'Algorithm', digest: 'abc', controller_cleanup_error: 'Permission denied', deployments: []}]}});
  load(context, 'function smallButton(', 'async function deleteProject(');
  load(context, 'function renderProjectCleanups(', 'function renderScheduling(');
  context.renderProjectCleanups();
  const row = context.$('project-cleanup-list').children[0];
  const text = row.children.map(child => child.textContent).join('\n');
  assert.match(text, /Permission denied/); assert.match(text, /重试清理/); assert.doesNotMatch(text, /管理端项目包已清理/);
});

check('metric templates retain scientific notation and reject ambiguous capture groups', () => {
  const context = baseContext();
  load(context, 'function metricTemplate(', 'function renderMetricRules(');
  const rule = context.metricTemplate('step={step} loss={loss}');
  const match = new RegExp(rule.pattern.replace(/\(\?P</g, '(?<')).exec('step=7 loss=-1.4e-5');
  assert.equal(match.groups.step, '7'); assert.equal(match.groups.loss, '-1.4e-5');
  assert.throws(() => context.metricTemplate('{step} {loss} {loss}'));
  assert.throws(() => context.metricTemplate('loss={loss}'));
});

(async () => {
  for (const [name, callback] of checks) { await callback(); console.log(`PASS ${name}`); }
  console.log(`${checks.length} frontend regression checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
