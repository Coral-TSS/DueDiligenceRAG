const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element {
 constructor(tag=''){this.tagName=tag;this.value='';this.disabled=false;this.hidden=false;this.textContent='';this.children=[];this.open=false;}
 append(...values){this.children.push(...values);} replaceChildren(...values){this.children=values;}
 set innerHTML(_){throw Error('Evidence must be inserted as text');}
}
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
node('search-project').value='project-1';node('query').value='资本';
const parent={index_kind:'parent_child',document_id:'doc',filenames:['工商档案.pdf'],rerank_score:.9,
 matched_children:[{text:'注册资本2000万元。'}],
 context:{text:'UNVERIFIED OLD CONTEXT',source_spans:[{page_number:1,source_url:'/reader?page=1'},{page_number:2,source_url:'/reader?page=2'}]},
 reference:{url:'/api/parent',params:{index_version:'v'}}};
const page={text:'页级片段',filenames:['其他.pdf'],source_url:'/reader?page=3',page_number:3,method:'text',rerank_score:.8};
let resolveRead,readSignal;
const context={document:{getElementById:node,createElement:tag=>new Element(tag)},location:{search:''},
 URLSearchParams,AbortController,console,addEventListener(){},fetch:async(url,options)=>{
  if(url==='/api/projects')return {ok:true,json:async()=>[{id:'project-1',name:'项目'}]};
  if(options?.method==='POST')return {ok:true,json:async()=>({selection_changed:true,parent_selection_limited:true,
   coverage:{indexed_pages:3,total_pages:3,unread_documents:0,complete:false},status:'candidates',results:[parent,page]})};
  assert(url.startsWith('/api/parent?index_version=v'));
  readSignal=options.signal;return new Promise(resolve=>{resolveRead=resolve;});
 }};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../../src/dd_agent/static/search.js'),'utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const descendants=e=>[e,...e.children.flatMap(descendants)];
const text=e=>descendants(e).map(n=>n.textContent).join(' ');
(async()=>{
 await flush();await node('search-form').onsubmit({preventDefault(){}});
 assert.strictEqual(node('search-results').children.length,2);
 assert(text(node('search-results')).includes('工商档案.pdf'));
 assert(text(node('search-results')).includes('直接命中'));
 assert(text(node('search-results')).includes('页级片段'));
 assert(!text(node('search-results')).includes('UNVERIFIED OLD CONTEXT'));
 assert(node('search-state').textContent.includes('变化'));
 assert(node('search-state').textContent.includes('跨页候选较多'));
 let detail=descendants(node('search-results')).find(n=>n.tagName==='details');
 assert(detail,'Parent context has a keyboard-accessible details control');
 detail.open=true;let reading=detail.ontoggle();await flush();
 resolveRead({ok:true,json:async()=>({text:'已核对的连续原文',next_offset:null})});await reading;
 assert(text(detail).includes('已核对的连续原文'));
 await node('search-form').onsubmit({preventDefault(){}});
 detail=descendants(node('search-results')).find(n=>n.tagName==='details');detail.open=true;
 reading=detail.ontoggle();await flush();
 resolveRead({ok:false,json:async()=>({detail:'引用已失效，请重新查询。'})});await reading;
 assert(text(detail).includes('引用已失效'));
 await node('search-form').onsubmit({preventDefault(){}});
 detail=descendants(node('search-results')).find(n=>n.tagName==='details');detail.open=true;
 reading=detail.ontoggle();await flush();node('search-project').onchange();
 assert(readSignal.aborted,'Project switch aborts old context read');
 resolveRead({ok:true,json:async()=>({text:'LATE OLD TEXT',next_offset:null})});await reading;
 assert.strictEqual(node('search-results').children.length,0);
 assert(!text(detail).includes('LATE OLD TEXT'));
})().catch(e=>{console.error(e);process.exitCode=1;});
