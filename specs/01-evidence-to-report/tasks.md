# 项目任务清单

更新：2026-11-08。基线是默认分支 `6bb8c8e`（PR #10 已合并，其后无新提交）。本轮只整理文档，没有实现授权，不改业务代码、测试或数据库。

本文件只有下面这一张表。状态、顺序和依赖只改这张表，不再另开章节、阶段表或“下一步”列表。细节见 [spec.md](./spec.md)、[plan.md](./plan.md)、[project-center-spec.md](./project-center-spec.md)、[project-center-plan.md](./project-center-plan.md)、[project-center-test-design.md](./project-center-test-design.md)。回归数字和过程在 [tasks-history.md](./tasks-history.md)；那份说明**不是**任务清单，也不维护当前状态。

读表顺序：待做、待核实、待确认、暂缓、放弃、移交、已完成。同一状态内看“顺序”。工程完成不等于客户整卷或真实模型质量验收。迁移文件到 `017_chunk_spans.sql`，健康检查要求 schema 17。

| 顺序 | ID | 目标 | 状态 | 依赖 | 验收／证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | PC-ISO | 共享库上补项目隔离回归：A／B 同名、相同内容，向量、关键词、混合、父块、缓存、补读、MCP 越界和异步任务 | 待做 | 无。2026-11-08 仍无实现授权 | [规格 C](./project-center-spec.md)、[TD-C](./project-center-test-design.md)。已正确行为直接作回归，不伪称先红 |
| 2 | PC-UI | 项目列表到工作台、进度、检索、补读共用显式当前项目；无项目不搜索、不默认最新项目；切换、刷新和迟到响应不串项目 | 待做 | 无。与 PC-ISO 可并行。别名：显式当前项目、切换与旧响应 | [规格 A](./project-center-spec.md)、[TD-A](./project-center-test-design.md) |
| 3 | RG-02 | 当前项目的覆盖缺口清单：未处理、失败、未知、空文本、待重建。别名 C-T02-b 覆盖部分 | 待做 | PC-UI | [RS-02](./spec.md)、[TD-D01](./project-center-test-design.md)。搜索摘要已有，不等于本项完成 |
| 4 | RG-03 | 按证据标识或来源范围补读，并提供必填项目的 MCP 工具。别名 C-T02-b 补读部分 | 待做 | PC-UI。可与 RG-02 并行 | [RS-03](./spec.md)、[TD-D02](./project-center-test-design.md)、[TD-D03](./project-center-test-design.md)。单页和父块 HTTP 已有，不计入本项完成 |
| 5 | RG-06 | 当前项目内的材料与来源范围过滤（HTTP 与页面）。别名：RG-06 其余。不含 RG-06a／b／c，也不含固定集 | 待做 | PC-UI | [RS-06](./spec.md)、[TD-D04](./project-center-test-design.md)。hybrid 已完成，见 RG-06c |
| 6 | RG-04 | 冻结开发／保留集并分开记录 OCR、召回、重排、来源和未知。至少 60 个可判读字段 | 待做 | 无。不阻塞 1–5。质量数字只来自真实模型运行 | [RS-04](./spec.md)、[B-Q](./spec.md#b-q-的首轮验收门槛)、[TD-D05](./project-center-test-design.md) |
| 20 | RG-07 | 本仓库只做 MCP 对接验收：遍历、覆盖、检索、补读、拒绝跨项目。编排和报告不在此行 | 待做 | RG-02、RG-03；对方教学 Bot 冻结调用（仓库外） | [RS-07](./spec.md)。检索 MCP 已交付，见 MCP-SSE |
| 7 | RG-05k | 核对渲染与 OCR 重叠是否已达到原定验收。2026-11-04 仍列为后续；代码有渲染计划与 OCR 等待，本轮未重跑 | 待核实 | RG-05b、RG-05e | [plan 的 RG-05k](./plan.md#rg-05k统一图像产物与坐标)。未重跑前不标完成 |
| 8 | RG-05l | 核对只读盘点 l1 与 8MB 日志备份 l2 的验收。别名 l1、l2 | 待核实 | 无 | `maintenance.py`。l3／l4 是 RG-05l3、RG-05l4，不并进本项完成 |
| 9 | RG-05f | GPU 整卷联合性能验收。2026-11-04 未见可替代的新整卷结论 | 待核实 | RG-06c、RG-05g。不依赖 RG-05j4、RG-05k、RG-05l3 | [plan 的 RG-05f](./plan.md#rg-05f联合验收与配置选择)。工程绿不能代替本项 |
| 30 | OBS-01 | 继续观察 Windows 上传路径。前缀分支已修，未证明是历史问题唯一根因 | 待核实 | 无 | [历史说明](./tasks-history.md#obs) |
| 31 | OBS-02 | 保留历史 Harness 启动超时观察。本仓库不恢复 Harness 进程 | 待核实 | 无 | 若做 RG-07，只记客户端能否连上现有 MCP |
| 32 | OBS-03 | GPU 上复测或排除调度等待。不靠加长测试等待关闭 | 待核实 | 无 | [历史说明](./tasks-history.md#obs) |
| 33 | OBS-04 | 继续观察测试库初始化、连接和关闭等待 | 待核实 | 无 | 运行目录里的 database-lifecycle.jsonl，不入库长文 |
| 10 | PC-FMT | 决定首批是否按 DOCX、再 XLSX、再 PPTX 解析。未确认不得实施，也不得改成已支持 | 待确认 | 无 | [规格 B](./project-center-spec.md)、[TD-B](./project-center-test-design.md)。建议顺序不是批准 |
| — | RG-05l3 | 清理预览。别名 l3 | 暂缓 | 无 | [plan 的 RG-05l](./plan.md#rg-05l可证明安全的回收)。不删除原件 |
| — | RG-05l4 | 回收无引用缓存。别名 l4 | 暂缓 | 无 | 同上 |
| — | JV-01 | 可选 Jev 重排：核实外发边界并冻结本地基线。不是入库加速 | 暂缓 | RG-04 | [Jev 实验](./plan.md#jev四组实验与融合算法) |
| — | JV-02 | 可选 Jev 重排适配；失败回退，默认仍用本地重排 | 暂缓 | JV-01 | 同上 |
| — | JV-03 | 对照直接替换、两级排序和排名融合 | 暂缓 | JV-02 | 同上 |
| — | JV-04 | 用保留集比较后才考虑启用 | 暂缓 | JV-03 | 同上 |
| — | FMT-LEGACY | 旧版 DOC／XLS／PPT、宏、密码文档和 LibreOffice 转换。必须明示不支持，不能静默跳过 | 暂缓 | 无 | [规格 B](./project-center-spec.md) |
| — | EXT-POST | 重要页语义、跨行漏字、复杂表格回补、主体时点、图像预处理、更大 OCR 池、独立图片、客户整卷。不自动升为待做 | 暂缓 | RG-04 只决定要不要升格 | [spec 后置范围](./spec.md)。PPT 导出的图片 PDF 仍走现有 PDF |
| — | SCOPE-DB | 不为每个项目建物理库或向量 collection | 暂缓 | 无 | [project-center-plan](./project-center-plan.md) |
| — | SCOPE-TENANT | 不做多租户身份鉴权。项目范围不是登录角色 | 暂缓 | 无 | [规格](./project-center-spec.md) |
| — | SCOPE-DEL | 不提供删除项目、删除材料或清空向量，除非另行确认 | 暂缓 | 无 | 同上 |
| — | RG-05j4 | 跨运行持久向量缓存，以及为此准备的 j4 合批实验。别名 j4 | 放弃 | 无 | 不恢复。[历史说明](./tasks-history.md#rg-05j) |
| — | JV-INGEST | 把 Jev 当作入库加速 | 放弃 | 无 | 不等于放弃 JV-01–04。[plan](./plan.md#jev官方信息核实与接入边界) |
| — | RPT | 报告生成、尽调 skill、两章 Word 和工作文件。别名 P1-C、C-T01 | 移交 | 无 | 由另一教学 Bot 做 DeepSeek Harness。不恢复 `harness-runtime/` |
| — | P1-PDF | PDF 项目、材料、去重、阅读、OCR、视觉、切块和语义重排。别名 P1-A、P1-B、B-T01、C-T02-a | 已完成 | 无 | [spec 的 P1-A／B](./spec.md)。限定工程，不是客户整卷 |
| — | RG-01 | 暂停、继续、执行锁和中断后保留成果 | 已完成 | 无 | [历史说明](./tasks-history.md#regression) |
| — | RG-05a | 阶段测量与两页真实模型基线 | 已完成 | 无 | 同上 |
| — | RG-05b | 预检、指纹缓存和空输入不启动嵌入 | 已完成 | RG-05a | 同上 |
| — | RG-05c | 持久阶段待办 | 已完成 | 无 | 同上 |
| — | RG-05d | 有界窗口、背压和阶段调度 | 已完成 | RG-05c | 同上 |
| — | RG-05e／RG-05i | 驻留模型、公平队列、取消、超时和查询调度。两个旧 ID 是同一已完成项 | 已完成 | RG-05d | [历史说明](./tasks-history.md#regression) |
| — | RG-05g | 有界页读取和页面刷新。别名 g1、g2 | 已完成 | 无 | 同上 |
| — | RG-05h | 短事务和共享连接池 | 已完成 | 无 | 同上 |
| — | RG-05j | 微批与同一页未改片段复用向量。别名 j1、j2、j3 | 已完成 | RG-05e、RG-06b | [历史说明](./tasks-history.md#rg-05j)。不含 j4 |
| — | RG-06a | 跨页父块组装 | 已完成 | 无 | [streaming-ingestion](./streaming-ingestion.md)。当时未接入活动索引 |
| — | RG-06b | 父子索引发布、恢复和受控 shadow。默认 off | 已完成 | RG-06a | 限定工程与两页真实模型。不是客户整卷 |
| — | RG-06c | 父块检索、补读和默认 mixed／hybrid。别名 c1、c2、c3、c4a、c4b、c4c | 已完成 | RG-06b | [历史说明](./tasks-history.md#rg-06c)、[README](../../README.md)。限定工程，不是整项 RG-06 |
| — | MCP-SSE | 只读 MCP：列项目、列材料、检索。不上传、不 OCR | 已完成 | 无 | [README](../../README.md)。不是 RG-07 对接验收 |
| — | PC-DOC | 项目中心规格、测试设计和方案 | 已完成 | 无 | PR #10。确认格式前不表示 PC-FMT 已批准 |
