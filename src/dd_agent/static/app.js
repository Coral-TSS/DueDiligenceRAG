"use strict";
// 材料工作区：项目切换、逐个上传、登记清单和原件下载，不直接执行模型推理。
// “全卷已读”来自基础阅读计数，不表示全部增强、向量化或法律审查已经完成。
const $ = (id) => document.getElementById(id);
let selected = null, projects = [], ready = false, uploading = false, generation = 0, limit = 0;
// 材料清单按 material-catalog 分批读取：每批 50 份，续读用游标，避免一次取回整个项目。
let materials = [], materialCursor = null, materialTotal = 0, loadingMaterials = false;
let coverageItems = [], coverageCursor = null;
const GAP_LABELS = {unprocessed: "未处理", failed: "失败", unknown: "未知", empty_text: "空文本", pending_rebuild: "待重建"};
const statusLabels = {registered: "已登记 · 待解析", unsupported: "暂不支持", invalid: "文件无效"};
const CATEGORIES = [
  ["uncategorized", "未分类"], ["business_license", "营业执照"], ["articles", "公司章程"],
  ["shareholder_resolution", "股东会决议"], ["board_resolution", "董事会决议"], ["equity_structure", "股权结构／名册"],
  ["audit_report", "审计报告"], ["financial_statement", "财务报表"], ["major_contract", "重大合同"],
  ["litigation", "诉讼／仲裁文书"], ["ip_certificate", "知识产权证书"], ["regulatory_filing", "监管报送／批复"],
  ["hr_labor", "劳动／人事材料"], ["interview_record", "访谈记录"], ["other", "其他"],
];
const categoryLabel = code => CATEGORIES.find(item => item[0] === code)?.[1] || "未分类";
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
  $("batch-apply").disabled = !ready || !selected || uploading;
  $("editor-save").disabled = !ready || !selected || uploading;
  document.querySelectorAll(".project").forEach(b => { b.disabled = false; });
}
function renderProjects() {
  $("projects").replaceChildren();
  if (!projects.length) $("projects").textContent = "还没有项目，先创建一个。";
  for (const p of projects) {
    const b = document.createElement("button"); b.className = "project"; b.textContent = p.name;
    b.setAttribute("aria-current", String(p.id === selected));
    b.onclick = () => selectProject(p.id); $("projects").append(b);
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
async function selectProject(id) {
  selected = id; rememberProject(id); generation++; editingId = null; subjectRows = [];
  $("files").value = ""; $("upload-results").replaceChildren();
  $("editor-panel").hidden = true; $("subject-rename-form").hidden = true; $("subject-error").textContent = "";
  $("project-title").textContent = projects.find(p => p.id === id)?.name || "选择或创建一个项目";
  renderProjects(); await loadMaterials({reset: true});
}
function cell(text) { const td = document.createElement("td"); td.textContent = text; return td; }
function size(bytes) { return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1048576).toFixed(1)} MiB`; }
function dateLabel(row) {
  if (row.date_kind === "point" && row.point_date) return row.point_date;
  if (row.date_kind === "period" && row.period_start) return `${row.period_start} 至 ${row.period_end}`;
  return "未填写";
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
function renderMaterials() {
  // 只重画已加载的批次；每项都是目录字段，不带正文。
  $("documents").replaceChildren();
  $("document-count").textContent = materialTotal ? `已显示 ${materials.length} / ${materialTotal} 份材料` : "";
  $("empty").hidden = materials.length > 0;
  $("empty").textContent = "项目已创建，还没有材料。可以从上方导入原件，或创建主体。";
  $("documents-table").hidden = !materials.length;
  $("batch-panel").hidden = !materials.length;
  $("more-materials").hidden = materialCursor === null;
  for (const d of materials) {
    const row = document.createElement("tr"), name = cell(d.filenames[0]);
    if (d.filenames.length > 1) { const extra = document.createElement("small"); extra.textContent = `其他来源：${d.filenames.slice(1).join("、")}`; name.append(extra); }
    const pick = cell(""); const box = document.createElement("input");
    box.type = "checkbox"; box.className = "pick-material"; box.value = d.document_id; box.checked = false;
    pick.append(box);
    const state = cell(""); const badge = document.createElement("span"); badge.className = `badge ${d.status}`;
    const reason = document.createElement("small");
    if (d.reading_version) {
      // page_count 是材料页数，不是成功页数；成功／失败数量请到识别结果页查看。
      badge.textContent = `已读取 · ${d.page_count} 页`;
      reason.textContent = "成功与失败页数以识别结果页为准。";
    } else {
      badge.textContent = statusLabels[d.status] || d.status;
      reason.textContent = d.reason || "";
    }
    state.append(badge, reason);
    const download = cell(""); const link = document.createElement("a"); link.textContent = "下载"; link.href = `/api/projects/${selected}/documents/${d.document_id}/file`; link.setAttribute("aria-label", `下载 ${d.filenames[0]}`); download.append(link);
    const readingLink = document.createElement("a"); readingLink.textContent = "自动处理与识别结果"; readingLink.href = `/reader?project=${selected}&document=${d.document_id}`; const readingRow = document.createElement("small"); readingRow.append(readingLink); download.append(readingRow);
    const edit = document.createElement("button"); edit.type = "button"; edit.className = "secondary"; edit.textContent = "材料信息";
    edit.onclick = () => openEditor(d); download.append(edit);
    row.append(pick, name, cell(categoryLabel(d.category)), cell(d.subject_name || "未填写"), cell(dateLabel(d)), state, cell(size(d.byte_size)), cell(new Date(d.created_at).toLocaleString("zh-CN", {hour12:false})), download);
    $("documents").append(row);
  }
}
function subjectOptions() {
  return subjectRows.map(row => [row.id, `${row.name}（${{target:"目标公司",subsidiary:"子公司",shareholder:"股东",counterparty:"交易对手",other:"其他"}[row.role] || "其他"}）`]);
}
function renderSubjects() {
  $("subjects").replaceChildren();
  if (!subjectRows.length) {
    $("subject-status").textContent = "当前项目还没有主体。可以创建主体后再绑定材料。";
    return;
  }
  $("subject-status").textContent = `本项目 ${subjectRows.length} 个主体。`;
  for (const row of subjectRows) {
    const item = document.createElement("li");
    const rename = document.createElement("button"); rename.type = "button"; rename.className = "secondary"; rename.textContent = "改名";
    rename.onclick = () => {
      $("subject-rename-id").value = row.id; $("subject-rename-name").value = row.name; $("subject-rename-form").hidden = false;
    };
    item.textContent = `${row.name} · ${{target:"目标公司",subsidiary:"子公司",shareholder:"股东",counterparty:"交易对手",other:"其他"}[row.role] || "其他"} `;
    item.append(rename); $("subjects").append(item);
  }
  fillSelect("editor-subject", subjectOptions(), "未填写");
  fillSelect("batch-subject", subjectOptions(), "未填写");
}
async function loadSubjects(project, token) {
  $("subject-status").textContent = "正在读取主体…";
  $("subject-error").textContent = "";
  try {
    const rows = await api(`/api/projects/${project}/subjects`);
    if (token !== generation) return;
    subjectRows = rows; renderSubjects();
  } catch (error) {
    if (token === generation) { $("subject-error").textContent = error.message; $("subject-status").textContent = "主体暂时无法读取。"; }
  }
}
function openEditor(row) {
  editingId = row.document_id;
  $("editor-panel").hidden = false;
  $("editor-title").textContent = `材料信息 · ${row.filenames[0]}`;
  $("editor-error").textContent = "";
  fillSelect("editor-category", CATEGORIES);
  fillSelect("editor-subject", subjectOptions(), "未填写");
  $("editor-category").value = row.category || "uncategorized";
  $("editor-subject").value = row.subject_id || "";
  $("editor-date-kind").value = row.date_kind || "none";
  $("editor-point").value = row.point_date || "";
  $("editor-period-start").value = row.period_start || "";
  $("editor-period-end").value = row.period_end || "";
}
function checkedDocumentIds() {
  const ids = [];
  const walk = el => {
    if (!el) return;
    if (el.className === "pick-material" && el.checked && el.value) ids.push(el.value);
    for (const child of el.children || []) walk(child);
  };
  walk($("documents"));
  return ids;
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
async function loadMaterials({reset = false} = {}) {
  // 保存本次项目与请求代次，丢弃切换项目后才返回的旧响应。
  const current = selected, token = ++generation;
  if (!current) {
    materials = []; materialCursor = null; materialTotal = 0;
    $("empty").hidden = false; $("empty").textContent = "先创建项目，再将收集到的材料导入这里。";
    $("documents-table").hidden = true; $("batch-panel").hidden = true; $("editor-panel").hidden = true;
    $("more-materials").hidden = true; $("document-count").textContent = "";
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
    const data = await api(`/api/projects/${current}/material-catalog?${query}`);
    if (token !== generation) return;
    materials = materials.concat(data.items); materialCursor = data.next_cursor; materialTotal = data.total_documents;
    renderMaterials();
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
    $("upload-help").textContent = `可多选；逐个导入，单个失败不会中断其他文件。单文件上限 ${size(limit)}。`;
    if (ready) {
      projects = await api("/api/projects");
      const requested = new URLSearchParams(location.search).get("project");
      selected = projects.some(p => p.id === requested) ? requested : null;
      await selectProject(selected);
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
  const files = Array.from($("files").files), project = selected, token = generation;
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
$("retry").onclick = refresh;
$("more-materials").onclick = () => loadMaterials();
fillSelect("editor-category", CATEGORIES);
fillSelect("batch-category", CATEGORIES);
$("editor-cancel").onclick = () => { $("editor-panel").hidden = true; editingId = null; };
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
    await api(`/api/projects/${selected}/subjects`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:$("subject-name").value, role:$("subject-role").value})});
    $("subject-name").value = ""; await loadSubjects(selected, generation);
  } catch (error) { $("subject-error").textContent = error.message; }
};
$("subject-rename-form").onsubmit = async event => {
  event.preventDefault(); if (!selected) return;
  const id = $("subject-rename-id").value;
  $("subject-error").textContent = "";
  try {
    await api(`/api/projects/${selected}/subjects/${id}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name:$("subject-rename-name").value})});
    $("subject-rename-form").hidden = true; await loadSubjects(selected, generation);
  } catch (error) { $("subject-error").textContent = error.message; }
};
$("batch-apply").onclick = async () => {
  if (!selected) return;
  const ids = checkedDocumentIds();
  const body = {document_ids: ids};
  if ($("batch-category-toggle").checked) body.category = {set: true, value: $("batch-category").value};
  if ($("batch-subject-toggle").checked) body.subject = {set: true, value: $("batch-subject").value || null};
  if ($("batch-date-toggle").checked) {
    const kind = $("batch-date-kind").value;
    body.date = {set: true, kind, point_date: kind === "point" ? $("batch-point").value || null : null, period_start: kind === "period" ? $("batch-period-start").value || null : null, period_end: kind === "period" ? $("batch-period-end").value || null : null};
  }
  $("batch-error").textContent = "";
  if (!ids.length) { $("batch-error").textContent = "请先勾选材料。"; return; }
  try {
    await api(`/api/projects/${selected}/documents/metadata`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    await loadMaterials({reset: true});
  } catch (error) { $("batch-error").textContent = error.message; }
};
controls(); refresh();
