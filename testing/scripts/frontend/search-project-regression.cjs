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
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}

function load(search) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  node('search-project').value = '';
  const calls = [];
  let pending;
  const context = {
    document: {getElementById: node, createElement: () => new Element()},
    location: {search},
    URLSearchParams, AbortController, console,
    addEventListener() {},
    fetch: async (url, options) => {
      calls.push(String(url));
      if (!options) {
        return {ok: true, json: async () => [
          {id: 'new-b', name: '较新的乙'},
          {id: 'old-a', name: '较早的甲'},
        ]};
      }
      return await new Promise(resolve => { pending = {resolve, url, options}; });
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../../src/dd_agent/static/search.js'), 'utf8'), context);
  return {node, calls, context, takePending: () => pending};
}

(async () => {
  const blank = load('');
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(blank.node('search-project').value, '', '没有地址项目时不能选中最新项目');
  assert.strictEqual(blank.node('submit-search').disabled, true);
  await blank.node('search-form').onsubmit({preventDefault() {}});
  assert.strictEqual(blank.calls.some(url => url.includes('/search')), false, '没有当前项目时不能发起检索');

  const chosen = load('?project=old-a');
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(chosen.node('search-project').value, 'old-a');
  const submission = chosen.node('search-form').onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(chosen.node('search-project').disabled, false, '检索进行中仍可切换项目');
  chosen.node('search-project').value = 'new-b';
  chosen.node('search-project').onchange();
  const late = chosen.takePending();
  late.resolve({ok: true, json: async () => ({
    coverage: {indexed_pages: 1, total_pages: 1, unread_documents: 0, complete: true},
    status: 'candidates',
    results: [{index_kind: 'page', filenames: ['只属于甲.pdf'], page_number: 1, text: '只属于甲的正文', method: 'text', verification: 'unverified', rerank_score: 0.9, source_url: '/reader?project=old-a'}],
  })});
  await submission;
  assert.strictEqual(chosen.node('search-results').textContent.includes('只属于甲'), false);
})().catch(error => { console.error(error); process.exitCode = 1; });
