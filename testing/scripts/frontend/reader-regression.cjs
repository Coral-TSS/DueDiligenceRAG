const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict'), path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../../../src/dd_agent/static/reader.js'),'utf8').replace(/init\(\);\s*$/,'');
const elements=new Map();
function element(){return {textContent:'',value:'',hidden:false,open:false,children:[],attrs:{},
  append(x){this.children.push(x)},replaceChildren(){this.children=[];this.textContent=''},
  getAttribute(k){return this.attrs[k]??null},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k];if(k==='src')delete this.src}}}
const document={hidden:false,addEventListener(){},createElement:element,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)}};
const ctx=vm.createContext({document,URLSearchParams,AbortController,location:{search:'?project=p&document=d'},
  setTimeout(){return 1},clearTimeout(){},processingView(){return {pending:false}},console,assert});
vm.runInContext(source,ctx);
const cases={
content_update:`
  state.page=1;state.pageCount=1;state.polling=true;state.statusRevision='status';state.contentRevision='old';
  state.catalog.set(1,{page_number:1,status:'succeeded',source_revision:'old'});
  state.pageCache={page:1,revision:'old',data:{page_number:1,text:'OLD',source_revision:'old',raw_revision:'raw',status:'succeeded'}};
  let bodies=0;
  api=async url=>{
    if(url.endsWith('/processing'))return null;if(url.endsWith('/reading'))return {};
    if(url.includes('page-catalog'))return {status_revision:'status',content_revision:'new',items:[{page_number:1,status:'succeeded',source_revision:'new'}]};
    if(url.includes('/summary'))return {source_revision:'new',current:null,candidate_count:0,adoption_count:0};
    bodies++;return {page_number:1,text:'NEW',source_revision:'new',raw_revision:'raw',status:'succeeded'};
  };
  await poll();assert.equal($('effective-text').textContent,'NEW');assert.equal(bodies,1);
`,
other_page_update:`
  state.page=1;state.pageCount=2;state.polling=true;state.statusRevision='status';state.contentRevision='old-global';
  state.catalog.set(1,{page_number:1,status:'succeeded',source_revision:'same'});
  state.pageCache={page:1,revision:'same',data:{page_number:1,text:'SAME',source_revision:'same',status:'succeeded'}};
  $('effective-text').textContent='SAME';let bodies=0;
  api=async url=>{if(url.endsWith('/processing'))return null;if(url.endsWith('/reading'))return {};
    if(url.includes('page-catalog'))return {status_revision:'status',content_revision:'new-global',items:[{page_number:1,status:'succeeded',source_revision:'same'}]};
    bodies++;throw new Error('Unchanged page body must not be requested')};
  await poll();assert.equal(bodies,0);assert.equal($('effective-text').textContent,'SAME');
`,
raw_text:`
  let urls=[];
  api=async url=>{urls.push(url);if(url.includes('/summary'))return {source_revision:'s',current:null};
    return {page_number:1,text:url.includes('view=raw')?'RAW OCR':'ENHANCED',source_revision:'s',raw_revision:'r',status:'succeeded',lines:[]}};
  await showPage(1);
  assert.notEqual($('page-text').textContent,'ENHANCED');
  assert(!urls.some(url=>url.includes('view=raw')),'Raw details should be lazy');
  $('raw-details').open=true;await $('raw-details').ontoggle();
  assert.equal($('page-text').textContent,'RAW OCR');assert.equal($('effective-text').textContent,'ENHANCED');
`,
failed_page:`
  renderPage({page_number:1,text:'PAGE ONE',status:'succeeded',has_image:true,image_url:'/one',source_revision:'s'});
  $('page-image').hidden=false;
  api=async()=>{let e=new Error('not processed');e.code='not_processed';throw e};
  await showPage(2);assert.equal(state.page,2);assert.equal($('page-image').hidden,true);
  assert(!$('page-image').src);assert(!$('page-state').textContent.includes('第 1 页'));
  assert.notEqual($('effective-text').textContent,'PAGE ONE');
`,
bounded_history:`
  let urls=[];
  api=async url=>{urls.push(url);if(url.includes('/automatic')||url.includes('/review'))throw new Error('Unbounded legacy endpoint');
    if(url.includes('/summary'))return {source_revision:'s',current:{outcome:'enhanced',route:{reason:'test'},regions:[]},candidate_count:30,adoption_count:0};
    if(url.includes('/history'))return {source_revision:'s',raw_revision:'r',items:[],next_cursor:null};
    return {page_number:1,text:'OK',source_revision:'s',raw_revision:'r',status:'succeeded'}};
  await showPage(1);assert(!urls.some(url=>url.includes('/history')),'History should be lazy');
  $('history-details').open=true;await $('history-details').ontoggle();
  assert(urls.some(url=>url.includes('/history')));assert(!urls.some(url=>url.includes('/automatic')||url.includes('/review')));
`,
serial_poll:`
  state.polling=true;let active=0,peak=0,release;
  api=url=>{if(url.endsWith('/processing')){active++;peak=Math.max(peak,active);
      if(!release)return new Promise(resolve=>release=()=>{active--;resolve(null)});
      active--;return Promise.resolve(null)}return Promise.resolve({status_revision:'',content_revision:''})};
  const one=poll(),two=poll();release();await Promise.all([one,two]);assert.equal(peak,1);
`,
late_page:`
  let resolveOld;
  api=url=>{if(url.includes('/summary'))return Promise.resolve({source_revision:'s2',current:null});
    if(url.includes('/pages/1?'))return new Promise(resolve=>resolveOld=resolve);
    return Promise.resolve({page_number:2,text:'TWO',source_revision:'s2',raw_revision:'r2',status:'succeeded'})};
  const old=showPage(1);await showPage(2);
  resolveOld({page_number:1,text:'LATE ONE',source_revision:'s1',status:'succeeded'});await old;
  assert.equal($('effective-text').textContent,'TWO');assert.equal(state.page,2);
`,
late_catalog:`
  let release;
  api=()=>new Promise(resolve=>release=resolve);
  const old=loadCatalog({reset:true,from:0});const firstRelease=release;
  const newer=loadCatalog({reset:true,from:599});
  release({items:[{page_number:600}],page_count:649,reading_version:'new',content_revision:'new',status_revision:'new',next_cursor:null});await newer;
  firstRelease({items:[{page_number:1}],page_count:649,reading_version:'old',content_revision:'old',status_revision:'old',next_cursor:1});await old;
  assert.equal(state.readingVersion,'new');assert(!state.catalog.has(1));assert(state.catalog.has(600));
`};
if(!cases[process.argv[2]])throw new Error('Unknown test case');
vm.runInContext(`(async()=>{${cases[process.argv[2]]}})()`,ctx).catch(error=>{console.error(error);process.exitCode=1});
