// Review-only probes: execute the actual reader script against controlled DOM/network stubs.
// These assertions capture observed defects; they are not acceptance tests for desired behavior.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../../src/dd_agent/static/reader.js'), 'utf8')
  .replace(/init\(\);\s*$/, '');
function context() {
  const elements = new Map();
  function element() {
    return {textContent:'', value:'', hidden:false, children:[], attributes:{},
      append(x){this.children.push(x)}, replaceChildren(){this.children=[]},
      getAttribute(k){return this.attributes[k] ?? null},
      setAttribute(k,v){this.attributes[k]=v}, removeAttribute(k){delete this.attributes[k]}};
  }
  const document = {hidden:false, addEventListener(){}, createElement:element,
    getElementById(id){if(!elements.has(id)) elements.set(id,element()); return elements.get(id)}};
  const ctx = vm.createContext({document, URLSearchParams, AbortController,
    location:{search:'?project=p&document=d'}, setTimeout(){return 1}, clearTimeout(){},
    processingView(){return {pending:false}}, console});
  vm.runInContext(source, ctx);
  return ctx;
}
const results = {};
(async () => {
  const c = context();
  results.content_only_update = await vm.runInContext(`(async()=>{
    state.page=1; state.pageCount=1; state.polling=true;
    state.statusRevision='same-status'; state.contentRevision='old-content';
    state.catalog.set(1,{page_number:1,status:'succeeded',source_revision:'old-source'});
    state.pageCache={page:1,revision:'old-source',data:{page_number:1,text:'OLD',status:'succeeded',has_image:false}};
    let bodyRequests=0;
    api=async path=>{
      if(path.endsWith('/processing'))return null;
      if(path.endsWith('/reading'))return {};
      if(path.includes('page-catalog'))return {status_revision:'same-status',content_revision:'new-content'};
      bodyRequests++; return {text:'NEW'};
    };
    loadExtras=async()=>{};
    await poll();
    return {bodyRequests,displayed:$('effective-text').textContent,acknowledged:state.contentRevision};
  })()`, c);
  assert.equal(results.content_only_update.bodyRequests, 0);
  assert.equal(results.content_only_update.displayed, 'OLD');
  assert.equal(results.content_only_update.acknowledged, 'new-content');

  const d = context();
  results.raw_panel = await vm.runInContext(`(async()=>{
    let urls=[];
    api=async path=>{urls.push(path);return {page_number:1,text:'ENHANCED',status:'succeeded',source_revision:'s',has_image:false}};
    loadExtras=async()=>{};
    await showPage(1);
    return {urls,rawPanel:$('page-text').textContent,effectivePanel:$('effective-text').textContent};
  })()`, d);
  assert(results.raw_panel.urls[0].includes('view=effective'));
  assert.equal(results.raw_panel.rawPanel, 'ENHANCED');

  const e = context();
  results.failed_navigation = await vm.runInContext(`(async()=>{
    renderPage({page_number:1,text:'PAGE ONE',status:'succeeded',has_image:true,image_url:'/page-one',source_revision:'s'});
    $('page-image').hidden=false;
    api=async()=>{let err=new Error('Not processed');err.code='not_processed';throw err};
    loadExtras=async()=>{};
    await showPage(2);
    return {selected:state.page,image:$('page-image').src,imageHidden:$('page-image').hidden,label:$('page-state').textContent};
  })()`, e);
  assert.equal(results.failed_navigation.selected, 2);
  assert.equal(results.failed_navigation.image, '/page-one?rev=s');
  assert.equal(results.failed_navigation.imageHidden, false);

  const f = context();
  results.concurrent_poll = await vm.runInContext(`(async()=>{
    state.polling=true;
    let active=0,peak=0,releases=[];
    api=path=>{
      if(path.endsWith('/processing')){
        active++;peak=Math.max(peak,active);
        return new Promise(resolve=>releases.push(()=>{active--;resolve(null)}));
      }
      return Promise.resolve({status_revision:'',content_revision:''});
    };
    const one=poll(),two=poll();
    releases.splice(0).forEach(release=>release());
    await Promise.all([one,two]);
    return {peak};
  })()`, f);
  assert.equal(results.concurrent_poll.peak, 2);
  console.log(JSON.stringify(results, null, 2));
})().catch(error => {console.error(error); process.exitCode=1});
