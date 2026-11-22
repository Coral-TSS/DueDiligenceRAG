"use strict";
// 材料工作区：项目切换、按固定目录归集、逐个上传、登记清单和原件下载，不直接执行模型推理。
// “全卷已读”来自基础阅读计数，不表示全部增强、向量化或法律审查已经完成。
const $ = (id) => document.getElementById(id);
let selected = null, projects = [], ready = false, uploading = false, processingBatch = false, generation = 0, limit = 0;
// 材料清单按 material-catalog 分批读取：每批 50 份，续读用游标，避免一次取回整个项目。
let materials = [], materialCursor = null, materialTotal = 0, loadingMaterials = false;
let coverageItems = [], coverageCursor = null;
let selectedFolder = "other";
let folderCounts = {};
const GAP_LABELS = {unprocessed: "未处理", failed: "失败", unknown: "未知", empty_text: "空文本", pending_rebuild: "待重建"};
const statusLabels = {registered: "已登记 · 待解析", unsupported: "暂不支持", invalid: "文件无效"};
const processingLabels = {
  queued: "排队中", running: "处理中", pause_requested: "正在暂停",
  paused: "已暂停", completed: "已处理", partial: "部分完成", failed: "处理失败",
};
const CATEGORIES = [
  ["incorporation", "公司的设立、变更"], ["organizational_docs", "公司的组织性文件"],
  ["shareholders", "股东及实际控制人"], ["outbound_investment", "对外股权投资"], ["business", "公司的业务"],
  ["major_contracts", "公司的重大合同"], ["major_assets", "公司拥有或使用的主要资产"], ["ip", "公司的知识产权"],
  ["litigation_admin", "公司的诉讼、行政处罚"], ["environmental", "公司的环境保护"], ["product_quality", "公司的产品服务质量"],
  ["labor_hr", "公司的劳动关系及人力资源"], ["finance", "财务相关"], ["attachments", "附件"],
  ["subsidiary_aic", "子公司工商档案"], ["other", "其他"],
];
const categoryLabel = code => CATEGORIES.find(item => item[0] === code)?.[1] || "其他";
const ROLE_LABELS = {target:"目标公司",subsidiary:"子公司",shareholder:"股东",counterparty:"交易对手",other:"其他"};
let subjectRows = [];
let editingId = null;
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const detail = result.detail;
    const error = new Error(Array.isArray(detail) ? detail.map(d => d.msg).join("；") : (detail || "操作失败，请重试。"));
    error.fields = result.fields || {};
    throw error;
  }
  return result;
}
function controls() {
  $("create-project").disabled = !ready || uploading;
  $("project-name").disabled = !ready || uploading;
  $("files").disabled = !ready || !selected || uploading;
  $("upload").disabled = !ready || !selected || uploading || !$("files").files.length;
  $("upload").textContent = uploading ? "正在逐个导入…" : "导入所选文件";
  $("create-subject").disabled = !ready || !selected || uploading;
  $("subject-name").disabled = !ready || !selected || uploading;
  $("editor-save").disabled = !ready || !selected || uploading;
  const canProcess = ready && selected && !uploading && !processingBatch;
  if ($("process-folder")) $("process-folder").disabled = !canProcess;
  if ($("process-all")) $("process-all").disabled = !canProcess;
  document.querySelectorAll(".project").forEach(b => { b.disabled = false; });
}
function renderProjects() {
  $("projects").replaceChildren();
  if (!projects.length) $("projects").textContent = "还没有项目，先创建一个。";
  for (const p of projects) {
    const b = document.createElement("button"); b.className = "project"; b.textContent = p.name;
    b.setAttribute("aria-current", String(p.id === selected));
    b.onclick = () => selectProject(p.id, {preferNonEmptyFolder: true}); $("projects").append(b);
  }
  controls();
}
function rememberProject(id) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("project", id);
  else url.searchParams.delete("project");
  history.replaceState(null, "", url);
  const link = $("search-link");
  if (link) link.href = id ? `/search?project=${id}` : "/search";
}
async function selectProject(id, {preferNonEmptyFolder = false} = {}) {
  selected = id; rememberProject(id); generation++; editingId = null; subjectRows = [];
  $("files").value = ""; $("upload-results").replaceChildren();
  $("editor-panel").hidden = true; $("subject-rename-form").hidden = true; $("subject-error").textContent = "";
  $("project-title").textContent = projects.find(p => p.id === id)?.name || "选择或创建一个项目";
  renderProjects(); await loadMaterials({reset: true, preferNonEmptyFolder});
}
function cell(text) { const td = document.createElement("td"); td.textContent = text; return td; }
function size(bytes) { return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1048576).toFixed(1)} MiB`; }
function dateLabel(row) {
  if (row.date_kind === "point" && row.point_date) return row.point_date;
  if (row.date_kind === "period" && row.period_start) return `${row.period_start} 至 ${row.period_end}`;
  return "";
}
function fillSelect(id, pairs, blank) {
  const select = $(id);
  select.replaceChildren();
  if (blank !== undefined) {
    const option = document.createElement("option"); option.value = ""; option.textContent = blank; select.append(option);
  }
  for (const [value, label] of pairs) {
    const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option);
  }
}
function folderHint() {
  const name = categoryLabel(selectedFolder);
  const help = $("upload-help");
  if (!help) return;
  const cap = size(limit || 0);
  const base = limit ? `可多选；逐个导入，单个失败不会中断其他文件。单文件上限 ${cap}。` : "可多选；逐个导入，单个失败不会中断其他文件。";
  help.textContent = selected ? `导入会归入当前目录「${name}」。${base}` : base;
}
function renderFolders() {
  const nav = $("folder-list");
  if (!nav) return;
  nav.replaceChildren();
  for (const [code, label] of CATEGORIES) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "folder";
    button.setAttribute("aria-current", String(code === selectedFolder));
    const name = document.createElement("span"); name.className = "folder-name"; name.textContent = label;
    const count = document.createElement("span"); count.className = "folder-count";
    const n = folderCounts[code] || 0;
    count.textContent = String(n);
    button.append(name, count);
    button.setAttribute("aria-label", `${label}，${n} 份`);
    button.onclick = () => selectFolder(code);
    nav.append(button);
  }
}
function selectFolder(code) {
  if (code === selectedFolder) return;
  selectedFolder = code;
  renderFolders();
  folderHint();
  if (selected) loadMaterials({reset: true});
}
function moveSelect(row) {
  const wrap = document.createElement("label");
  wrap.className = "muted";
  wrap.append("移动到 ");
  const select = document.createElement("select");
  select.className = "move-folder";
  select.setAttribute("aria-label", `将 ${row.filenames[0]} 移动到目录`);
  for (const [value, label] of CATEGORIES) {
    const option = document.createElement("option"); option.value = value; option.textContent = label; select.append(option);
  }
  select.value = row.category || "other";
  select.onchange = async () => {
    if (!selected) return;
    $("list-error").textContent = "";
    try {
      await api(`/api/projects/${selected}/documents/metadata`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({document_ids: [row.document_id], category: {set: true, value: select.value}}),
      });
      await loadMaterials({reset: true});
    } catch (error) {
      $("list-error").textContent = error.message;
      select.value = row.category || "other";
    }
  };
  wrap.append(select);
  return wrap;
}
function fileNameCell(row) {
  const td = cell(row.filenames[0]);
  if (row.filenames.length > 1) {
    const extra = document.createElement("small");
    extra.textContent = `其他来源：${row.filenames.slice(1).join("、")}`;
    td.append(extra);
  }
  const bits = [row.subject_name, dateLabel(row)].filter(Boolean);
  if (bits.length) {
    const meta = document.createElement("small");
    meta.className = "file-meta";
    meta.textContent = bits.join(" · ");
    td.append(meta);
  }
  return td;
}
function renderMaterials() {
  // 只重画已加载的批次；每项都是目录字段，不带正文。
  $("documents").replaceChildren();
  const folderName = categoryLabel(selectedFolder);
  $("document-count").textContent = selected ? `「${folderName}」已显示 ${materials.length} / ${materialTotal} 份` : "";
  $("empty").hidden = materials.length > 0;
  $("empty").textContent = selected
    ? `「${folderName}」里还没有材料。导入时会归入当前目录。`
    : "项目已创建，还没有材料。可以从上方导入原件。";
  $("documents-table").hidden = !materials.length;
  $("more-materials").hidden = materialCursor === null;
  for (const d of materials) {
    const row = document.createElement("tr");
    const state = cell(""); const badge = document.createElement("span");
    const reason = document.createElement("small");
    const job = d.processing_status;
    if (job && ["queued","running","pause_requested","paused","failed","partial"].includes(job)) {
      badge.className = `badge ${job}`;
      badge.textContent = processingLabels[job] || job;
      reason.textContent = d.reading_version ? `已读取 ${d.page_count} 页。` : (d.reason || "");
    } else if (d.reading_version) {
      // page_count 是材料页数，不是成功页数；成功／失败数量请到识别结果页查看。
      badge.className = "badge registered";
      badge.textContent = `已读取 · ${d.page_count} 页`;
      reason.textContent = "成功与失败页数以识别结果页为准。";
    } else {
      badge.className = `badge ${d.status}`;
      badge.textContent = job === "completed" ? "已处理" : (statusLabels[d.status] || d.status);
      reason.textContent = d.reason || "";
    }
    state.append(badge, reason);
    const actions = document.createElement("td");
    actions.className = "row-actions";
    const link = document.createElement("a"); link.textContent = "下载"; link.href = `/api/projects/${selected}/documents/${d.document_id}/file`; link.setAttribute("aria-label", `下载 ${d.filenames[0]}`);
    const readingLink = document.createElement("a"); readingLink.textContent = "识别结果"; readingLink.href = `/reader?project=${selected}&document=${d.document_id}`;
    const edit = document.createElement("button"); edit.type = "button"; edit.className = "secondary"; edit.textContent = "材料信息";
    edit.onclick = () => openEditor(d);
    actions.append(link, readingLink, edit, moveSelect(d));
    row.append(fileNameCell(d), cell(categoryLabel(d.category)), state, cell(size(d.byte_size)), cell(new Date(d.created_at).toLocaleString("zh-CN", {hour12:false})), actions);
    $("documents").append(row);
  }
}
function subjectOptions() {
  return subjectRows.map(row => [row.id, `${row.name}（${ROLE_LABELS[row.role] || "其他"}）`]);
}
function syncSubjectTools() {
  const selectedId = $("editor-subject") ? $("editor-subject").value : "";
  const open = $("subject-rename-open");
  const form = $("subject-rename-form");
  if (open) open.hidden = !selectedId;
  if (form && !selectedId) form.hidden = true;
}
function renderSubjects() {
  const select = $("editor-subject");
  const previous = select ? select.value : "";
  fillSelect("editor-subject", subjectOptions(), "未指定");
  if (select && (previous === "" || subjectRows.some(row => row.id === previous))) select.value = previous;
  syncSubjectTools();
}
async function loadSubjects(project, token) {
  $("subject-error").textContent = "";
  try {
    const rows = await api(`/api/projects/${project}/subjects`);
    if (token !== generation) return;
    subjectRows = rows; renderSubjects();
  } catch (error) {
    if (token === generation) $("subject-error").textContent = error.message;
  }
}
function openEditor(row) {
  editingId = row.document_id;
  $("editor-panel").hidden = false;
  $("editor-title").textContent = `材料信息 · ${row.filenames[0]}`;
  $("editor-error").textContent = "";
  $("subject-error").textContent = "";
  $("subject-rename-form").hidden = true;
  fillSelect("editor-category", CATEGORIES);
  fillSelect("editor-subject", subjectOptions(), "未指定");
  $("editor-category").value = row.category || "other";
  $("editor-subject").value = row.subject_id || "";
  $("editor-date-kind").value = row.date_kind || "none";
  $("editor-point").value = row.point_date || "";
  $("editor-period-start").value = row.period_start || "";
  $("editor-period-end").value = row.period_end || "";
  syncSubjectTools();
}
function clearCoverage(message) {
  coverageItems = []; coverageCursor = null;
  const summary = $("coverage-summary"), gaps = $("coverage-gaps"), error = $("coverage-error"), more = $("more-gaps");
  const definition = $("coverage-definition");
  if (summary) summary.textContent = message;
  if (gaps) gaps.replaceChildren();
  if (error) error.textContent = "";
  if (definition) definition.textContent = "";
  if (more) more.hidden = true;
}
function renderCoverage(data, append) {
  const summary = $("coverage-summary"), gaps = $("coverage-gaps"), more = $("more-gaps");
  const definition = $("coverage-definition");
  if (!summary || !gaps) return;
  if (!append) coverageItems = [];
  coverageItems = coverageItems.concat(data.items || []);
  coverageCursor = data.next_cursor || null;
  const counts = data.counts || {};
  const lines = Object.entries(GAP_LABELS).map(([key, label]) => `${label} ${counts[key] ?? 0}`);
  const verdict = data.complete ? "当前口径下没有缺口。" : "当前项目不能视为全文可检索。空项目、页数未知和空文本都不算全文可检索。";
  summary.textContent = `${verdict} 已知页数 ${counts.known_pages ?? 0}。${lines.join("，")}。这些数字不能相加。`;
  if (definition) definition.textContent = data.definition || "";
  gaps.replaceChildren();
  for (const item of coverageItems) {
    const row = document.createElement("li");
    const kinds = (item.kinds || []).map(kind => GAP_LABELS[kind] || kind).join("、");
    const where = item.page_number ? `第 ${item.page_number} 页` : (item.locator && item.locator.kind ? item.locator.kind : "整份材料");
    const name = (item.filenames && item.filenames[0]) || item.document_id;
    row.textContent = `${name} · ${where} · ${kinds}`;
    gaps.append(row);
  }
  if (!coverageItems.length) {
    const row = document.createElement("li");
    row.textContent = data.complete ? "没有缺口条目。" : "没有可分页的缺口条目，但当前项目仍不能视为全文可检索。";
    gaps.append(row);
  }
  if (more) more.hidden = !coverageCursor;
}
async function loadCoverage(project, token, cursor) {
  const error = $("coverage-error");
  if (!project) { clearCoverage("先选择项目，再查看覆盖缺口。不按全库统计。"); return; }
  if (!cursor) clearCoverage("正在读取覆盖缺口…");
  try {
    const query = new URLSearchParams({limit: "50"});
    if (cursor) query.set("cursor", cursor);
    const data = await api(`/api/projects/${project}/coverage?${query}`);
    if (token !== generation || project !== selected) return;
    if (error) error.textContent = "";
    renderCoverage(data, Boolean(cursor));
  } catch (errorValue) {
    if (token === generation && error) error.textContent = errorValue.message;
  }
}

function preferFolderWithMaterials() {
  // 打开项目时，如果当前目录是空的、别的目录有材料，就先进入有材料的目录，避免看起来像“全部丢了”。
  if ((folderCounts[selectedFolder] || 0) > 0) return false;
  const found = CATEGORIES.find(([code]) => (folderCounts[code] || 0) > 0);
  if (!found) return false;
  selectedFolder = found[0];
  return true;
}

async function loadMaterials({reset = false, preferNonEmptyFolder = false} = {}) {
  // 保存本次项目与请求代次，丢弃切换项目后才返回的旧响应。
  const current = selected, token = ++generation;
  if (!current) {
    materials = []; materialCursor = null; materialTotal = 0; folderCounts = {};
    $("empty").hidden = false; $("empty").textContent = "先创建项目，再将收集到的材料导入这里。";
    $("documents-table").hidden = true; $("editor-panel").hidden = true;
    $("more-materials").hidden = true; $("document-count").textContent = "";
    if ($("process-status")) $("process-status").textContent = "";
    renderFolders(); folderHint();
    clearCoverage("先选择项目，再查看覆盖缺口。不按全库统计。");
    return;
  }
  if (reset) { materials = []; materialCursor = null; materialTotal = 0; $("documents").replaceChildren(); }
  $("list-error").textContent = ""; $("empty").hidden = materials.length > 0;
  if (!materials.length) { $("empty").hidden = false; $("empty").textContent = "正在读取材料…"; }
  loadingMaterials = true; $("more-materials").disabled = true;
  try {
    const query = new URLSearchParams({limit: "50"});
    if (materialCursor) query.set("after", materialCursor);
    if (selectedFolder) query.append("categories", selectedFolder);
    const data = await api(`/api/projects/${current}/material-catalog?${query}`);
    if (token !== generation) return;
    materials = materials.concat(data.items); materialCursor = data.next_cursor; materialTotal = data.total_documents;
    if (data.category_counts) folderCounts = data.category_counts;
    if (preferNonEmptyFolder && preferFolderWithMaterials()) {
      renderFolders(); folderHint();
      return loadMaterials({reset: true});
    }
    renderFolders(); renderMaterials(); folderHint();
    await loadSubjects(current, token);
  } catch (error) {
    if (token === generation) { $("list-error").textContent = error.message; $("empty").textContent = "材料暂时无法读取，请检查连接后重试。"; }
  } finally {
    if (token === generation) {
      loadingMaterials = false; $("more-materials").disabled = false;
      await loadCoverage(current, token);
    }
  }
}
async function refresh() {
  // 先检查数据库／配置是否就绪，再读取项目；不把连接正常等同于模型已加载。
  $("retry").disabled = true;
  try {
    const health = await api("/api/health"); ready = health.status === "ready"; limit = health.max_upload_bytes;
    $("health-title").textContent = ready ? "工作区已就绪" : "工作区需要配置";
    $("health-message").textContent = health.message;
    $("health-title").closest("section").classList.toggle("ready", ready);
    folderHint();
    if (ready) {
      projects = await api("/api/projects");
      const requested = new URLSearchParams(location.search).get("project");
      const fallback = projects[0] ? projects[0].id : null;
      selected = projects.some(p => p.id === requested) ? requested : fallback;
      await selectProject(selected, {preferNonEmptyFolder: true});
    }
  } catch (error) { ready = false; $("health-title").textContent = "暂时无法连接"; $("health-message").textContent = error.message; }
  finally { controls(); $("retry").disabled = false; }
}
$("more-gaps").onclick = () => { if (selected && coverageCursor) loadCoverage(selected, generation, coverageCursor); };
$("project-form").onsubmit = async (event) => {
  event.preventDefault(); $("project-error").textContent = ""; $("create-project").disabled = true;
  try {
    const p = await api("/api/projects", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:$("project-name").value})});
    projects.unshift(p); $("project-name").value = ""; await selectProject(p.id);
  } catch (error) { $("project-error").textContent = error.message; }
  finally { controls(); }
};
$("files").onchange = controls;
$("upload-form").onsubmit = async (event) => {
  event.preventDefault(); if (uploading || !selected) return;
  const files = Array.from($("files").files), project = selected, token = generation, folder = selectedFolder;
  uploading = true; controls(); $("retry").disabled = true; $("upload-results").replaceChildren();
  try {
    for (let i = 0; i < files.length; i++) {
      const file = files[i], line = document.createElement("div"); line.className = "upload-result";
      if (token === generation) {
        line.textContent = `${i+1}/${files.length} · ${file.name}：正在导入…`; $("upload-results").append(line);
      }
      try {
        if (file.size > limit) throw new Error(`超过 ${size(limit)} 上限，未上传。`);
        const data = new FormData(); data.append("file", file);
        if (folder) data.append("category", folder);
        const result = await api(`/api/projects/${project}/documents`, {method:"POST", body:data});
        if (token !== generation) continue;
        line.textContent = `${file.name}：${result.duplicate ? "内容重复，已保留来源名" : statusLabels[result.document.status]}`;
      } catch (error) {
        if (token !== generation) continue;
        line.classList.add("failed"); line.textContent = `${file.name}：${error.message}`;
      }
    }
    if (token === generation) await loadMaterials({reset: true});
  } finally { uploading = false; $("files").value = ""; controls(); $("retry").disabled = false; }
};
async function processBatch(scope) {
  if (!selected || processingBatch) return;
  processingBatch = true; controls();
  const status = $("process-status");
  if (status) status.textContent = scope === "folder" ? "正在将本目录已登记材料加入队列…" : "正在将全部已登记材料加入队列…";
  $("list-error").textContent = "";
  try {
    const body = scope === "folder" ? {category: selectedFolder} : {};
    const result = await api(`/api/projects/${selected}/processing`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    if (status) status.textContent = result.message || `已加入队列 ${result.queued || 0} 份。`;
    await loadMaterials({reset: true});
  } catch (error) {
    if (status) status.textContent = "";
    $("list-error").textContent = error.message;
  } finally {
    processingBatch = false; controls();
  }
}
$("process-folder").onclick = () => processBatch("folder");
$("process-all").onclick = () => processBatch("all");
$("retry").onclick = refresh;
$("more-materials").onclick = () => loadMaterials();
fillSelect("editor-category", CATEGORIES);
renderFolders();
folderHint();
$("editor-cancel").onclick = () => { $("editor-panel").hidden = true; editingId = null; };
$("editor-subject").onchange = syncSubjectTools;
$("editor-save").onclick = async () => {
  if (!selected || !editingId) return;
  const kind = $("editor-date-kind").value;
  const body = {
    category: $("editor-category").value,
    subject_id: $("editor-subject").value || null,
    date_kind: kind,
    point_date: kind === "point" ? $("editor-point").value || null : null,
    period_start: kind === "period" ? $("editor-period-start").value || null : null,
    period_end: kind === "period" ? $("editor-period-end").value || null : null,
  };
  $("editor-error").textContent = "";
  try {
    await api(`/api/projects/${selected}/documents/${editingId}/metadata`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    $("editor-panel").hidden = true; editingId = null; await loadMaterials({reset: true});
  } catch (error) {
    const fields = Object.values(error.fields || {}).filter(Boolean).join("；");
    $("editor-error").textContent = fields ? `${error.message} ${fields}` : error.message;
  }
};
$("subject-form").onsubmit = async event => {
  event.preventDefault(); if (!selected) return;
  $("subject-error").textContent = "";
  try {
    const created = await api(`/api/projects/${selected}/subjects`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:$("subject-name").value, role:$("subject-role").value})});
    $("subject-name").value = "";
    await loadSubjects(selected, generation);
    if (created && created.id) $("editor-subject").value = created.id;
    syncSubjectTools();
  } catch (error) { $("subject-error").textContent = error.message; }
};
$("subject-rename-open").onclick = () => {
  const id = $("editor-subject").value;
  if (!id) return;
  const row = subjectRows.find(item => item.id === id);
  $("subject-rename-id").value = id;
  $("subject-rename-name").value = row ? row.name : "";
  $("subject-rename-form").hidden = false;
};
$("subject-rename-form").onsubmit = async event => {
  event.preventDefault(); if (!selected) return;
  const id = $("subject-rename-id").value;
  $("subject-error").textContent = "";
  try {
    await api(`/api/projects/${selected}/subjects/${id}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:$("subject-rename-name").value})});
    $("subject-rename-form").hidden = true;
    await loadSubjects(selected, generation);
    $("editor-subject").value = id;
    syncSubjectTools();
  } catch (error) { $("subject-error").textContent = error.message; }
};
controls(); refresh();
