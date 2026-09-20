'use strict';
// Behavioral checks for resource payloads, draft persistence, and rollout states.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../expman/static/app.js'), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
function fragment(start, end) {
  const begin = source.indexOf(start), finish = source.indexOf(end, begin);
  assert.ok(begin >= 0 && finish > begin, `Missing source boundary: ${start}`);
  return source.slice(begin, finish);
}
function context(extra = {}) {
  const elements = new Map(), document = {activeElement: null};
  function node(tag = 'div', text, cls) {
    const attributes = {};
    return {
      tagName: tag.toUpperCase(), textContent: text === undefined ? '' : String(text), className: cls || '',
      children: [], value: '', min: '', max: '', step: '', hidden: false, disabled: false, style: {},
      classList: {toggle() {}}, dataset: {},
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      setAttribute(key, value) { attributes[key] = value; },
      getAttribute(key) { return attributes[key] || ''; },
      setCustomValidity(message) { this.validationMessage = message; },
      reportValidity() { return !this.validationMessage; },
      querySelectorAll(selector) {
        const tags = selector.split(',').map(value => value.trim().toUpperCase());
        return this.children.flatMap(item => [...(tags.includes(item.tagName) ? [item] : []), ...item.querySelectorAll(selector)]);
      },
      focus() { document.activeElement = this; }, scrollIntoView() {},
    };
  }
  const $ = id => { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); };
  const env = vm.createContext({node, $, document, copy: clone, console, Date, Map,
    state: {jobs: [], nodes: []}, nodeResourceEditor: null, nodeResourceBusy: false,
    notify() {}, renderProjects() {}, renderJobs() {}, renderOverview() {},
    inlineFeedback(id, message) { $(id).textContent = message; $(id).hidden = !message; },
    ...extra,
  });
  env.load = (start, end) => vm.runInContext(fragment(start, end), env);
  env.load('function editField(', 'function smallButton(');
  env.load('function objectFields(', 'function renderProjectParameters(');
  return env;
}
function worker(overrides = {}) {
  return {
    id: 'a6000', last_seen: Date.now() / 1000, mode: 'run', resource_policy: null,
    snapshot: {
      capabilities: ['node-resource-policy-v1'], resource_policy_revision: 0,
      resource_policy_limits: {cpu_budget: 32, ram_budget_mb: 128 * 1024},
      policy: {max_running: 2, max_prefetch: 4, cpu_budget: 8, ram_budget_mb: 16384},
      gpus: [
        {uuid: 'GPU-A', name: 'A6000', total_mb: 49152, free_mb: 30000, max_jobs: 2, reserve_mb: 1024, local_enabled: true},
        {uuid: 'GPU-B', name: 'A6000', total_mb: 49152, free_mb: 40000, max_jobs: 0, reserve_mb: 1024, local_enabled: true},
        {uuid: 'GPU-disabled', name: 'Disabled device', total_mb: 8192, free_mb: 8192, max_jobs: 0, reserve_mb: 1024, local_enabled: false},
      ],
    }, ...overrides,
  };
}
function nodeContext(current = worker()) {
  const env = context({state: {nodes: [current], jobs: []}});
  env.load('function openNodeResources(', 'function render(){');
  env.load('function render(){', 'function renderJobs(){');
  const form = env.$('node-resource-form'); form.hidden = true;
  for (const id of ['node-resource-fields', 'node-resource-gpus']) form.append(env.$(id));
  for (const id of ['node-resource-save', 'node-resource-close', 'node-resource-reload']) {
    env.$(id).tagName = 'BUTTON'; form.append(env.$(id));
  }
  env.refresh = async () => env.render();
  env.openNodeResources('a6000'); return env;
}
function change(input, value) { input.value = String(value); if (input.oninput) input.oninput(); else input.onchange(); }
const checks = [];
function check(name, callback) { checks.push([name, callback]); }

check('shared resources are explicit, preserve zero and fractional budgets, and leave templates unchanged', () => {
  const env = context(), template = {gpu_memory_mb: 0, cpu: 0.5, ram_mb: 512, exclusive: true, gpu_count: 1};
  const original = clone(template);
  env.renderTaskResources('resources', template, () => {});
  let payload = env.readTaskResources('resources', template);
  assert.deepEqual(clone(payload), original);
  const inputs = env.$('resources').resourceInputs;
  change(inputs.exclusive, 'shared'); change(inputs.gpu_memory_mb, 6000); change(inputs.cpu, 1.5);
  payload = env.readTaskResources('resources', template);
  assert.deepEqual(clone(payload), {gpu_memory_mb: 6000, cpu: 1.5, ram_mb: 512, exclusive: false, gpu_count: 1});
  assert.deepEqual(template, original);
  env.renderTaskResources('resources', {}, () => {});
  assert.equal(env.readTaskResources('resources').exclusive, false);
});

check('invalid edited resource values cannot silently submit an older valid draft', () => {
  const env = context(); env.renderTaskResources('resources', {}, () => {});
  const input = env.$('resources').resourceInputs.gpu_memory_mb;
  for (const value of ['', '-1', '0.5', 'Infinity']) {
    change(input, value); assert.throws(() => env.readTaskResources('resources'), /超出有效范围/);
  }
});

check('node polling keeps input elements, keyboard focus, values, and feedback intact', () => {
  const env = nodeContext(), input = env.nodeResourceEditor.inputs.gpu_policy['GPU-A'].max_jobs;
  change(input, 4); input.focus(); env.inlineFeedback('node-resource-feedback', 'Saved feedback');
  for (let count = 0; count < 10; count++) {
    env.state.nodes[0].snapshot.gpus[0].free_mb -= 100; env.render();
  }
  assert.equal(env.nodeResourceEditor.inputs.gpu_policy['GPU-A'].max_jobs, input);
  assert.equal(env.document.activeElement, input); assert.equal(input.value, '4');
  assert.equal(env.$('node-resource-feedback').textContent, 'Saved feedback');
  assert.equal(env.nodeResourcePayload().gpu_policy['GPU-A'].max_jobs, 4);
});

check('node limits, remote-disabled cards, and locally disabled cards produce the correct payload', () => {
  const env = nodeContext(), editor = env.nodeResourceEditor;
  change(editor.inputs.policy.max_running, 4); change(editor.inputs.policy.cpu_budget, 3.5);
  change(editor.inputs.gpu_policy['GPU-B'].max_jobs, 2);
  assert.equal(editor.inputs.gpu_policy['GPU-disabled'], undefined, 'Local disable must not be remotely reversible');
  assert.deepEqual(clone(env.nodeResourcePayload()), {
    node_id: 'a6000', revision: 0,
    policy: {max_running: 4, max_prefetch: 4, cpu_budget: 3.5, ram_budget_mb: 16384},
    gpu_policy: {'GPU-A': {max_jobs: 2, reserve_mb: 1024}, 'GPU-B': {max_jobs: 2, reserve_mb: 1024}},
  });
  change(editor.inputs.policy.cpu_budget, 33); assert.throws(() => env.nodeResourcePayload(), /超出有效范围/);
  change(editor.inputs.policy.cpu_budget, 4); change(editor.inputs.gpu_policy['GPU-A'].reserve_mb, 50000);
  assert.throws(() => env.nodeResourcePayload(), /超出有效范围/);
});

check('old workers cannot save; upgraded offline workers retain pending settings and apply errors', async () => {
  const old = worker(); old.snapshot.capabilities = [];
  const env = nodeContext(old); let calls = 0; env.api = async () => { calls++; };
  assert.equal(env.$('node-resource-save').disabled, true); await env.saveNodeResources(); assert.equal(calls, 0);
  const current = worker({last_seen: 1, resource_policy: {revision: 2, policy: {max_running: 4}, gpu_policy: {}}});
  current.snapshot.resource_policy_error = 'GPU currently unavailable'; env.state.nodes = [current]; env.openNodeResources('a6000');
  assert.equal(env.$('node-resource-save').disabled, false);
  assert.match(env.$('node-resource-sync').textContent, /离线.*重连后生效/);
  assert.match(env.$('node-resource-sync').textContent, /GPU currently unavailable/);
  assert.equal(env.nodeResourcePayload().revision, 2);
  assert.equal(env.nodeResourcePayload().policy.max_running, 4);
});

check('lost node-policy responses retry the identical revision and preserve edits on conflicts', async () => {
  const env = nodeContext(), requests = []; let calls = 0;
  change(env.nodeResourceEditor.inputs.policy.max_running, 4);
  env.api = async (endpoint, payload) => {
    assert.equal(endpoint, '/api/node-policy'); requests.push(clone(payload));
    if (calls++ === 0) throw Error('Response lost');
    return {node_id: 'a6000', resource_policy: {revision: 1, policy: payload.policy, gpu_policy: payload.gpu_policy}};
  };
  await env.saveNodeResources(); await env.saveNodeResources();
  assert.deepEqual(requests[0], requests[1]); assert.equal(env.nodeResourceEditor.revision, 1);
  assert.match(env.$('node-resource-feedback').textContent, /资源设置已保存/);
  env.state.nodes[0].resource_policy.revision = 2; env.render();
  assert.match(env.$('node-resource-sync').textContent, /另一页面修改/);
  assert.equal(env.nodeResourceEditor.inputs.policy.max_running.value, '4');
  env.api = async () => { throw Error('Revision conflict'); }; await env.saveNodeResources();
  assert.equal(env.nodeResourceEditor.revision, 1);
  assert.equal(env.nodeResourceEditor.inputs.policy.max_running.value, '4');
});

check('confirmation requires matching revisions and explicitly refers to the saved policy', () => {
  const current = worker({resource_policy: {revision: 2, policy: {}, gpu_policy: {}}});
  current.snapshot.resource_policy_revision = 2;
  const env = nodeContext(current); change(env.nodeResourceEditor.inputs.policy.max_running, 4);
  env.render(); assert.match(env.$('node-resource-sync').textContent, /上次保存/);
  current.snapshot.resource_policy_revision = 3; env.render();
  assert.match(env.$('node-resource-sync').textContent, /版本不同/);
  assert.doesNotMatch(env.$('node-resource-sync').textContent, /已确认/);
});

check('single-experiment submission uses resource overrides without editing the project template', async () => {
  const template = {name: 'Original', params: {seed: 1}, resources: {gpu_memory_mb: 6000, cpu: 2, ram_mb: 8192, exclusive: true}};
  const env = context({projectPresets: [{template}], requestId: () => 'request', applyScheduling() {}, readScheduling() {}, showView() {}, refresh: async () => {}});
  const original = clone(template); env.$('project-preset').value = '0'; env.$('project-run-name').value = 'Shared'; env.$('project-params').value = '{"seed":42}';
  env.renderTaskResources('project-resources', template.resources, () => {});
  change(env.$('project-resources').resourceInputs.exclusive, 'shared');
  change(env.$('project-resources').resourceInputs.gpu_memory_mb, 8000);
  let submitted; env.api = async (_path, payload) => { submitted = payload; return {ids: ['job']}; };
  env.load("$('project-run-form').onsubmit=", "$('spec').value='';");
  await env.$('project-run-form').onsubmit({preventDefault() {}});
  assert.deepEqual(clone(submitted.spec.resources), {...original.resources, gpu_memory_mb: 8000, exclusive: false});
  assert.deepEqual(template, original);
});

check('matrix imports, template switches, and payloads preserve explicit resource choices', () => {
  const imported = {name: 'Matrix', spec: {name: 'Original', params: {seed: 1}, resources: {gpu_memory_mb: 6000, cpu: 2, ram_mb: 8192, exclusive: true}}, datasets: [], grid: {}};
  const other = {name: 'Second', params: {}, resources: {gpu_memory_mb: 2048, cpu: 1, ram_mb: 2048, exclusive: false}};
  const env = context({matrixDraft: null, matrixTemplates: [], matrixAxes: [],
    availableTemplates: () => [{label: 'Second', spec: other}],
    renderMatrixDatasets() {}, renderMatrixAxes() {}, renderScheduling() {}, showView() {},
    valueText: String, lines: value => value.split('\n').filter(Boolean), parseValue: JSON.parse,
    readScheduling: () => ({scheduling: {mode: 'auto'}, priority: 0}),
  });
  env.load('function matrixPayload(', 'function renderDatasetParameters(');
  env.openMatrix(imported);
  assert.equal(env.readTaskResources('matrix-resources').exclusive, true);
  change(env.$('matrix-resources').resourceInputs.exclusive, 'shared');
  change(env.$('matrix-resources').resourceInputs.gpu_memory_mb, 9000);
  env.$('matrix-template').value = '0'; env.switchMatrixTemplate();
  assert.equal(env.readTaskResources('matrix-resources').gpu_memory_mb, 2048);
  env.$('matrix-template').value = 'current'; env.switchMatrixTemplate();
  const payload = env.matrixPayload();
  assert.deepEqual(clone(payload.spec.resources), {gpu_memory_mb: 9000, cpu: 2, ram_mb: 8192, exclusive: false});
  env.openMatrix(payload); assert.deepEqual(clone(env.matrixPayload().spec.resources), clone(payload.spec.resources));
  assert.equal(imported.spec.resources.exclusive, true); assert.equal(other.resources.gpu_memory_mb, 2048);
});

(async () => {
  for (const [name, callback] of checks) { await callback(); console.log(`PASS ${name}`); }
  console.log(`${checks.length} resource UI checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
