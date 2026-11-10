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
    this.checked = false;
    this.type = '';
    this.href = '/search';
    this.files = [];
    this.classList = {toggle() {}};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  closest() { return this; }
}

function textOf(el) {
  if (!el) return '';
  return [el.textContent || '', ...(el.children || []).map(textOf)].join(' ');
}
function walk(el, visit) {
  if (!el) return;
  visit(el);
  for (const child of el.children || []) walk(child, visit);
}

function workspaceHarness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const calls = [];
  let failSave = true;
  const catalog = {items: [
    {document_id: 'u1', filenames: ['空白.pdf'], status: 'registered', byte_size: 10, created_at: '2026-11-01T00:00:00Z', page_count: null, reading_version: null, reason: '', category: 'uncategorized', subject_id: null, subject_name: null, date_kind: 'none', point_date: null, period_start: null, period_end: null},
    {document_id: 'a1', filenames: ['章程.pdf'], status: 'registered', byte_size: 12, created_at: '2026-11-02T00:00:00Z', page_count: 1, reading_version: 'v', reason: '', category: 'articles', subject_id: 's1', subject_name: '甲公司', date_kind: 'point', point_date: '2023-06-01', period_start: null, period_end: null},
  ], next_cursor: null, total_documents: 2};
  const location = new URL('http://127.0.0.1:8765/?project=p1');
  const context = {
    document: {
      getElementById: node,
      createElement: () => new Element(),
      querySelectorAll: selector => (selector === '.project' ? node('projects').children.filter(item => item.className === 'project') : []),
    },
    location, URL, URLSearchParams, console,
    history: {replaceState(_state, _title, next) { location.href = String(next); }},
    fetch: async (url, options) => {
      const entry = {url: String(url), method: options && options.method, body: options && options.body ? JSON.parse(options.body) : null};
      calls.push(entry);
      if (url === '/api/health') return {ok: true, json: async () => ({status: 'ready', message: '工作区已就绪', max_upload_bytes: 100})};
      if (url === '/api/projects') return {ok: true, json: async () => [{id: 'p1', name: '甲项目', created_at: '2026-11-01T00:00:00Z'}]};
      if (String(url).includes('/material-catalog')) return {ok: true, json: async () => catalog};
      if (String(url).endsWith('/subjects')) return {ok: true, json: async () => [{id: 's1', name: '甲公司', role: 'target', project_id: 'p1'}]};
      if (options && options.method === 'PATCH' && String(url).endsWith('/metadata')) {
        if (failSave) return {ok: false, json: async () => ({detail: '时点日期无效。', fields: {point_date: '须填写单一日期。'}})};
        return {ok: true, json: async () => ({document: {id: 'a1', category: entry.body.category}})};
      }
      if (options && options.method === 'POST' && String(url).endsWith('/documents/metadata')) {
        return {ok: true, json: async () => ({documents: []})};
      }
      return {ok: false, json: async () => ({detail: '未预期的请求 ' + url})};
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../../src/dd_agent/static/app.js'), 'utf8'), context);
  return {node, calls, text: () => textOf(node('documents')) + textOf(node('subjects')), setFailSave(value) { failSave = value; }};
}

function searchHarness(search) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  const calls = [];
  let response = {coverage: {indexed_pages: 1, total_pages: 1, unread_documents: 0, complete: true}, status: 'no_matches', results: [], message: '过滤后没有命中。项目已有索引，本次条件过窄。未分类被排除 1 份。', excluded_uncategorized: 1, excluded_unsubjected: 0, excluded_undated: 0};
  const context = {
    document: {getElementById: node, createElement: () => new Element()},
    location: {search}, URLSearchParams, AbortController, console, addEventListener() {},
    fetch: async (url, options) => {
      calls.push({url: String(url), method: options && options.method, body: options && options.body ? JSON.parse(options.body) : null});
      if (String(url).includes('/material-catalog')) return {ok: true, json: async () => ({items: [{document_id: 'd1', filenames: ['章程.pdf']}], total_documents: 1, next_cursor: null})};
      if (String(url).endsWith('/subjects')) return {ok: true, json: async () => [{id: 's1', name: '甲公司'}]};
      if (!options) return {ok: true, json: async () => [{id: 'p1', name: '甲项目'}]};
      if (options.method === 'POST') return {ok: true, json: async () => response};
      return {ok: false, json: async () => ({detail: '未预期的请求'})};
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../../src/dd_agent/static/search.js'), 'utf8'), context);
  return {node, calls, setResponse(value) { response = value; }};
}

const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const page = workspaceHarness();
  let shown = '';
  for (let i = 0; i < 10 && !shown.includes('公司章程'); i += 1) {
    await flush();
    shown = page.text();
  }
  const editorOptions = page.node('editor-category').children;
  const interviewOption = editorOptions.find(el => el.value === 'interview_record');
  assert.ok(interviewOption, '材料信息的类别下拉包含访谈记录');
  assert.strictEqual(interviewOption.textContent, '访谈记录');
  assert.ok(page.node('batch-category').children.some(el => el.value === 'interview_record'), '批量类别包含访谈记录');
  assert.ok(shown.includes('未分类'), '未填类别显示未分类');
  assert.ok(shown.includes('未填写'), '未填主体或日期显示未填写');
  assert.ok(shown.includes('公司章程') && shown.includes('甲公司') && shown.includes('2023-06-01'), '已填元数据出现在材料列表');
  assert.ok(shown.includes('改名') && !shown.includes('删除'), '主体可以改名，列表不提供删除');

  let button = null;
  walk(page.node('documents'), el => { if (el.textContent === '材料信息') button = el; });
  assert.ok(button, '每份材料可以打开材料信息');
  button.onclick();
  page.node('editor-category').value = 'litigation';
  page.node('editor-date-kind').value = 'point';
  page.node('editor-point').value = '';
  await page.node('editor-save').onclick();
  assert.strictEqual(page.node('editor-category').value, 'litigation', '保存失败时不清空已填类别');
  assert.ok(page.node('editor-error').textContent.includes('时点'), '保存失败指出字段');
  page.setFailSave(false);
  await page.node('editor-save').onclick();
  const saved = page.calls.filter(call => call.method === 'PATCH' && call.url.endsWith('/metadata')).pop();
  assert.strictEqual(saved.body.category, 'litigation');
  assert.strictEqual(saved.body.subject_id, 's1');
  button.onclick();
  page.node('editor-category').value = 'interview_record';
  await page.node('editor-save').onclick();
  const interviewSaved = page.calls.filter(call => call.method === 'PATCH' && call.url.endsWith('/metadata')).pop();
  assert.strictEqual(interviewSaved.body.category, 'interview_record', '访谈记录可以提交保存');

  walk(page.node('documents'), el => { if (el.className === 'pick-material') el.checked = true; });
  page.node('batch-category-toggle').checked = true;
  page.node('batch-category').value = 'board_resolution';
  page.node('batch-subject-toggle').checked = false;
  page.node('batch-date-toggle').checked = false;
  await page.node('batch-apply').onclick();
  const batch = page.calls.filter(call => call.method === 'POST' && call.url.endsWith('/documents/metadata')).pop();
  assert.deepStrictEqual(batch.body.document_ids.sort(), ['a1', 'u1']);
  assert.deepStrictEqual(batch.body.category, {set: true, value: 'board_resolution'});
  assert.strictEqual(batch.body.subject, undefined, '未勾选主体时不提交主体');
  assert.strictEqual(batch.body.date, undefined, '未勾选日期时不提交日期');

  const blank = searchHarness('');
  await flush();
  await blank.node('search-form').onsubmit({preventDefault() {}});
  assert.ok(!blank.calls.some(call => String(call.url).includes('/search')), '没有当前项目时不发起检索');

  const chosen = searchHarness('?project=p1');
  await flush();
  chosen.node('search-project').value = 'p1';
  chosen.node('search-filters').open = true;
  await chosen.node('search-filters').ontoggle();
  let interviewFilter = null;
  walk(chosen.node('filter-categories'), el => { if (el.value === 'interview_record') interviewFilter = el; });
  assert.ok(interviewFilter, '检索类别过滤包含访谈记录');
  assert.ok(textOf(chosen.node('filter-categories')).includes('访谈记录'));
  walk(chosen.node('filter-categories'), el => { if (el.value === 'articles') el.checked = true; });
  walk(chosen.node('filter-documents'), el => { if (el.value === 'd1') el.checked = true; });
  chosen.node('page-from').value = '2';
  chosen.node('page-to').value = '2';
  chosen.node('query').value = '注册资本';
  const pending = chosen.node('search-form').onsubmit({preventDefault() {}});
  await flush();
  await pending;
  const posted = chosen.calls.filter(call => call.method === 'POST').pop();
  assert.ok(String(posted.url).includes('/api/projects/p1/search'));
  assert.deepStrictEqual(posted.body.document_ids, ['d1']);
  assert.deepStrictEqual(posted.body.categories, ['articles']);
  assert.strictEqual(posted.body.page_from, 2);
  assert.strictEqual(posted.body.page_to, 2);
  assert.ok(chosen.node('search-state').textContent.includes('过滤后没有命中'));
  assert.ok(chosen.node('search-state').textContent.includes('未分类'));
  assert.ok(!chosen.node('search-state').textContent.includes('尚无当前版本索引'));

  chosen.setResponse({coverage: {indexed_pages: 0, total_pages: 0, unread_documents: 0, complete: false, office_chunks: 0}, status: 'not_indexed', results: [], message: '项目尚无当前版本索引。'});
  await chosen.node('search-form').onsubmit({preventDefault() {}});
  assert.ok(chosen.node('search-state').textContent.includes('尚无当前版本索引'));
  assert.ok(!chosen.node('search-state').textContent.includes('过滤后没有命中'));
})().catch(error => { console.error(error); process.exitCode = 1; });
