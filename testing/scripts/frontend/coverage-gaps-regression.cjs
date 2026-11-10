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

function textOf(node) {
  if (!node) return '';
  return [node.textContent || '', ...(node.children || []).map(textOf)].join(' ');
}

function harness(search) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const calls = [];
  let hold = null;
  let coverage = {
    project_id: 'old-a', complete: false, definition: '这些数字不能相加。',
    counts: {known_pages: 2, unprocessed: 1, failed: 1, unknown: 1, empty_text: 1, pending_rebuild: 1},
    items: [{document_id: 'doc-a', filenames: ['只属于甲.pdf'], page_number: 2, kinds: ['failed']}],
    next_cursor: null,
  };
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
    fetch: async (url) => {
      calls.push(String(url));
      if (String(url).includes('/coverage')) {
        if (hold) await hold;
        const project = String(url).includes('/old-a/') ? 'old-a' : 'new-b';
        const body = project === 'old-a' ? coverage : {
          project_id: 'new-b', complete: false, definition: '这些数字不能相加。',
          counts: {known_pages: 0, unprocessed: 0, failed: 0, unknown: 0, empty_text: 0, pending_rebuild: 0},
          items: [], next_cursor: null,
        };
        return {ok: true, json: async () => body};
      }
      if (url === '/api/health') return {ok: true, json: async () => ({status: 'ready', message: '工作区已就绪', max_upload_bytes: 100})};
      if (url === '/api/projects') return {ok: true, json: async () => [
        {id: 'new-b', name: '较新的乙', created_at: '2026-11-08T00:00:00Z'},
        {id: 'old-a', name: '较早的甲', created_at: '2026-11-01T00:00:00Z'},
      ]};
      if (String(url).includes('/material-catalog')) return {ok: true, json: async () => ({items: [], next_cursor: null, total_documents: 0})};
      if (String(url).endsWith('/subjects')) return {ok: true, json: async () => []};
      return {ok: false, json: async () => ({detail: '未预期的请求 ' + url})};
    },
  };
  context.window = context;
  vm.createContext(context);
  return {context, node, calls, hold(promise) { hold = promise; }};
}

(async () => {
  const source = fs.readFileSync(path.join(__dirname, '../../../src/dd_agent/static/app.js'), 'utf8');
  const noProject = harness('');
  vm.runInContext(source, noProject.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(noProject.calls.some(url => url.includes('/coverage')), false, '没有当前项目时不请求覆盖');
  assert.strictEqual(noProject.calls.some(url => url.includes('/search')), false, '覆盖面板不能靠检索凑数');
  assert.ok(textOf(noProject.node('coverage-summary')).includes('先选择项目'));

  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const older = harness('?project=old-a');
  older.hold(pending);
  vm.runInContext(source, older.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(older.calls.some(url => url.includes('/api/projects/old-a/coverage')));
  assert.ok(!older.calls.some(url => url.includes('/api/projects/new-b/coverage')));
  assert.ok(!older.calls.some(url => url.includes('/search')));
  older.hold(null);
  const late = older.context.selectProject('old-a');
  await new Promise(resolve => setImmediate(resolve));
  await older.context.selectProject('new-b');
  release();
  await late;
  await new Promise(resolve => setImmediate(resolve));
  const shown = textOf(older.node('coverage-gaps')) + textOf(older.node('coverage-summary'));
  assert.strictEqual(shown.includes('只属于甲'), false, '迟到的甲项目缺口不能画进乙');
  assert.ok(shown.includes('不能视为全文可检索'));
  assert.ok(shown.includes('未处理') && shown.includes('失败') && shown.includes('未知') && shown.includes('空文本') && shown.includes('待重建'));
  assert.ok(older.calls.filter(url => url.includes('/coverage')).every(url => url.includes('/api/projects/')));
})().catch(error => { console.error(error); process.exitCode = 1; });
