"use strict";
// 材料工作区：项目切换、逐个上传、登记清单和原件下载，不直接执行模型推理。
// “全卷已读”来自基础阅读计数，不表示全部增强、向量化或法律审查已经完成。
const $ = (id) => document.getElementById(id);
let selected = null, projects = [], ready = false, uploading = false, generation = 0, limit = 0;
// 材料清单按 material-catalog 分批读取：每批 50 份，续读用游标，避免一次取回整个项目。
let materials = [], materialCursor = null, materialTotal = 0, loadingMaterials = false;
const statusLabels = {registered: "已登记 · 待解析", unsupported: "暂不支持", invalid: "文件无效"};
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const detail = result.detail;
    throw new Error(Array.isArray(detail) ? detail.map(d => d.msg).join("；") : (detail || "操作失败，请重试。"));
  }
  return result;
}
function controls() {
  $("create-project").disabled = !ready || uploading;
  $("project-name").disabled = !ready || uploading;
  $("files").disabled = !ready || !selected || uploading;
  $("upload").disabled = !ready || !selected || uploading || !$("files").files.length;
  $("upload").textContent = uploading ? "正在逐个导入…" : "导入所选文件";
  document.querySelectorAll(".project").forEach(b => b.disabled = uploading);
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
async function selectProject(id) {
  selected = id; generation++; $("files").value = ""; $("upload-results").replaceChildren();
  $("project-title").textContent = projects.find(p => p.id === id)?.name || "选择或创建一个项目";
  renderProjects(); await loadMaterials({reset: true});
}
function cell(text) { const td = document.createElement("td"); td.textContent = text; return td; }
function size(bytes) { return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1048576).toFixed(1)} MiB`; }
function renderMaterials() {
  // 只重画已加载的批次；每项都是目录字段，不带正文。
  $("documents").replaceChildren();
  $("document-count").textContent = materialTotal ? `已显示 ${materials.length} / ${materialTotal} 份材料` : "";
  $("empty").hidden = materials.length > 0;
  $("empty").textContent = "项目已创建，还没有材料。可以从上方导入原件。";
  $("documents-table").hidden = !materials.length;
  $("more-materials").hidden = materialCursor === null;
  for (const d of materials) {
    const row = document.createElement("tr"), name = cell(d.filenames[0]);
    if (d.filenames.length > 1) { const extra = document.createElement("small"); extra.textContent = `其他来源：${d.filenames.slice(1).join("、")}`; name.append(extra); }
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
    row.append(name, state, cell(size(d.byte_size)), cell(new Date(d.created_at).toLocaleString("zh-CN", {hour12:false})), download); $("documents").append(row);
  }
}
async function loadMaterials({reset = false} = {}) {
  // 保存本次项目与请求代次，丢弃切换项目后才返回的旧响应。
  const current = selected, token = ++generation;
  if (!current) {
    materials = []; materialCursor = null; materialTotal = 0;
    $("empty").hidden = false; $("empty").textContent = "先创建项目，再将收集到的材料导入这里。";
    $("documents-table").hidden = true; $("more-materials").hidden = true; $("document-count").textContent = ""; return;
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
  } catch (error) {
    if (token === generation) { $("list-error").textContent = error.message; $("empty").textContent = "材料暂时无法读取，请检查连接后重试。"; }
  } finally { if (token === generation) { loadingMaterials = false; $("more-materials").disabled = false; } }
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
      if (!projects.some(p => p.id === selected)) selected = projects[0]?.id || null;
      await selectProject(selected);
    }
  } catch (error) { ready = false; $("health-title").textContent = "暂时无法连接"; $("health-message").textContent = error.message; }
  finally { controls(); $("retry").disabled = false; }
}
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
  const files = Array.from($("files").files), project = selected;
  uploading = true; controls(); $("retry").disabled = true; $("upload-results").replaceChildren();
  try {
    for (let i = 0; i < files.length; i++) {
      const file = files[i], line = document.createElement("div"); line.className = "upload-result";
      line.textContent = `${i+1}/${files.length} · ${file.name}：正在导入…`; $("upload-results").append(line);
      try {
        if (file.size > limit) throw new Error(`超过 ${size(limit)} 上限，未上传。`);
        const data = new FormData(); data.append("file", file);
        const result = await api(`/api/projects/${project}/documents`, {method:"POST", body:data});
        line.textContent = `${file.name}：${result.duplicate ? "内容重复，已保留来源名" : statusLabels[result.document.status]}`;
      } catch (error) { line.classList.add("failed"); line.textContent = `${file.name}：${error.message}`; }
    }
    await loadMaterials({reset: true});
  } finally { uploading = false; $("files").value = ""; controls(); $("retry").disabled = false; }
};
$("retry").onclick = refresh;
$("more-materials").onclick = () => loadMaterials();
controls(); refresh();
