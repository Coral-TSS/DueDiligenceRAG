"use strict";
// 纯展示函数：同一任务对象得到同一套文案和按钮状态，可独立于浏览器验证。
// status 是持久化调度状态；execution_state 是后端根据执行锁观察出的派生状态。
// current_page 可能在恢复重检时变小，processed_pages 仍保留已保存的整批检查点。
(function(root){
 function processingView(job){
  if(!job)return {text:"尚未启动自动处理任务。",pending:false,active:false,resumeAvailable:false,startHidden:false};
  const pending=["queued","running","pause_requested"].includes(job.status);
  const interrupted=job.execution_state==="interrupted";
  // 中断不等于计算仍在进行；允许明确继续，但不能伪装成“正在等待当前页保存”。
  const resumeAvailable=interrupted||job.status==="paused";
  const labels={queued:"等待处理",running:"正在自动处理",pause_requested:"正在暂停：等待当前页／区域保存",paused:"已暂停，可稍后继续",completed:"处理完成",partial:"部分处理未完成，可重新运行",failed:"处理失败，请检查本地模型与配置后重试"};
  const p=job.progress||{},stages={preflight:"检测材料",reading:"提取文字／渲染",ocr:"基础 OCR",vision:"视觉增强",vision_saved:"视觉区域已保存",embedding:"向量化入库",batch_saved:"本批已保存",completed:"完成"};
  let text=interrupted?"执行已中断，已保存结果保留，可继续处理。":(labels[job.status]||job.status)+"。";
  if(!interrupted&&p.recovery)text+="上次执行已停止，已保存结果保留。";
  if(p.scheduler==="stages-v1"){
   if(p.parent_index_mode==="shadow")text+=" 跨页索引试运行，当前检索使用页级索引。";
   else if(p.parent_index_mode==="off")text+=" 跨页索引未启用，已保存成果保留。";
   const names={reading:"基础识别",vision:"机器文字",embedding:"向量入库"};
   const waiting={model_resource:"等待模型资源",backpressure:"等待下游释放积压",paused:"已暂停"};
   for(const [name,label] of Object.entries(names)){
    const value=p.stages?.[name]||{};
    text+=` ${label} ${value.succeeded||0}/${p.total_pages||"待检测"} 页`;
    if(value.failed)text+=`（失败 ${value.failed} 页）`;
    if(value.current_page&&pending&&!interrupted)text+=`（检查第 ${value.current_page} 页）`;
    if(value.paused)text+=value.running ? "（正在保存当前页／区域后暂停）" : "（阶段已暂停）";
    else if(value.waiting_reason)text+=`（${waiting[value.waiting_reason]||"等待处理"}）`;
    text+="。";
   }
   // 一个包可能覆盖多页，一页也可贡献多个包；因此另列，不能加进页数。
   const packets=p.parent_work||{};
   const packetTotal=["pending","running","succeeded","failed","cancelled"].reduce((n,key)=>n+(packets[key]||0),0);
   if(packetTotal){
    text+=` 跨页工作包 ${packets.succeeded||0}/${packetTotal}`;
    if(packets.pending)text+=`（待处理 ${packets.pending} 包）`;
    if(packets.running)text+=`（运行中 ${packets.running} 包）`;
    if(packets.failed)text+=`（失败 ${packets.failed} 包）`;
    if(packets.cancelled)text+=`（来源失效或已取消 ${packets.cancelled} 包）`;
    text+="。";
   }
   const assembly=p.assembly;
   if(assembly?.enabled){
    const reasons={reading:"等待基础读取",vision:"等待视觉结果",window:"等待窗口凑齐或定时保存",
     backpressure:"等待工作包处理",handoff:"正在交接收据",paused:"已暂停",
     stale_sources:"来源已变化，等待局部重建",incomplete_coverage:"材料范围尚未完整覆盖",
     legacy_receipt_unverified:"历史收据缺少完整来源记录",vision_artifact_missing:"缺少当前视觉结果",
     assembly_input_too_large:"单页超出组装预算",assembly_retry:"保存失败，等待有限重试",
     assembly_failed:"组装保存失败",assembly_claimed:"等待窗口执行",assembly_conflict:"等待重新核对检查点",
     index_version_changed:"索引配置已变化，等待重建",
     repair_replan_exhausted:"来源反复变化，重建次数已用尽",repair_step_exhausted:"重建重试次数已用尽",
     repair_baseline_changed:"重建基线已变化",repair_policy_changed:"识别策略已变化",
     repair_open_baseline:"历史组装尚未封口",repair_no_progress:"重建未能消除缺口",
     repair_recheck:"重新核对变化来源"};
    text+=` 跨页组装 ${assembly.saved_pages||0}/${assembly.total_pages||p.total_pages||"待检测"} 页`;
    if(assembly.failed)text+=`（${reasons[assembly.waiting]||"部分来源待处理"}）`;
    else if(assembly.waiting)text+=`（${reasons[assembly.waiting]||"等待处理"}）`;
    else if(assembly.closed)text+="（已封口）";
    text+="。";
    const repair=assembly.repair;
    if(repair){
     const phases={probing:"核对重建来源",starting:"创建重建计划",scanning:"定位变化窗口",
      preparing:"重算变化窗口",validating:"校验重建结果",adopting:"接纳重建结果",
      done:"重建已接纳",blocked:"重建已停止"};
     text+=` 局部重建：${phases[repair.phase]||"核对处理状态"}，已核对 ${repair.checked_pages||0} 页`;
     if(repair.attempts)text+=`，计划尝试 ${repair.attempts} 次`;
     if(repair.error_code)text+=`（${reasons[repair.error_code]||"需检查来源或处理日志"}）`;
     if(repair.phase==="done")text+="，仍需核对工作包与来源覆盖";
     text+="。";
    }
   }
   text+="这些是阶段处理数量，不表示法律审查或识别质量已通过。";
  }else text+=` 已保存整批进度 ${p.processed_pages||0}/${p.total_pages||"待检测"} 页（不是有效索引覆盖）。`;
  // 批次检查点不是当前有效向量数量，尤其不能当作识别质量或全文审查完成度。
  if(p.scheduler!=="stages-v1"&&p.current_page&&p.stage)text+=` 检查位置：第 ${p.current_page} 页 · ${stages[p.stage]||p.stage}。`;
  if(p.scheduler!=="stages-v1"&&p.current_page&&p.current_page<=p.processed_pages&&pending&&!interrupted)text+="正在重检已保存页面，优先复用同版本结果。";
  if(job.error_code)text+=" 本地处理异常。";
  return {text,pending,active:pending&&!interrupted,resumeAvailable,startHidden:resumeAvailable||job.status==="pause_requested"};
 }
 if(typeof module!=="undefined"&&module.exports)module.exports={processingView};
 else root.processingView=processingView;
})(globalThis);
