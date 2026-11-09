"use strict";
// 检索页只展示本地召回与重排后的原文候选，不执行生成回答或法律分析。
// 片段、相关分数、机器文字状态与来源链接分别展示；相关性不能替代事实核实。
const node = id => document.getElementById(id);
function recallSuffix(hit) {
  const recall = hit && hit.recall;
  if (!recall || !Array.isArray(recall.channels) || !recall.channels.length) return "";
  const label = {vector: "向量", keyword: "关键词"};
  const parts = recall.channels.map(channel => {
    const score = channel === "vector" ? recall.vector_score : recall.keyword_score;
    const shown = typeof score === "number" ? score.toFixed(3) : "—";
    return `${label[channel] || channel} ${shown}`;
  });
  const fusion = typeof recall.fusion_score === "number" ? ` · 融合 ${recall.fusion_score.toFixed(3)}` : "";
  return ` · 召回 ${parts.join("、")}${fusion}`;
}
let activeSearch = null;
let resultGeneration = 0;
const contextReads = new Set();
function clearResults() {
  resultGeneration++;
  for (const controller of contextReads) controller.abort();
  contextReads.clear(); node("search-results").replaceChildren();
}
node("cancel-search").onclick = () => {
  if (activeSearch) {
    activeSearch.abort(); node("cancel-search").disabled = true;
    node("search-state").textContent = "已取消查询。";
  }
};
addEventListener("pagehide", () => { activeSearch?.abort(); clearResults(); });
async function request(path, options) {
  const response = await fetch(path, options), data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "查询失败，请检查输入或连接后重试。");
  return data;
}
async function init() {
  try {
    const projects = await request("/api/projects");
    const select = node("search-project");
    select.replaceChildren();
    const blank = document.createElement("option"); blank.value = ""; blank.textContent = "请选择项目";
    select.append(blank);
    for (const p of projects) {
      const option = document.createElement("option"); option.value = p.id; option.textContent = p.name;
      select.append(option);
    }
    const selected = new URLSearchParams(location.search).get("project");
    select.value = projects.some(p => p.id === selected) ? selected : "";
    node("submit-search").disabled = !select.value;
    node("search-state").textContent = select.value ? "输入问题后查询；CPU 模型加载和排序可能需要数十秒。" : (projects.length ? "请先选择项目。未选择时不会搜索。" : "尚无项目，请先返回工作区创建并导入材料。");
  } catch (error) { node("search-error").textContent = error.message; node("search-state").textContent = "项目暂时无法读取，请刷新页面重试。"; }
}
function parentResult(hit) {
  const article = document.createElement("article"); article.className = "search-hit";
  const title = document.createElement("p");
  const pages = [...new Set(hit.context.source_spans.map(s => s.page_number))];
  title.textContent = `${hit.filenames?.[0] || "材料"} · PDF 第 ${pages.join("、")} 页`;
  const label = document.createElement("p"); label.textContent = "直接命中片段";
  article.append(title, label);
  for (const child of hit.matched_children) {
    const text = document.createElement("p"); text.className = "evidence-text"; text.textContent = child.text;
    article.append(text);
  }
  const links = document.createElement("p");
  for (const page of pages) {
    const source = hit.context.source_spans.find(s => s.page_number === page);
    const link = document.createElement("a"); link.href = source.source_url;
    link.textContent = `查看第 ${page} 页原件 `; links.append(link);
  }
  const meta = document.createElement("p"); meta.className = "muted";
  meta.textContent = `跨页原文候选 · 事实未核实 · 相关分数 ${hit.rerank_score.toFixed(3)}${recallSuffix(hit)}`;
  const details = document.createElement("details"), summary = document.createElement("summary");
  summary.textContent = "展开连续上下文（含补充原文）";
  const body = document.createElement("p"); body.className = "evidence-text";
  body.textContent = "展开后核对引用版本并读取原文。"; details.append(summary, body);
  const generation = resultGeneration; let loaded = false, reading = null;
  details.ontoggle = async () => {
    if (!details.open) { reading?.abort(); reading = null; return; }
    if (loaded || reading) return;
    const controller = new AbortController(); reading = controller; contextReads.add(controller);
    body.textContent = "正在核对原文版本…"; body.className = "evidence-text";
    try {
      const data = await request(`${hit.reference.url}?${new URLSearchParams(hit.reference.params)}`, {signal: controller.signal});
      if (controller.signal.aborted || generation !== resultGeneration || reading !== controller) return;
      body.textContent = data.text; loaded = data.next_offset === null;
      if (!loaded) body.textContent += "\n原文尚未读完，请重新检索后继续核对。";
    } catch (error) {
      if (controller.signal.aborted || generation !== resultGeneration || reading !== controller) return;
      body.className = "error"; body.textContent = error.message;
    } finally {
      contextReads.delete(controller); if (reading === controller) reading = null;
    }
  };
  article.append(links, meta, details); return article;
}
node("search-project").onchange = () => {
  if (activeSearch) activeSearch.abort();
  clearResults();
  node("submit-search").disabled = !node("search-project").value;
  node("search-error").textContent = "";
  node("search-state").textContent = node("search-project").value ? "已切换项目，请重新查询。" : "请先选择项目。未选择时不会搜索。";
};
node("search-form").onsubmit = async event => {
 // 查询期间锁定项目选择，避免旧项目的响应出现在新项目中；失败不沿用旧查询结果。
  event.preventDefault();
  const project = node("search-project").value;
  if (!project || node("submit-search").disabled) return;
  const controller = new AbortController(); activeSearch = controller;
  node("cancel-search").hidden = false; node("cancel-search").disabled = false;
  node("submit-search").disabled = true;
  node("submit-search").textContent = "查找中…";
  clearResults(); node("search-error").textContent = "";
  node("search-state").textContent = "正在本地查找并排序，请等待…";
  try {
    const data = await request(`/api/projects/${project}/search`, {method: "POST", signal: controller.signal, headers: {"Content-Type": "application/json"}, body: JSON.stringify({query: node("query").value})});
    if (controller.signal.aborted || activeSearch !== controller) return;
    const c = data.coverage;
    // 这是搜索附带的简要覆盖；独立的缺页／待重建／质量覆盖清单仍待 RG-02 实现。
    node("search-state").textContent = `当前可检索 ${c.indexed_pages} / 已知 ${c.total_pages} 页；${c.unread_documents} 份材料尚未预检。${data.selection_changed ? "查询期间材料或候选发生变化，本轮结果不完整，请重新查询。" : c.complete ? "已覆盖已登记材料。" : "仅覆盖部分材料。"} ${data.status === "not_indexed" ? "尚无当前版本索引，请先分批建立索引。" : !data.results.length ? "没有候选片段，请换个关键词或补充已读页面的索引。" : `找到 ${data.results.length} 条候选，请对照原件核实。`}`;
    if (data.parent_selection_limited) node("search-state").textContent += " 跨页候选较多，本轮最多检查 20 份，其余材料按页查询。";
    for (const hit of data.results) {
      if (hit.index_kind === "parent_child") { node("search-results").append(parentResult(hit)); continue; }
      const article = document.createElement("article"); article.className = "search-hit";
      const title = document.createElement("a"); title.href = hit.source_url || '#';
      title.textContent = hit.locator_label ? `${hit.filenames[0]} · ${hit.locator_label}` : `${hit.filenames[0]} · PDF 第 ${hit.page_number} 页`;
      const text = document.createElement("p"); text.className = "evidence-text"; text.textContent = hit.text;
      const meta = document.createElement("p"); meta.className = "muted"; meta.textContent = `${hit.method === "ocr" ? "OCR 来源" : "原生文字来源"} · ${({machine_enhanced:"已视觉增强，事实未核实",machine_checked:"OCR 与视觉识别一致，事实未核实",machine_uncertain:"含未知信息"})[hit.verification] || "未核实"} · 相关分数 ${hit.rerank_score.toFixed(3)}${recallSuffix(hit)}`;
      article.append(title, text, meta); node("search-results").append(article);
    }
  } catch (error) {
    if (controller.signal.aborted) node("search-state").textContent = "已取消查询。";
    else { node("search-error").textContent = error.message; node("search-state").textContent = "本次查询未完成，可稍后重试。"; }
  }
  finally {
    if (activeSearch === controller) {
      activeSearch = null; node("cancel-search").hidden = true;
      node("submit-search").disabled = !node("search-project").value; node("submit-search").textContent = "查找";
    }
  }
};
init();
