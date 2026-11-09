const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');

class Element {
  constructor() {
    this.children = [];
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this.value = '';
    this.href = '/search';
    this.files = [];
    this.classList = {toggle() {}};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  closest() { return this; }
}

function harness(search, {failCatalog = false} = {}) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const calls = [];
  let catalog = {items: [], next_cursor: null, total_documents: 0};
  let holdCatalog = null;
  const location = new URL('http://127.0.0.1:8765/' + (search || ''));
  const context = {
    document: {
      getElementById: node,
      createElement: () => new Element(),
      querySelectorAll: selector => (
        selector === '.project' ? node('projects').children.filter(item => item.className === 'project') : []
      ),
    },
    location, URL, URLSearchParams, console,
    history: {replaceState(_state, _title, next) { location.href = String(next); }},
    fetch: async (url, options) => {
      calls.push(String(url));
      if (url === '/api/health') {
        return {ok: true, json: async () => ({status: 'ready', message: '工作区已就绪', max_upload_bytes: 100})};
      }
      if (url === '/api/projects') {
        return {ok: true, json: async () => [
          {id: 'new-b', name: '较新的乙', created_at: '2026-11-08T00:00:00Z'},
          {id: 'old-a', name: '较早的甲', created_at: '2026-11-01T00:00:00Z'},
        ]};
      }
      if (String(url).includes('/material-catalog')) {
        if (holdCatalog) await holdCatalog;
        if (failCatalog) return {ok: false, json: async () => ({detail: '材料暂时无法读取'})};
        return {ok: true, json: async () => catalog};
      }
      return {ok: false, json: async () => ({detail: '未预期的请求 ' + url})};
    },
  };
  context.window = context;
  vm.createContext(context);
  return {context, node, calls, location, setCatalog(value) { catalog = value; }, hold(promise) { holdCatalog = promise; }};
}

(async () => {
  const source = fs.readFileSync(path.join(__dirname, '../../../src/dd_agent/static/app.js'), 'utf8');
  const noProject = harness('');
  vm.runInContext(source, noProject.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(
    noProject.calls.some(url => url.includes('material-catalog')), false,
    '没有当前项目时不应读取任何项目的材料');
  assert.strictEqual(noProject.node('project-title').textContent, '选择或创建一个项目');
  assert.strictEqual(noProject.node('search-link').href.includes('project='), false, '没有当前项目时检索链接不能带上某个项目');

  const older = harness('?project=old-a');
  vm.runInContext(source, older.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(older.calls.some(url => url.includes('/api/projects/old-a/material-catalog')), '刷新后应保持地址中的较早项目');
  assert.ok(!older.calls.some(url => url.includes('/api/projects/new-b/material-catalog')), '不应改读最新项目');
  assert.strictEqual(older.node('search-link').href, '/search?project=old-a');

  let release;
  const pending = new Promise(resolve => { release = resolve; });
  older.hold(pending);
  older.setCatalog({items: [{document_id: 'doc-a', filenames: ['只属于甲.pdf'], status: 'registered', byte_size: 10, created_at: '2026-11-01T00:00:00Z', page_count: null, reading_version: null, reason: ''}], next_cursor: null, total_documents: 1});
  const late = older.context.selectProject('old-a');
  await new Promise(resolve => setImmediate(resolve));
  older.hold(null);
  older.setCatalog({items: [], next_cursor: null, total_documents: 0});
  await older.context.selectProject('new-b');
  release();
  await late;
  assert.strictEqual(older.node('documents').textContent.includes('只属于甲'), false, '迟到的旧项目材料不能画进新项目');
  assert.ok(older.location.href.includes('project=new-b'));
  assert.strictEqual(older.node('empty').textContent.includes('还没有材料'), true);

  const failed = harness('?project=old-a', {failCatalog: true});
  vm.runInContext(source, failed.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(failed.node('list-error').textContent.includes('材料暂时无法读取'), true);
  assert.strictEqual(failed.node('documents').children.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
