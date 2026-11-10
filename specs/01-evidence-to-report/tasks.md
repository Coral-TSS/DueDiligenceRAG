# 项目任务清单

更新：2026-11-10。基线是默认分支 `938e662`。PC-ISO、PC-UI、PC-FMT 已在 PR #12 落地。RG-02、RG-03、RG-07 的仓库内部分和 RG-04 脚手架已有工程测试；本机与真实模型未验收。评测集和真实模型由用户在本机运行。RG-07 的教练 Bot Harness 冻结调用仍待对方确认。

本文件只有下面这一张表。状态、顺序和依赖只改这张表，不再另开章节、阶段表或“下一步”列表。细节见 [spec.md](./spec.md)、[plan.md](./plan.md)、[project-center-spec.md](./project-center-spec.md)、[project-center-plan.md](./project-center-plan.md)、[project-center-test-design.md](./project-center-test-design.md)、[material-metadata-spec.md](./material-metadata-spec.md)、[material-metadata-test-design.md](./material-metadata-test-design.md)。回归数字和过程在 [tasks-history.md](./tasks-history.md)；那份说明**不是**任务清单，也不维护当前状态。

读表顺序：待做、工程完成、本机验收待定、待核实、待确认、暂缓、放弃、移交、已完成。同一状态内看“顺序”。工程完成不等于客户整卷或真实模型质量验收。迁移文件仍到 `019_material_metadata.sql`，健康检查要求 schema 19。本轮没有新迁移。

| 顺序 | ID | 目标 | 状态 | 依赖 | 验收／证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | PC-ISO | 共享库上补项目隔离回归：A／B 同名、相同内容，向量、关键词、混合、父块、缓存、补读、MCP 越界和异步任务 | 工程完成、本机验收待定 | 无 | PR #12 提交 `aa974cd`。断言在 `testing/tests/test_project_isolation.py`。云端 `testing/run.py` 运行 `20261110-024545-0e8a9d`：573 项里 528 通过、29 失败、16 跳过。29 项失败都是缺少 Windows 视觉／OCR 工作进程，与本项无关。未做本机验收 |
| 2 | PC-UI | 项目列表到工作台、进度、检索、补读共用显式当前项目；无项目不搜索、不默认最新项目；切换、刷新和迟到响应不串项目 | 工程完成、本机验收待定 | 无。与 PC-ISO 可并行。别名：显式当前项目、切换与旧响应 | PR #12 提交 `0d35914`。断言在 `testing/tests/test_project_ui.py`。同上运行 `20261110-024545-0e8a9d`（528 通过、29 失败、16 跳过）。未做本机浏览器验收 |
| 3 | RG-02 | 当前项目的覆盖缺口清单：未处理、失败、未知、空文本、待重建。别名 C-T02-b 覆盖部分 | 工程完成、本机验收待定 | PC-UI | `GET /api/projects/{id}/coverage` 与工作台面板。断言在 `testing/tests/test_coverage_gaps.py`、`testing/scripts/frontend/coverage-gaps-regression.cjs`。同上运行 `20261110-024545-0e8a9d`（528 通过、29 失败、16 跳过）。未做本机验收。搜索摘要不算本项 |
| 4 | RG-03 | 按证据标识或来源范围补读，并提供必填项目的 MCP 工具。别名 C-T02-b 补读部分 | 工程完成、本机验收待定 | PC-UI。可与 RG-02 并行 | 证据标识与来源范围 HTTP，以及 MCP `read_evidence`。断言在 `testing/tests/test_evidence_read.py`。同上运行 `20261110-024545-0e8a9d`（528 通过、29 失败、16 跳过）。单页和父块旧接口不计入本项。未做本机验收 |
| 7 | RG-04 | 冻结开发／保留集并分开记录 OCR、召回、重排、来源和未知。至少 60 个可判读字段 | 工程完成、本机验收待定 | 无。不阻塞 1–6。质量数字只来自真实模型运行 | 只冻结清单、62 个空字段槽和报告脚本：`testing/quality/fixed_set/`、`testing/scripts/quality/run_fixed_set.py`。断言在 `testing/tests/test_fixed_set.py`。同上运行 `20261110-024545-0e8a9d`（528 通过、29 失败、16 跳过）。评测集和真实模型运行由用户在本机完成。仓库内没有质量数字，也没有客户数据 |
| 20 | RG-07 | 本仓库只做 MCP 对接验收：遍历、覆盖、检索、补读、拒绝跨项目。编排和报告不在此行 | 工程完成、本机验收待定 | RG-02、RG-03；对方教学 Bot 冻结调用（仓库外） | 仓库内合成客户端：`testing/tests/test_mcp_acceptance.py`，说明见 [mcp-client.md](../../docs/mcp-client.md)。同上运行 `20261110-024545-0e8a9d`（528 通过、29 失败、16 跳过）。教练 Bot Harness 的冻结调用仍待对方确认，不在本仓库 |
| 5 | MM-01 | 材料元数据：类别（默认表）、项目主体、日期／期间；单份与批量人工录入；列表与 MCP 投影。迁移 019。不含自动分类、可信度／管理字段；主体首批只改名不删除 | 工程完成、本机验收待定 | PC-UI | 云端 `testing/run.py` 运行 `20261109-142741-095200`：560 项里 515 通过、29 失败、16 跳过。29 项失败都是缺少 Windows 视觉／OCR 工作进程，与本项无关。未做本机或真实模型验收。规格见 [material-metadata-spec](./material-metadata-spec.md) |
| 6 | RG-06 | 当前项目内材料多选、PDF 页范围、类别／主体／日期过滤（HTTP、页面、MCP）。字段间 AND、字段内 OR；未填维默认排除并给计数。别名：RG-06 其余。不含 RG-06a／b／c、不含固定集、不含 Office 定位过滤（设页范围时排除无页码单元） | 工程完成、本机验收待定 | PC-UI、MM-01 | 同上运行 `20261109-142741-095200`（515 通过、29 失败、16 跳过）。断言在 `testing/tests/test_material_metadata.py`。未做本机或真实模型验收。hybrid 已完成，见 RG-06c |
| 7 | RG-05k | 核对渲染与 OCR 重叠是否已达到原定验收。2026-11-04 仍列为后续；代码有渲染计划与 OCR 等待，本轮未重跑 | 待核实 | RG-05b、RG-05e | [plan 的 RG-05k](./plan.md#rg-05k统一图像产物与坐标)。未重跑前不标完成 |
| 8 | RG-05l | 核对只读盘点 l1 与 8MB 日志备份 l2 的验收。别名 l1、l2 | 待核实 | 无 | `maintenance.py`。l3／l4 是 RG-05l3、RG-05l4，不并进本项完成 |
| 9 | RG-05f | GPU 整卷联合性能验收。2026-11-04 未见可替代的新整卷结论 | 待核实 | RG-06c、RG-05g。不依赖 RG-05j4、RG-05k、RG-05l3 | [plan 的 RG-05f](./plan.md#rg-05f联合验收与配置选择)。工程绿不能代替本项 |
| 30 | OBS-01 | 继续观察 Windows 上传路径。前缀分支已修，未证明是历史问题唯一根因 | 待核实 | 无 | [历史说明](./tasks-history.md#obs) |
| 31 | OBS-02 | 保留历史 Harness 启动超时观察。本仓库不恢复 Harness 进程 | 待核实 | 无 | 若做 RG-07，只记客户端能否连上现有 MCP |
| 32 | OBS-03 | GPU 上复测或排除调度等待。不靠加长测试等待关闭 | 待核实 | 无 | [历史说明](./tasks-history.md#obs) |
| 33 | OBS-04 | 继续观察测试库初始化、连接和关闭等待 | 待核实 | 无 | 运行目录里的 database-lifecycle.jsonl，不入库长文 |
| 10 | PC-FMT | 首批按 DOCX、再 XLSX、再 PPTX 解析。用户已确认并实施 | 工程完成、本机验收待定 | 无 | PR #12 提交 `2e6c00b`、`8435bb8`。实现在 `src/dd_agent/office.py`，断言在 `testing/tests/test_office_formats.py`。同上运行 `20261110-024545-0e8a9d`（528 通过、29 失败、16 跳过）。未做本机验收 |
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
| — | PC-DOC | 项目中心规格、测试设计和方案 | 已完成 | 无 | PR #10。格式实施见 PC-FMT |
