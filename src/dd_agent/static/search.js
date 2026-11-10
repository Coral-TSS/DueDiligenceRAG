"use strict";
// 检索页只展示本地召回与重排后的原文候选，不执行生成回答或法律分析。
// 片段、相关分数、机器文字状态与来源链接分别展示；相关性不能替代事实核实。
const node = id => document.getElementById(id);
const CATEGORIES = [
  ["uncategorized", "未分类"], ["business_license", "营业执照"], ["articles", "公司章程"],
  ["shareholder_resolution", "股东会决议"], ["board_resolution", "董事会决议"], ["equity_structure", "股权结构／名册"],
  ["audit_report", "审计报告"], ["financial_statement", "财务报表"], ["major_contract", "重大合同"],
  ["litigation", "诉讼／仲裁文书"], ["ip_certificate", "知识产权证书"], ["regulatory_filing", "监管报送／批复"],
  ["hr_labor", "劳动／人事材料"], ["other", "其他"],
];
function selectedBoxes(containerId) {
  const found = [];
  const walk = el => {
    if (!el) return;
    if (el.type === "checkbox" && el.checked && el.value) found.push(el.value);
    for (const child of el.children || []) walk(child);
  };
  walk(node(containerId));
  return found;
}
function renderChoices(containerId, pairs) {
  const root = node(containerId);
  const kept = Array.from(root.children || []).filter(child => child.tagName === "LEGEND" || (child.id && String(child.id).endsWith("-status")));
  root.replaceChildren(...kept);
  for (const [value, label] of pairs) {
    const line = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox"; box.value = value; box.checked = false;
    const caption = document.createElement("span"); caption.textContent = label;
    line.append(box, caption); root.append(line);
  }
}
function collectFilters() {
  const body = {};
  const documents = selectedBoxes("filter-documents");
  const categories = selectedBoxes("filter-categories");
  const subjects = selectedBoxes("filter-subjects");
  if (documents.length) body.document_ids = documents;
  if (categories.length) body.categories = categories;
  if (subjects.length) body.subject_ids = subjects;
  const field = id => String(node(id).value ?? "").trim();
  const from = field("page-from");
  const to = field("page-to");
  if (from || to) {
    const start = Number(from), end = Number(to);
    if (!from || !to || !Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start) {
      return {error: "页范围须为起页小于等于止页的正整数。"};
    }
    body.page_from = start; body.page_to = end;
  }
  const kind = field("filter-date-kind");
  if (kind === "point") {
    if (!field("filter-date-point")) return {error: "请填写要匹配的时点。"};
    body.date_point = field("filter-date-point");
  } else if (kind === "period") {
    const start = field("filter-period-start"), end = field("filter-period-end");
    if (!start || !end || start > end) return {error: "期间过滤须填写起日和止日，且起日不晚于止日。"};
    body.period_start = start; body.period_end = end;
  }
  return {body};
}
async function loadFilterOptions() {
  const project = node("search-project").value;
  if (!project) return;
  node("filter-documents-status").textContent = "正在读取材料…";
  node("filter-subjects-status").textContent = "正在读取主体…";
  try {
    const catalog = await request(`/api/projects/${project}/material-catalog?limit=100`);
    const items = Array.isArray(catalog?.items) ? catalog.items : [];
    renderChoices("filter-documents", items.map(item => [item.document_id, (item.filenames && item.filenames[0]) || item.document_id]));
    node("filter-documents-status").textContent = items.length ? `当前项目 ${catalog.total_documents} 份材料。` : "当前项目还没有材料。";
    const subjects = await request(`/api/projects/${project}/subjects`);
    const rows = Array.isArray(subjects) ? subjects : [];
    renderChoices("filter-subjects", rows.map(row => [row.id, row.name]));
    node("filter-subjects-status").textContent = rows.length ? `当前项目 ${rows.length} 个主体。` : "当前项目还没有主体。";
  } catch (error) {
    node("search-error").textContent = error.message;
  }
}
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
  if (node("search-filters").open && node("search-project").value) loadFilterOptions();
};
node("search-form").onsubmit = async event => {
 // 查询期间锁定项目选择，避免旧项目的响应出现在新项目中；失败不沿用旧查询结果。
  event.preventDefault();
  const project = node("search-project").value;
  if (!project || node("submit-search").disabled) return;
  const filters = collectFilters();
  if (filters.error) { node("search-error").textContent = filters.error; node("search-state").textContent = "请修正过滤条件后再查询。"; return; }
  const controller = new AbortController(); activeSearch = controller;
  node("cancel-search").hidden = false; node("cancel-search").disabled = false;
  node("submit-search").disabled = true;
  node("submit-search").textContent = "查找中…";
  clearResults(); node("search-error").textContent = "";
  node("search-state").textContent = "正在本地查找并排序，请等待…";
  try {
    const data = await request(`/api/projects/${project}/search`, {method: "POST", signal: controller.signal, headers: {"Content-Type": "application/json"}, body: JSON.stringify({query: node("query").value, ...filters.body})});
    if (controller.signal.aborted || activeSearch !== controller) return;
    const c = data.coverage;
    // 这是搜索附带的简要覆盖；独立的缺页／待重建／质量覆盖清单仍待 RG-02 实现。
    const outcome = data.results.length
      ? `找到 ${data.results.length} 条候选，请对照原件核实。`
      : (data.message || (data.status === "not_indexed" ? "尚无当前版本索引，请先分批建立索引。" : "没有候选片段，请换个关键词或补充已读页面的索引。"));
    node("search-state").textContent = `当前可检索 ${c.indexed_pages} / 已知 ${c.total_pages} 页；${c.unread_documents} 份材料尚未预检。${data.selection_changed ? "查询期间材料或候选发生变化，本轮结果不完整，请重新查询。" : c.complete ? "已覆盖已登记材料。" : "仅覆盖部分材料。"} ${outcome}`;
    if (data.results.length) {
      const excluded = [];
      if (data.excluded_uncategorized) excluded.push(`未分类排除 ${data.excluded_uncategorized} 份`);
      if (data.excluded_unsubjected) excluded.push(`未填主体排除 ${data.excluded_unsubjected} 份`);
      if (data.excluded_undated) excluded.push(`未填日期排除 ${data.excluded_undated} 份`);
      if (excluded.length) node("search-state").textContent += ` ${excluded.join("，")}。`;
    }
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
renderChoices("filter-categories", CATEGORIES);
node("search-filters").ontoggle = () => { if (node("search-filters").open) return loadFilterOptions(); };
init();
