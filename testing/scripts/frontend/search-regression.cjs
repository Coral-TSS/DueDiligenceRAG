const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element {
 constructor(){this.value='';this.disabled=false;this.hidden=false;this.textContent='';this.children=[];}
 append(...x){this.children.push(...x);} replaceChildren(...x){this.children=x;}
}
const nodes=new Map();
const node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
node('search-project').value='project-1';node('query').value='资本';
let pending,signal;
const context={document:{getElementById:node,createElement:()=>new Element()},location:{search:'?project=project-1'},
 URLSearchParams,AbortController,console,addEventListener(){},fetch:async(url,options)=>{
  if(!options)return {ok:true,json:async()=>[{id:'project-1',name:'项目'}]};
  signal=options.signal;
  return await new Promise(resolve=>{pending=resolve;});
 }};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../../src/dd_agent/static/search.js'),'utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await flush();
 const submission=node('search-form').onsubmit({preventDefault(){}});
 await flush();
 assert.strictEqual(node('cancel-search').hidden,false,'Cancel is visible during work');
 assert(signal,'fetch receives an abort signal');
 node('cancel-search').onclick();
 assert.strictEqual(signal.aborted,true);
 // Even if a transport ignores abort and returns late, it must not paint results.
 pending({ok:true,json:async()=>({coverage:{indexed_pages:1,total_pages:1,unread_documents:0,complete:true},status:'candidates',results:[{id:'late'}]})});
 await submission;
 assert.strictEqual(node('search-results').children.length,0);
 assert(node('search-state').textContent.includes('取消'));
 assert.strictEqual(node('submit-search').disabled,false);
 assert.strictEqual(node('cancel-search').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
