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
    this.classList = {toggle() {}, add() {}};
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  closest() { return this; }
}

class FakeFormData {
  constructor() { this.data = new Map(); }
  append(key, value) { this.data.set(key, value); }
  get(key) { return this.data.has(key) ? this.data.get(key) : null; }
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
    {document_id: 'u1', filenames: ['空白.pdf'], status: 'registered', byte_size: 10, created_at: '2026-11-01T00:00:00Z', page_count: null, reading_version: null, reason: '', category: 'other', subject_id: null, subject_name: null, date_kind: 'none', point_date: null, period_start: null, period_end: null, processing_status: 'queued'},
    {document_id: 'a1', filenames: ['章程.pdf'], status: 'registered', byte_size: 12, created_at: '2026-11-02T00:00:00Z', page_count: 1, reading_version: 'v', reason: '', category: 'organizational_docs', subject_id: 's1', subject_name: '甲公司', date_kind: 'point', point_date: '2023-06-01', period_start: null, period_end: null, processing_status: 'completed'},
  ], next_cursor: null, total_documents: 2};
  const categoryCounts = {other: 1, organizational_docs: 1};
  const subjects = [{id: 's1', name: '甲公司', role: 'target', project_id: 'p1'}];
  const location = new URL('http://127.0.0.1:8765/?project=p1');
  const context = {
    document: {
      getElementById: node,
      createElement: () => new Element(),
      querySelectorAll: selector => (selector === '.project' ? node('projects').children.filter(item => item.className === 'project') : []),
    },
    location, URL, URLSearchParams, console, FormData: FakeFormData,
    history: {replaceState(_state, _title, next) { location.href = String(next); }},
    fetch: async (url, options) => {
      const entry = {url: String(url), method: options && options.method, body: options && options.body ? (typeof options.body === 'string' ? JSON.parse(options.body) : null) : null};
      if (options && options.body && typeof options.body.append === 'function') {
        entry.body = {category: options.body.get && options.body.get('category')};
      }
      calls.push(entry);
      if (url === '/api/health') return {ok: true, json: async () => ({status: 'ready', message: '工作区已就绪', max_upload_bytes: 100})};
      if (url === '/api/projects') return {ok: true, json: async () => [{id: 'p1', name: '甲项目', created_at: '2026-11-01T00:00:00Z'}]};
      if (String(url).includes('/material-catalog')) {
        const wanted = new URL(String(url), 'http://127.0.0.1').searchParams.getAll('categories');
        let items = catalog.items;
        if (wanted.length) items = items.filter(item => wanted.includes(item.category));
        return {ok: true, json: async () => ({items, next_cursor: null, total_documents: items.length, category_counts: categoryCounts})};
      }
      if (options && options.method === 'POST' && String(url).endsWith('/subjects')) {
        const row = {id: 's2', name: entry.body.name, role: entry.body.role, project_id: 'p1'};
        subjects.push(row);
        return {ok: true, json: async () => row};
      }
      if (String(url).endsWith('/subjects')) return {ok: true, json: async () => subjects};
      if (options && options.method === 'POST' && String(url).endsWith('/documents')) {
        return {ok: true, json: async () => ({duplicate: false, document: {status: 'registered'}})};
      }
      if (options && options.method === 'PATCH' && String(url).endsWith('/metadata')) {
        if (failSave) return {ok: false, json: async () => ({detail: '时点日期无效。', fields: {point_date: '须填写单一日期。'}})};
        return {ok: true, json: async () => ({document: {id: 'a1', category: entry.body.category}})};
      }
      if (options && options.method === 'POST' && String(url).endsWith('/documents/metadata')) {
        return {ok: true, json: async () => ({documents: []})};
      }
      if (options && options.method === 'POST' && /\/api\/projects\/[^/]+\/processing$/.test(String(url))) {
        const category = entry.body && entry.body.category || null;
        return {ok: true, json: async () => ({
          queued: 1,
          skipped: {already_done: 1, already_active: 0, paused: 0, unsupported: 0, invalid: 0},
          message: category ? '已将 1 份材料加入处理队列。跳过：已完成 1 份。' : '已将 1 份材料加入处理队列。',
          category,
        })};
      }
      return {ok: false, json: async () => ({detail: '未预期的请求 ' + url})};
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../../src/dd_agent/static/app.js'), 'utf8'), context);
  return {node, calls, text: () => textOf(node('documents')) + textOf(node('editor-panel')), folders: () => textOf(node('folder-list')), setFailSave(value) { failSave = value; }};
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
      if (String(url).includes('/material-catalog')) return {ok: true, json: async () => ({items: [{document_id: 'd1', filenames: ['章程.pdf']}], total_documents: 1, next_cursor: null, category_counts: {organizational_docs: 1}})};
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
async function waitFor(predicate) {
  let last = '';
  for (let i = 0; i < 20; i += 1) {
    await flush();
    last = predicate();
    if (last) return last;
  }
  return last;
}

(async () => {
  const html = fs.readFileSync(path.join(__dirname, '../../../src/dd_agent/static/index.html'), 'utf8');
  assert.ok(!html.includes('id="subject-panel"') && !html.includes('项目主体'), '材料页不再把项目主体做成主面板');
  assert.ok(!html.includes('id="batch-panel"') && !html.includes('批量设置'), '材料页不再提供批量设置');
  assert.ok(!html.includes('>选择</th>'), '列表不再使用仅服务于批量设置的勾选列');
  assert.ok(html.includes('新建主体') && html.includes('id="editor-panel"'), '可在材料信息编辑器中新建主体');
  assert.ok(html.includes('id="process-folder"') && html.includes('处理本目录'), '材料页提供处理本目录');
  assert.ok(html.includes('id="process-all"') && html.includes('处理全部已登记材料'), '材料页提供处理全部已登记材料');

  const page = workspaceHarness();
  const folders = await waitFor(() => page.folders().includes('公司的组织性文件') && page.folders());
  assert.ok(!folders.includes('未分类') && folders.includes('其他') && folders.includes('公司的设立、变更'), '左侧列出固定目录且不含未分类');
  assert.ok(folders.includes('附件') && folders.includes('其他') && folders.includes('子公司工商档案'), '目录包含附件和其他');
  const shownUnfiled = await waitFor(() => textOf(page.node('documents')).includes('空白.pdf') && textOf(page.node('documents')));
  assert.ok(shownUnfiled.includes('其他'), '未填目录显示其他');
  assert.ok(shownUnfiled.includes('排队中'), '列表显示紧凑的处理状态');
  assert.ok(!shownUnfiled.includes('未填写') && !shownUnfiled.includes('未指定'), '未填主体或日期不在列表里占一列');
  assert.ok(!shownUnfiled.includes('章程.pdf'), '默认只显示当前目录的材料');

  await page.node('process-folder').onclick();
  const folderProcess = page.calls.filter(call => call.method === 'POST' && /\/processing$/.test(String(call.url))).pop();
  assert.strictEqual(folderProcess.body.category, 'other', '处理本目录会带上当前目录');
  assert.ok(page.node('process-status').textContent.includes('加入处理队列'), '入队后显示短状态');

  page.node('files').files = [{name: '新件.pdf', size: 10}];
  await page.node('upload-form').onsubmit({preventDefault() {}});
  const uploaded = page.calls.filter(call => call.method === 'POST' && String(call.url).endsWith('/documents')).pop();
  assert.strictEqual(uploaded.body.category, 'other', '默认在其他目录下导入时登记为 other');

  let orgFolder = null;
  walk(page.node('folder-list'), el => { if (el.className === 'folder' && textOf(el).includes('公司的组织性文件')) orgFolder = el; });
  assert.ok(orgFolder, '可以点击公司的组织性文件目录');
  orgFolder.onclick();
  const shownOrg = await waitFor(() => page.text().includes('章程.pdf') && page.text());
  assert.ok(shownOrg.includes('公司的组织性文件') && shownOrg.includes('甲公司') && shownOrg.includes('2023-06-01'), '已填元数据作为次要信息出现在对应目录的材料列表');
  assert.ok(shownOrg.includes('已读取'), '已完成处理且已有阅读结果时显示已读取');
  assert.ok(!shownOrg.includes('空白.pdf'), '切换目录后只显示该目录材料');

  await page.node('process-all').onclick();
  const allProcess = page.calls.filter(call => call.method === 'POST' && /\/processing$/.test(String(call.url))).pop();
  assert.ok(!allProcess.body.category, '处理全部已登记材料不限定目录');
  assert.ok(!shownOrg.includes('删除'), '列表不提供删除');
  assert.ok(page.folders().includes('1'), '目录显示材料数量');

  let mover = null;
  walk(page.node('documents'), el => { if (el.className === 'move-folder') mover = el; });
  assert.ok(mover, '每份材料可以移动到其他目录');
  mover.value = 'finance';
  await mover.onchange();
  const moved = page.calls.filter(call => call.method === 'POST' && String(call.url).endsWith('/documents/metadata')).pop();
  assert.deepStrictEqual(moved.body, {document_ids: ['a1'], category: {set: true, value: 'finance'}});

  const editorOptions = page.node('editor-category').children;
  const attachmentOption = editorOptions.find(el => el.value === 'attachments');
  assert.ok(attachmentOption, '材料信息的目录下拉包含附件');
  assert.strictEqual(attachmentOption.textContent, '附件');
  assert.ok(!editorOptions.some(el => el.value === 'interview_record'), '访谈记录不再作为独立目录');
  assert.ok(!editorOptions.some(el => el.value === 'uncategorized'), '未分类不再作为独立目录');

  let button = null;
  walk(page.node('documents'), el => { if (el.textContent === '材料信息') button = el; });
  assert.ok(button, '每份材料可以打开材料信息');
  button.onclick();
  const subjectBlank = page.node('editor-subject').children.find(el => el.value === '');
  assert.ok(subjectBlank && subjectBlank.textContent === '未指定', '所属主体可选，允许未指定');
  assert.strictEqual(page.node('editor-subject').value, 's1');
  assert.strictEqual(page.node('subject-rename-open').hidden, false, '已指定主体时可在编辑器中改名');
  assert.ok(page.node('create-subject') && page.node('subject-name'), '编辑器内可以新建主体');
  page.node('editor-category').value = 'litigation_admin';
  page.node('editor-date-kind').value = 'point';
  page.node('editor-point').value = '';
  await page.node('editor-save').onclick();
  assert.strictEqual(page.node('editor-category').value, 'litigation_admin', '保存失败时不清空已填目录');
  assert.ok(page.node('editor-error').textContent.includes('时点'), '保存失败指出字段');
  page.setFailSave(false);
  await page.node('editor-save').onclick();
  const saved = page.calls.filter(call => call.method === 'PATCH' && call.url.endsWith('/metadata')).pop();
  assert.strictEqual(saved.body.category, 'litigation_admin');
  assert.strictEqual(saved.body.subject_id, 's1');
  button.onclick();
  page.node('editor-category').value = 'attachments';
  await page.node('editor-save').onclick();
  const attachmentSaved = page.calls.filter(call => call.method === 'PATCH' && call.url.endsWith('/metadata')).pop();
  assert.strictEqual(attachmentSaved.body.category, 'attachments', '附件目录可以提交保存');

  button.onclick();
  page.node('subject-name').value = '乙公司';
  page.node('subject-role').value = 'subsidiary';
  await page.node('subject-form').onsubmit({preventDefault() {}});
  const created = page.calls.filter(call => call.method === 'POST' && String(call.url).endsWith('/subjects')).pop();
  assert.strictEqual(created.body.name, '乙公司');
  assert.strictEqual(created.body.role, 'subsidiary');
  assert.strictEqual(page.node('editor-subject').value, 's2', '新建主体后可选中该主体');
  assert.ok(page.node('editor-subject').children.some(el => el.value === 's2'), '新建的主体进入下拉');

  const catalogCalls = page.calls.filter(call => String(call.url).includes('/material-catalog'));
  assert.ok(catalogCalls.some(call => String(call.url).includes('categories=other')), '默认读取其他目录时带上过滤');
  assert.ok(catalogCalls.some(call => String(call.url).includes('categories=organizational_docs')), '读取组织性文件目录时带上过滤');
  assert.ok(!page.calls.some(call => call.method === 'POST' && String(call.url).endsWith('/documents/metadata') && call.body && Object.prototype.hasOwnProperty.call(call.body, 'subject')), '不再从界面提交批量主体修改');

  const blank = searchHarness('');
  await flush();
  await blank.node('search-form').onsubmit({preventDefault() {}});
  assert.ok(!blank.calls.some(call => String(call.url).includes('/search')), '没有当前项目时不发起检索');

  const chosen = searchHarness('?project=p1');
  await flush();
  chosen.node('search-project').value = 'p1';
  chosen.node('search-filters').open = true;
  await chosen.node('search-filters').ontoggle();
  let attachmentFilter = null;
  walk(chosen.node('filter-categories'), el => { if (el.value === 'attachments') attachmentFilter = el; });
  assert.ok(attachmentFilter, '检索目录过滤包含附件');
  assert.ok(textOf(chosen.node('filter-categories')).includes('附件'));
  assert.ok(textOf(chosen.node('filter-categories')).includes('公司的组织性文件'));
  walk(chosen.node('filter-categories'), el => { if (el.value === 'organizational_docs') el.checked = true; });
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
  assert.deepStrictEqual(posted.body.categories, ['organizational_docs']);
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
