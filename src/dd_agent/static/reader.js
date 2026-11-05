"use strict";
// 目录不含正文；当前文字、原始 OCR、增强摘要和历史分别按需读取。
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const project = params.get("project"), documentId = params.get("document");
const base = `/api/projects/${project}/documents/${documentId}`;
const CATALOG_BATCH = 100, POLL_ACTIVE = 3000, POLL_HIDDEN = 15000;
const outcomes = {base:"基础识别", enhanced:"已视觉增强", checked:"OCR 与视觉识别一致", uncertain:"存在无法可靠识别的信息"};
const state = {
  catalog:new Map(), cursor:null, loaded:false, pageCount:0, readingVersion:"",
  contentRevision:"", statusRevision:"", catalogGeneration:0,
  page:null, pageCache:null, rawCache:null, summaryCache:null, history:null,
  generation:0, abort:null, jobStamp:"", timer:null, polling:false,
  pollPromise:null, pollAgain:false,
};
async function api(path, options={}) {
  const response = await fetch(path, options);
  let data={}; try { data=await response.json(); } catch {}
  if (!response.ok) {
    const error=new Error(typeof data.detail==="string" ? data.detail : "操作失败，请重试。");
    error.code=data.code||""; throw error;
  }
  return data;
}
function paragraph(parent,text,tag="p") { const el=document.createElement(tag); el.textContent=text; parent.append(el); return el; }
function error(text) { $("reading-error").textContent=text||""; }
function current(number,token) { return token===state.generation && number===state.page; }
function pageOptions() { return {signal:state.abort?.signal}; }
function renderCatalog() {
  const numbers=[...state.catalog.keys()].sort((a,b)=>a-b), select=$("page-select");
  select.replaceChildren();
  for (const number of numbers) {
    const option=document.createElement("option"); option.value=String(number);
    option.textContent=`第 ${number} 页${state.catalog.get(number)?.status==="failed" ? "（失败）" : ""}`;
    select.append(option);
  }
  if (numbers.includes(state.page)) select.value=String(state.page);
  select.disabled=!numbers.length; $("page-more").hidden=state.cursor===null;
  $("catalog-state").textContent=numbers.length ? `已加载 ${numbers.length} 页 / 共 ${state.pageCount} 页${state.cursor===null ? "" : "，还有更多"}。` : "尚未加载已处理页。";
}
async function loadCatalog({reset=false,from=null}={}) {
  if (!reset && state.loaded && state.cursor===null) return;
  const token=++state.catalogGeneration, cursor=reset ? (from??0) : (state.cursor??0);
  const query=new URLSearchParams({limit:String(CATALOG_BATCH),after:String(cursor)});
  const data=await api(`${base}/page-catalog?${query}`);
  // 只有最后一次目录请求可提交；在途旧响应不能覆盖用户跳转后的窗口。
  if (token!==state.catalogGeneration) return false;
  if (reset) state.catalog.clear();
  for (const item of data.items) state.catalog.set(item.page_number,item);
  state.pageCount=data.page_count; state.readingVersion=data.reading_version;
  state.contentRevision=data.content_revision; state.statusRevision=data.status_revision;
  state.cursor=data.next_cursor; state.loaded=true; renderCatalog(); return true;
}
function clearPage(number, message="正在读取当前页…") {
  const image=$("page-image"); image.onload=null; image.onerror=null;
  image.hidden=true; image.removeAttribute("src"); $("image-empty").hidden=false;
  $("effective-text").textContent=message; $("page-text").textContent="展开后读取基础识别文字。";
  $("page-state").textContent=`第 ${number} 页 · ${message}`;
  $("route-state").textContent=""; $("line-results").replaceChildren();
  $("machine-regions").replaceChildren(); $("history-results").replaceChildren();
  $("history-counts").textContent="";
  $("history-next").hidden=true;
}
function renderPage(data) {
  $("effective-text").textContent=data.text||"该页暂无可用文字。";
  $("page-state").textContent=`第 ${data.page_number} 页 · ${data.method==="ocr" ? "基础 OCR" : "原生文字"} · ${data.status==="succeeded" ? "已读取" : "读取失败"}`;
  if (data.next_offset!==null && data.next_offset!==undefined) $("effective-text").textContent+=`\n（正文较长，当前展示前 ${data.text.length} 字。）`;
  const image=$("page-image"), token=state.generation;
  if (data.has_image) {
    const src=`${data.image_url}?rev=${encodeURIComponent(data.raw_revision||data.source_revision||"")}`;
    image.onload=()=>{if(current(data.page_number,token)){image.hidden=false;$("image-empty").hidden=true;}};
    image.onerror=()=>{if(current(data.page_number,token)){image.hidden=true;$("image-empty").hidden=false;}};
    if (image.getAttribute("src")!==src) image.src=src;
    if (image.complete && image.naturalWidth>0) image.onload();
  } else { image.hidden=true; image.removeAttribute("src"); $("image-empty").hidden=false; }
}
async function fetchPage(number,token) {
  try {
    const data=await api(`${base}/pages/${number}?view=effective`,pageOptions());
    if (!current(number,token)) return false;
    state.pageCache={page:number,revision:data.source_revision,data};
    const old=state.catalog.get(number)||{};
    state.catalog.set(number,{...old,page_number:number,status:data.status,method:data.method,source_revision:data.source_revision});
    renderCatalog(); renderPage(data); return true;
  } catch (caught) {
    if (caught.name==="AbortError" || !current(number,token)) return false;
    state.pageCache=null; clearPage(number,caught.code==="not_processed" ? "该页尚未处理。" : "该页暂时无法读取。");
    error(caught.message); return false;
  }
}
async function showPage(number) {
  number=Number(number);
  const token=++state.generation;
  state.abort?.abort(); state.abort=new AbortController(); state.page=number;
  state.history=null; error(""); clearPage(number);
  const item=state.catalog.get(number);
  if (state.pageCache?.page===number && item && state.pageCache.revision===item.source_revision) renderPage(state.pageCache.data);
  else if (!await fetchPage(number,token)) return;
  if (!current(number,token)) return;
  await Promise.all([loadSummary(number,token),
    $("raw-details").open ? loadRaw(number,token) : null,
    $("history-details").open ? loadHistory(number,token) : null]);
}
async function loadSummary(number,token) {
  const revision=state.pageCache?.data.source_revision;
  try {
    let data=state.summaryCache?.page===number && state.summaryCache.revision===revision ? state.summaryCache.data : null;
    if (!data) data=await api(`${base}/pages/${number}/summary?source_revision=${encodeURIComponent(revision)}`,pageOptions());
    if (!current(number,token)) return;
    if (data.source_revision!==revision) throw new Error("该页来源已变化，请刷新结果。");
    state.summaryCache={page:number,revision,data};
    $("machine-regions").replaceChildren();
    $("route-state").textContent="尚无当前版本的视觉增强结果。";
    if (data.current) {
      const result=data.current;
      $("route-state").textContent=`${outcomes[result.outcome]||result.outcome} · ${result.route?.reason||"自动处理"}。识别结果尚不等于已核实事实。`;
      for (const region of result.regions||[]) {
        const line=paragraph($("machine-regions"),`${region.decision==="unknown" ? "未知" : region.decision==="enhanced" ? "已视觉增强" : "一致"}：${region.reason}`,"div");
        line.className="machine-region"; paragraph(line,region.text,"pre");
      }
    }
    $("history-counts").textContent=`保留视觉候选 ${data.candidate_count||0} 条、历史人工记录 ${data.adoption_count||0} 条；下方按批查看视觉增强历史。`;
  } catch (caught) { if (caught.name!=="AbortError" && current(number,token)) error(caught.message); }
}
async function loadRaw(number=state.page,token=state.generation) {
  if (!number || state.pageCache?.page!==number) return;
  const revision=state.pageCache.data.raw_revision;
  try {
    let data=state.rawCache?.page===number && state.rawCache.revision===revision ? state.rawCache.data : null;
    if (!data) data=await api(`${base}/pages/${number}?view=raw&include_lines=true&source_revision=${encodeURIComponent(revision)}`,pageOptions());
    if (!current(number,token)) return;
    state.rawCache={page:number,revision,data}; $("page-text").textContent=data.text||"该页基础识别文字为空。";
    if (data.next_offset!==null && data.next_offset!==undefined) $("page-text").textContent+=`\n（正文较长，当前展示前 ${data.text.length} 字。）`;
    $("line-results").replaceChildren();
    for (const line of data.lines||[]) paragraph($("line-results"),`${line.text} · 识别分数 ${Number(line.score).toFixed(3)}`,"div");
  } catch (caught) { if (caught.name!=="AbortError" && current(number,token)) error(caught.message); }
}
async function loadHistory(number=state.page,token=state.generation,after=null) {
  if (!number || state.pageCache?.page!==number) return;
  const query=new URLSearchParams({limit:"10"}); if (after) query.set("after",after);
  $("history-next").disabled=true;
  try {
    const data=await api(`${base}/pages/${number}/history?${query}`,pageOptions());
    if (!current(number,token)) return;
    if (data.source_revision!==state.pageCache.data.source_revision) throw new Error("历史对应的页面版本已变化，请刷新结果。");
    state.history={page:number,cursor:data.next_cursor};
    $("history-results").replaceChildren();
    if (!data.items.length) paragraph($("history-results"),"暂无视觉增强历史。");
    for (const record of data.items) {
      paragraph($("history-results"),`${new Date(record.created_at).toLocaleString()} · ${record.is_current ? "当前生效" : "历史版本"} · ${record.verification||"未核实"}`);
      paragraph($("history-results"),record.text_preview+(record.preview_truncated ? "…（预览）" : ""),"pre");
    }
    $("history-next").hidden=!data.next_cursor;
  } catch (caught) { if (caught.name!=="AbortError" && current(number,token)) error(caught.message); }
  finally { if (current(number,token)) $("history-next").disabled=false; }
}
async function refreshReading() {
  const reading=await api(`${base}/reading`);
  $("reading-title").textContent=(reading.filenames||[]).join("、")||"材料";
  $("reading-progress").textContent=`基础阅读已保存 ${reading.successful_pages}/${reading.page_count||"待检测"} 页，失败 ${reading.failed_pages} 页。`;
}
async function probeRevisions() {
  return api(`${base}/page-catalog?${new URLSearchParams({limit:"1",after:String(state.pageCount||0)})}`);
}
async function refreshSelected() {
  const number=state.page,token=state.generation;
  if (!number) return;
  // 全卷变更不等于当前页变更：只读一项元数据，再决定是否重取当前正文。
  const data=await api(`${base}/page-catalog?${new URLSearchParams({limit:"1",after:String(number-1)})}`);
  if (!current(number,token)) return;
  const item=data.items.find(item=>item.page_number===number);
  if (item) state.catalog.set(number,item); else state.catalog.delete(number);
  renderCatalog();
  if (!item || state.pageCache?.page!==number || state.pageCache.revision!==item.source_revision) await showPage(number);
}
function schedule(delay) { clearTimeout(state.timer); state.timer=setTimeout(poll,delay); }
async function pollOnce() {
  try {
    const job=await api(`${base}/processing`),view=processingView(job);
    renderStageControls(job);
    $("start-processing").hidden=view.startHidden; $("start-processing").disabled=!!view.active||controlling;
    $("pause-processing").hidden=!view.active; $("pause-processing").disabled=controlling||job?.status==="pause_requested";
    $("resume-processing").hidden=!view.resumeAvailable; $("resume-processing").disabled=controlling;
    $("job-state").textContent=view.text;
    const stamp=JSON.stringify(job);
    if (stamp!==state.jobStamp) { state.jobStamp=stamp; await refreshReading(); }
    const revisions=await probeRevisions();
    const changed=revisions.content_revision!==state.contentRevision || revisions.status_revision!==state.statusRevision || revisions.reading_version!==state.readingVersion;
    if (revisions.status_revision!==state.statusRevision || revisions.reading_version!==state.readingVersion) {
      const lowest=state.catalog.size ? Math.min(...state.catalog.keys()) : 1;
      const committed=await loadCatalog({reset:true,from:Math.max(0,lowest-1)});
      if (committed===false) return view.pending ? POLL_ACTIVE : null;
    }
    if (changed) await refreshSelected();
    state.contentRevision=revisions.content_revision; state.statusRevision=revisions.status_revision;
    state.readingVersion=revisions.reading_version;
    return view.pending ? (document.hidden ? POLL_HIDDEN : POLL_ACTIVE) : null;
  } catch (caught) { error(caught.message); return document.hidden ? POLL_HIDDEN : POLL_ACTIVE; }
}
function poll() {
  clearTimeout(state.timer);
  if (!state.polling) return Promise.resolve();
  // 合并刷新、可见性和控制操作的触发；计时器不等于在途请求锁。
  if (state.pollPromise) { state.pollAgain=true; return state.pollPromise; }
  state.pollPromise=(async()=>{
    let delay;
    do { state.pollAgain=false; delay=await pollOnce(); } while (state.pollAgain && state.polling);
    if (delay!==null && state.polling) schedule(delay);
  })().finally(()=>{state.pollPromise=null;});
  return state.pollPromise;
}
function renderStageControls(job) {
  const container=$("stage-controls");container.replaceChildren();
  container.hidden=!job||!["queued","running","paused","pause_requested"].includes(job.status);
  if(container.hidden)return;
  for(const [stage,label] of Object.entries({reading:"基础识别",vision:"视觉增强",embedding:"向量入库"})){
    const button=document.createElement("button"),paused=job.paused_stages?.includes(stage);
    button.className="secondary";button.textContent=`${paused ? "继续" : "暂停"}${label}`;
    button.disabled=controlling||job.status==="pause_requested";
    button.onclick=async()=>{
      controlling=true;error("");let failure="";
      try{await api(`${base}/processing/stages/${stage}/${paused ? "resume" : "pause"}`,{method:"POST"});}
      catch(caught){failure=caught.message;}
      finally{controlling=false;await poll();if(failure)error(failure);}
    };
    container.append(button);
  }
}
$("raw-details").ontoggle=()=>$("raw-details").open ? loadRaw() : undefined;
$("history-details").ontoggle=()=>$("history-details").open ? loadHistory() : undefined;
$("history-next").onclick=()=>loadHistory(state.page,state.generation,state.history?.cursor);
$("page-select").onchange=()=>showPage(Number($("page-select").value));
$("page-more").onclick=async()=>{ $("page-more").disabled=true; try{await loadCatalog();}catch(caught){error(caught.message);}finally{$("page-more").disabled=false;} };
$("page-jump-go").onclick=async()=>{
  const number=Number($("page-jump").value);
  if (!Number.isInteger(number)||number<1) {error("请输入正确的页码。");return;}
  if (state.pageCount && number>state.pageCount) {error(`该材料共 ${state.pageCount} 页。`);return;}
  await showPage(number);
};
$("refresh-reading").onclick=async()=>{error("");await refreshReading();await poll();};
$("start-processing").onclick=async()=>{
  $("start-processing").disabled=true;
  try {await api(`${base}/processing`,{method:"POST"});state.polling=true;await poll();}
  catch(caught){error(caught.message);$("start-processing").disabled=false;}
};
let controlling=false;
async function controlJob(action) {
  controlling=true;let failure="";error("");$("pause-processing").disabled=true;$("resume-processing").disabled=true;
  try{await api(`${base}/processing/${action}`,{method:"POST"});}catch(caught){failure=caught.message;}
  finally{controlling=false;await poll();if(failure)error(failure);}
}
$("pause-processing").onclick=()=>controlJob("pause");$("resume-processing").onclick=()=>controlJob("resume");
document.addEventListener("visibilitychange",()=>{if(!document.hidden && state.polling)poll();});
async function init() {
  if(!project||!documentId){error("缺少项目或材料编号，请从材料工作区打开。");return;}
  try{
    await refreshReading();const deep=Number(params.get("page")||0);
    await loadCatalog({reset:true,from:deep>0 ? Math.max(0,deep-1) : 0});
    const target=deep>0 ? deep : Number($("page-select").value||0)||(state.pageCount ? 1 : 0);
    if(target)await showPage(target);else $("effective-text").textContent="尚未处理页面，可以启动自动处理。";
    state.polling=true;await poll();
  }catch(caught){error(caught.message);state.polling=true;schedule(POLL_ACTIVE);}
}
init();
