const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}

function editorHarness(api) {
  const tab = {path: '/work.ipynb', kind: 'notebook', cells: [{id: 'a', source: 'base'}]};
  const editor = {value: 'first edit'};
  const context = {tab, editor, active: tab.path, currentSession: 'workspace',
    cells: tab.cells, activeTab() {return this.tab;},
    document: {getElementById: () => editor}, api, renderTabs() {}, flash() {},
    URLSearchParams, clearTimeout, setTimeout, setInterval: () => 0};
  // Functions invoked from a VM need an explicit closure rather than `this`.
  context.activeTab = () => context.tab;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('src/gusnotebook/static/js/actions.js', 'utf8'), context);
  return {context, editor, tab, run: code => vm.runInContext(code, context)};
}

test('failed saves remain dirty and an explicit retry preserves the original base', async () => {
  let fail = true;
  const bodies = [];
  const h = editorHarness(async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    if (fail) throw new Error('offline');
  });
  await assert.rejects(h.run("saveCell('a')"), /offline/);
  assert.equal(h.tab.dirty, true);
  assert.equal(h.run("unsaved.has('a')"), true);
  fail = false;
  await h.run("saveCell('a')");
  assert.equal(h.tab.dirty, false);
  assert.equal(bodies[1].expected_source, 'base');
});

test('typing during a save serializes the newer revision and advances its base', async () => {
  const requests = [];
  const h = editorHarness((_url, options) => {
    const request = {...deferred(), body: JSON.parse(options.body)};
    requests.push(request);
    return request.promise;
  });
  const first = h.run("saveCell('a')");
  h.editor.value = 'second edit';
  const second = h.run("saveCell('a')");
  assert.equal(requests.length, 1);
  requests[0].resolve({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].body, {source: 'second edit', expected_source: 'first edit'});
  assert.equal(h.tab.dirty, true);
  requests[1].resolve({});
  await Promise.all([first, second]);
  assert.equal(h.tab.dirty, false);
});

test('a pending save stays pinned to its notebook when another tab has the same cell id', async () => {
  const request = deferred();
  const urls = [];
  const h = editorHarness(url => {urls.push(url); return request.promise;});
  const firstTab = h.tab;
  const saving = h.run("saveCell('a')");
  h.context.tab = {path: '/other.ipynb', kind: 'notebook', cells: [{id: 'a', source: 'other'}]};
  h.context.active = '/other.ipynb';
  h.editor.value = 'other text';
  request.resolve({});
  await saving;
  assert.equal(urls.length, 1);
  assert.match(urls[0], /work.ipynb/);
  assert.equal(firstTab.dirty, false);
  assert.equal(h.context.tab.cells[0].source, 'other');
});

test('a successful text save advances disk version even while the editor is dirty', async () => {
  const request = deferred();
  const t = {kind: 'text', path: '/tool.py', text: 'first', diskVersion: 'v1', editRevision: 1, dirty: true};
  const context = {t, active: t.path, document: {getElementById: () => ({textContent: ''})},
    AbortController, setTimeout, clearTimeout, renderTabs() {}, isMarkupTab: () => false,
    api: () => request.promise};
  const source = fs.readFileSync('src/gusnotebook/static/js/cells.js', 'utf8');
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('async function persistText('), source.indexOf('\nfunction saveText(')), context);
  const saving = vm.runInContext('persistText(t, t.text)', context);
  t.editRevision++;
  t.text = 'second';
  request.resolve({disk_version: 'v2'});
  await saving;
  assert.equal(t.diskVersion, 'v2');
  assert.equal(t.text, 'second');
  assert.equal(t.dirty, true);
});
