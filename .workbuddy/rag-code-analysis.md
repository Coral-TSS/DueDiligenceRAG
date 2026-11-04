# 尽调报告 Agent（RAG 应用）逐文件代码分析

> 分析范围：`src/dd_agent/` 全部 Python、worker、前端静态文件、SQL 迁移与配置档案（约 2700 行）。  
> 分析时间：2026-10-23。

## 一、这是什么

一个**本地（离线、单人、CPU）的尽调材料工作区 + 证据检索（RAG）应用**，业务首轮聚焦"公司基本情况与历史沿革"。端到端链路是：

```
上传 PDF → 内容摘要去重登记 → PDFium 提取原生文字/渲染页面图
        → 扫描页走 PaddleOCR(PP-OCRv5) 基础识别
        → 按章节画像定位重要区域 → 调用 PaddleOCR-VL 视觉模型局部增强
        → 机器两路文字一致性/数值/身份校验（不可消解则标"未知"）
        → 有效文字按页分块(700字/重叠80) → Qwen3-Embedding-0.6B 向量化(1024维)
        → pgvector 入库
查询：问题向量 → 余弦召回 top12 → Qwen3-Reranker-0.6B 重排 → top5 候选证据（含来源页链接）
```

**注意：它目前只做到"证据检索"，没有生成报告/LLM 回答环节**；`harness`（DeepSeek Harness SDK）只是运行基线探针，业务 MCP 工具与报告生成是下一步（C-T02）。

## 二、核心架构特点

1. **多进程/多虚拟环境隔离**：主应用、PDF、OCR、视觉、检索、Harness 各用独立 venv，主进程不 import 任何模型库；通过 `worker_records()` 用"JSON 清单文件 + stdout 逐行 JSONL"协议驱动子进程 worker。
2. **全链路版本化**：阅读版本 = 管线版本 + OCR 权重逐字节摘要 + 渲染比例；增强版本 = 画像 + 选择规则 + 视觉模型指纹 + 区域预算；索引版本 = 分块规则 + Embedding 指纹。版本不匹配的旧结果保留但不会被当前检索返回。
3. **三级并发控制**：① `pg_advisory_lock 74332103` 跨进程队列执行租约；② `74332104` 入队/暂停/继续串行化；③ `model_slot()` 文件锁保护本机模型内存（视觉、Embedding、Rerank 共享）。
4. **可暂停/可恢复**：暂停在页/区域安全检查点生效，先保存再检查；中断任务启动后转"暂停"等用户显式继续，不默默重跑；视觉区域候选按 (页, 修订, 坐标, 版本) 缓存复用。
5. **保守的证据观**：大量防御性校验（向量维度/NaN/零向量、OCR 三元组对齐、rerank 分数范围、发布前 FOR UPDATE 重比修订）；机器一致 ≠ 事实核实，全程在 UI 与代码注释中强调。
6. **安全边界**：仅监听 127.0.0.1、TrustedHost、CSP、跨站写请求拒绝、存储路径防目录穿越、worker 环境变量白名单（不带数据库凭据）。

## 三、Python 模块（src/dd_agent/）

| 文件                                    | 行数  | 职责                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------------- | --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`                              | 280 | FastAPI Web 入口。定义全部 REST 路由（项目 CRUD、上传、下载、页面图、阅读状态、检索、自动处理启停/暂停/继续、material-catalog 分页）；`UploadRequestLimit` 中间件在流入口限上传体积；防跨站写、安全响应头、数据库/文件系统异常统一转 503。**不跑模型**，只做参数校验、项目边界和路由分发。                                                                                                                                                                       |
| `pipeline.py`                         | 237 | 总调度。`ProcessingPipeline.run()` 固定顺序执行 基础阅读→自动增强→向量索引 三阶段；`JobManager` 是数据库-backed 可恢复队列：后台线程每 3 秒 `drain()`，用会话级 advisory lock 拿执行权，每 5 页一批，`_checkpoint()` 在安全点响应暂停/停止，`_recover_orphans()` 把失去执行者的任务收束为 paused，`_save()` 用 CASE 防止旧进度覆盖暂停状态。三类锁的分工有明确注释。                                                                                              |
| `retrieval.py`                        | 284 | **RAG 核心**。`split_text()` 按段落优先、700 字符上限、80 字符重叠分块（start/end 可回查原文）；`EvidenceService.index()` 逐页流式 embed 后原子发布到 `indexed_pages`+`evidence_chunks`（发布前 FOR UPDATE 锁行并重比 source_revision）；`search()` 两阶段检索：余弦召回 12 → rerank 0–1 分 → 取 5，返回前再剔除来源已失效的块；`CURRENT` SQL 片段是"当前有效页"的统一过滤条件；`model_slot()` 是跨进程文件锁；`LocalModels` 把推理请求打包成清单交给 retrieval_worker。 |
| `automatic.py`                        | 232 | 自动增强层。`route_page()` 按画像关键词（含 OCR 别名纠错、文字不足兜底）判断该页是否值得增强；`regions_for()` 用 OCR 行框 + 上下文窗口换算 0–1 归一化裁剪坐标，超预算区域显式标 `over_budget`；`choose_region()` 保守决策：候选缺失/截断→未知；年份有限修复后比对**数值、单位、身份标签、日期合法性、相似度≥0.72**，任一不通过→整区域掩为"〔该区域信息无法可靠识别〕"；`AutomaticProcessor.process()` 复用同版本缓存候选、从后向前回填文字、锁源页追加 `machine_pages`（不改原始 OCR），未知最多尝试两轮。                          |
| `reading.py`                          | 160 | 基础阅读层。`parse_pages()` 解析 `1-5,8` 页码（单批≤20）；`normalize_ocr()` 把 Paddle 输出严格对齐成 text+lines(text/score/box)；`worker_records()` 是**通用 worker 子进程协议**（清单 JSON 入、JSONL 出、1200s 超时、环境变量白名单、异常时 kill 子进程防模型内存泄漏）；`Reader.read()` 计算阅读版本（OCR 权重逐文件摘要），PDFium 文字页直接保存、OCR 页集中一次 worker 调用，每页先提交再检查暂停；失败只补失败记录，已存页保留。                                              |
| `db.py`                               | 160 | psycopg 直连 SQL 数据访问层（无 ORM，`dict_row`）。覆盖 projects/documents/document_sources（同名多来源留档）、阅读版本切换、页保存、覆盖统计、键集分页 material_catalog、按 (project_id, sha256) 冲突去重登记；`health()` 核对 schema 版本=6；`migrate()` 用 advisory lock 幂等执行迁移 SQL。部分方法只收 document_id，项目归属校验依赖服务层。                                                                                             |
| `__main__.py`                         | 133 | CLI 入口（`python -m dd_agent`）。`serve` 启动 FastAPI；`init-db` 幂等迁移；`check` 健康检查；`harness-check` 环境基线；诊断命令 `inspect-pdf / import-file / read-pages / index-pages / search`；`process-pages` 是唯一跑完整三阶段链路的 CLI（不受网页暂停按钮控制）。                                                                                                                                     |
| `review.py`                           | 122 | 历史命名遗留：现在是**视觉候选的调用与持久化服务**。`LocalVision.profile()` 对视觉权重逐字节摘要生成指纹；`LocalVision.recognize()` 启动 vision_worker（限 256 新 token、显式 truncated 标记）；`ReviewService.recognize()` 调用前后各校验一次页面修订未变（防止旧图绑定新 OCR），先插 running 记录、在 model_slot 锁内推理、成功/stale/failed 状态落库；`state()` 返回旧人工 adoption 历史（只读留档）与视觉候选。                                                      |
| `workers/retrieval_worker.py`         | 119 | 独立 venv 中的 Qwen 推理进程。`profile()` 全量读权重文件算指纹（RG-05 待优化 I/O）；`embed()` 左 padding 取末 token hidden state L2 归一化，批 4、超 4096 token 报错不截断；`rerank()` 用 yes/no 首 token softmax 出 0–1 相关分（批 2 控制全词表 logits 内存）；查询向量追加 `Instruct:` 前缀而资料片段不加。不访问数据库。                                                                                                              |
| `workers/vision_worker.py`            | 102 | PaddleOCR-VL 区域 OCR。裁剪前校验 0–1 坐标和 1200 万像素上限；逐字节算模型/推理配置指纹；尊重 checkpoint 的 `tie_word_embeddings=False`；eager attention、fp32、CPU；只解码生成部分，**达到 token 上限且无 EOS 时显式 truncated=true**（主进程会拒用截断候选）；附 Windows ctypes 峰值内存统计。                                                                                                                                   |
| `workers/pdf_worker.py`               | 75  | PDFium worker。`inspect` 模式遍历全页返回页数+字符数+路由；`read` 模式提取文字并渲染 PNG 预览；`text_route()` 用长度、替代字符、异常字符占比、中英文组成判定 text/ocr。                                                                                                                                                                                                                                      |
| `workers/ocr_worker.py`               | 63  | PaddleOCR PP-OCRv5 基础 OCR（检测/识别/文档方向/文本行方向 4 个模型）。缺权重时逐页报错不自动下载；Paddle 日志 redirect 到 stderr 保护 stdout JSONL 协议；方向校正后的图存回 image_path 供后续区域裁剪。                                                                                                                                                                                                            |
| `workers/harness_worker.py`           | 59  | DeepSeek Harness 基线探针。校验 SDK/CLI/Node 版本钉死（0.2.1rc2 / v22.19.0）、home 与 workspace 目录不重叠，然后 initialize + get_working_directory，**不发送任何模型 prompt**；错误只暴露错误码不泄露环境信息。                                                                                                                                                                                        |
| `config.py`                           | 73  | `Settings` 冻结 dataclass：数据库 URL、数据目录、各 venv 解释器路径、模型目录、超时/预算等全部集中，`from_env()` 读 `.env`，`__post_init__` 范围校验，`repr=False` 防凭据外泄。                                                                                                                                                                                                                        |
| `storage.py`                          | 76  | 原件存储：流式写临时文件 + SHA-256、大小上限、PDF 头/扩展名筛查、`documents/{project}/{sha}.bin` 内容寻址（硬链接发布、不覆盖）、文件名清洗、`path()` 防目录穿越。                                                                                                                                                                                                                                           |
| `materials.py`                        | 18  | `MaterialCatalog`：构造时绑定项目、调用时再限分页参数，为未来 MCP 工具复用的项目边界最小原型。                                                                                                                                                                                                                                                                                              |
| `agent_runtime.py`                    | 30  | `check_harness()`：把 harness 路径配置打包成清单交给 harness_worker，60 秒超时；ready 不代表报告能力可用。                                                                                                                                                                                                                                                                          |
| `quality.py`                          | 30  | 离线字段评测辅助：按类别统计 exact_accuracy / prediction_coverage，reference=None 永不算通过，拒绝重复 ID——明确的"机器自评不算数"原则。                                                                                                                                                                                                                                                       |
| `control.py`                          | 12  | 定义 `ProcessingPaused` / `ProcessingStopped` 两个控制流异常，把用户暂停/服务停止从深层回调传回 JobManager，禁止被普通识别失败的 except 吞掉。                                                                                                                                                                                                                                                  |
| `__init__.py` / `workers/__init__.py` | 1   | 空包标记。                                                                                                                                                                                                                                                                                                                                                   |

## 四、数据库迁移（migrations/，当前版本 6）

| 文件                       | 内容                                                                                                 |
| ------------------------ | -------------------------------------------------------------------------------------------------- |
| `001_initial.sql`        | schema、schema_version、projects、documents（(project_id,sha256) 唯一）、document_sources                  |
| `002_reading.sql`        | document_readings（每材料一个当前阅读版本）、document_pages（按版本存页，主键含 pipeline_version）                          |
| `003_evidence_index.sql` | 启用 pgvector；indexed_pages（页级发布记录）+ evidence_chunks（1024 维向量、offset 可回查、外键级联）                       |
| `004_review.sql`         | page_adoptions（旧人工原型，只读留档）、vision_candidates；effective_document_pages 视图 v1                        |
| `005_automatic.sql`      | machine_pages（机器增强历史）；effective_document_pages 视图重写为"机器文字优先、修订匹配才生效"；processing_jobs 队列表（活动任务唯一索引） |
| `006_pause_resume.sql`   | 状态机扩展 pause_requested/paused；活动唯一索引覆盖暂停态；vision_candidates 加 profile_version                       |

**关键视图** `effective_document_pages`：机器增强文字只在 `machine_pages.base_revision` 与当前基础页修订指纹匹配时生效，否则回退基础文字——这是"来源变更立即排除旧结果"的数据库层保证。

## 五、前端（static/）

| 文件                                         | 职责                                                                                                                      |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `index.html` + `app.js` + `styles.css`     | 材料工作区：健康检查横幅、项目创建/切换、逐个上传（单个失败不中断）、材料清单（去重来源名、阅读进度徽章）、原件下载。用 generation 计数丢弃切换项目后返回的旧响应。                                |
| `reader.html` + `reader.js` + `reader.css` | 自动处理与识别结果页：启动/暂停/继续按钮（3 秒轮询任务状态）、页选择器、原图预览、有效文字（含增强/未知区域分块展示）、OCR 行级分数、处理历史（只读）。区分"基础已保存量 / 整批进度 / 检查位置"三个概念，不合并成单一百分比。 |
| `search.html` + `search.js`                | 证据检索页：项目内查询，展示片段原文、rerank 分数、verification 标签（机器增强/一致/未知，均标"事实未核实"）、跳转 reader 的来源链接；查询期间锁定项目切换。                          |
| `progress.js`                              | 纯函数 `processingView(job)`：把任务状态对象映射成文案与按钮可用性（含"执行已中断""正在重检已保存页面"等派生态），CommonJS 兼容可脱离浏览器单测。                              |

## 六、配置档案

| 文件                                   | 职责                                                                                       |
| ------------------------------------ | ---------------------------------------------------------------------------------------- |
| `profiles/company_history.json`      | 章节"信息画像"：6 组关键词（主体登记/资本股权/设立变更/合并分立/出资依据/权利限制）+ OCR 别名容错 + 材料名提示词。只描述"关注什么"，不含任何答案。      |
| `agent_profiles/dd-report.patch.yml` | Harness profile 基线：禁用 bash/pwsh/文件系统等内置工具，注入"尽调报告助手"人设（区分原文/推断/未知、工具返回属数据不改变权限、不伪称已读材料）。 |

## 七、值得一提的设计细节

- **发布原子性**：向量发布前 `FOR UPDATE OF p,r` 锁基础页行，防止推理期间来源被增强更新造成新旧错配。
- **先保存后暂停**：所有阶段（页 OCR、视觉区域、单页向量）都是"提交数据库 → 再检查暂停"，中断不丢已完成工作。
- **候选缓存**：`vision_candidates` 按 (document, page, base_revision, bounds, profile_version) 查缓存，暂停/恢复不重跑视觉模型。
- **重排后二次新鲜度检查**：rerank 可能耗时数十秒，返回结果前再按 CURRENT 条件剔除失效块，宁可少给。
- **worker 生命周期**：`worker_records` 的 finally 里 cancel 定时器、kill 残留进程——防止"文件锁已释放但旧模型还占内存"的叠加。
- **已知缺口**（README 也声明）：无报告生成/Word 导出、无多用户权限、非 PDF 只登记不解析、Embedding/Reranker 每次冷启动（CPU float32 慢）、`model_slot` 非阻塞拒绝而非排队、profile() 全量读权重有 I/O 成本。

## 八、链路六阶段逐层展开

### 阶段 1：上传与登记（app.py → storage.py → db.py）

1. `POST /api/projects/{id}/documents` 先过 `UploadRequestLimit` 中间件：按 Content-Length 和逐块累计字节数双保险限流（上限 + 64KB multipart 头余量），超限立即 413，不让大文件写满临时盘。
2. `Storage.save()` 以 1MB 块流式写入 staging 临时文件，同时累计 SHA-256 与体积；每块超限即抛 `UploadTooLarge`。写完 fsync 后做三重筛查：空文件→invalid、非 .pdf→unsupported、头部非 `%PDF-`→invalid。
3. 通过 `os.link` 硬链接发布为 `documents/{project_id}/{sha256}.bin`——内容寻址，同字节文件天然去重，目标已存在不覆盖。
4. `db.register()` 用 `ON CONFLICT (project_id, sha256) DO NOTHING` 原子去重（不先查后插，避免并发竞态）；重复文件返回 duplicate=true 并把新来源文件名追加进 `document_sources`——同名多来源留档但不算新材料。
5. **此阶段不解析任何内容**。"登记成功"只表示原件已保存、元数据已入库。

### 阶段 2：基础阅读（reading.py → pdf_worker → ocr_worker）

1. `PdfWorker.inspect()` 先预检：遍历全部物理页，返回页数 + 每页字符数 + 路由判定。`text_route()` 规则：去空白后 <16 字符或含 `\ufffd` → ocr；异常字符占比 >2% → ocr；中文 ≥10 字或拉丁占比 >85% → text，否则 ocr。损坏/加密 PDF 在这里就报错。
2. **阅读版本计算**：`PIPELINE_VERSION + SHA-256(4 个 Paddle 模型文件逐字节摘要) + 渲染比例 + force-ocr 标记`。OCR 换权重、改渲染比例都会产生新版本——旧版本页面保留但不计入当前进度。
3. `Reader.read()` 只处理"当前版本未成功"的 pending 页：pdf_worker 逐页提取文字 + 渲染 PNG 预览（scale=2，文字页也渲染）；判定为 ocr 的页集中收集，一次 ocr_worker 调用复用同一个 Paddle 实例（含方向分类模型）。
4. `normalize_ocr()` 用 `zip(strict=True)` 强制 rec_texts/rec_scores/rec_polys 三元组一一对应，任一异常整页报错——不静默丢弃坐标。
5. **每页 persist：先 `save_page` 提交数据库，再调 checkpoint 回调**。此时暂停，刚完成的页不丢。worker 中途失败，只为剩余页补 failed 记录，已保存页保留。

### 阶段 3：自动增强（automatic.py → review.py → vision_worker）

1. **增强版本** = sha256(画像 JSON + 选择规则版本 + 视觉权重逐字节指纹 + 每页区域预算)。
2. **跳过逻辑**：同 base_revision + 同 profile_version 已有结果且（outcome≠uncertain 或已试 ≥2 轮）→ skip；未知结果最多重试两轮，不无限重跑。
3. `route_page()`：OCR 别名替换（"注册资木"→"注册资本"）→ 按画像 6 组关键词匹配 → 命中=important；文字为空/含替代字符/过短且文件名含"工商、章程"等提示 → poor 兜底。两者都触发 needs_vision（仅 OCR 页）。
4. `regions_for()` 定位：在 page text 中按序 find 每行 OCR 文字得到字符偏移 → 找出"重要行"（逐行重跑 route_page）→ 每个重要行取前后各一行组成上下文窗口、相邻窗口合并（间隔 ≤1 行且跨度 <10 行）→ 用该窗口内全部行框四点坐标 + 12px 外扩换算 0–1 归一化裁剪框。**定位失败退回整页，不是跳过该页**；超预算（>3 个）窗口仍返回并标 `over_budget`，下游标未知。
5. 视觉调用链（`ReviewService.recognize`）：先校验页面修订未变 → 插入 `vision_candidates(running)` → 在 `model_slot` 文件锁内启动 vision_worker（裁剪、fp32、eager、限 256 新 token、显式 truncated 标记）→ 调用后再查一次修订，变了则状态记 stale 而非 succeeded。
6. **候选缓存**：同 (document, page, base_revision, bounds, profile_version) 的成功非截断候选直接复用——暂停/恢复不重跑视觉模型。
7. `choose_region()` 决策树：候选缺失/截断/空 → unknown（掩码）；缺位年份仅允许"数字子序列对齐"的有限修复；然后五重检查——**数值序列相等（Decimal，含千分位归一）、货币/百分比单位序列相等、身份标签值相等（企业名称/法定代表人/统一社会信用代码等 9 种标签）、视觉侧日期全部合法、归一化相似度 ≥0.72**。任一失败 → 整区域掩为"〔该区域信息无法可靠识别〕"；全等 → consistent（保留基础文字）；否则 → enhanced（采用视觉文字）。
8. 回填：区域按 start 倒序替换（避免前段长度变化破坏后段偏移），结果整体追加进 `machine_pages`——**追加新记录，不改写原始 OCR**。写库前 FOR UPDATE 锁源页行并重比修订，推理期间来源变了就整页记失败。

### 阶段 4：向量索引（retrieval.py → retrieval_worker）

1. 索引版本 = sha256(CHUNK_VERSION + Embedding 权重指纹)。分块规则换、Embedding 换，都自动是新索引。
2. `index()` 取 `effective_document_pages` 的当前有效文字快照（可能含增强文字），跳过当前版本已索引页，单页 >100000 字符直接失败。
3. `split_text()`：700 字符上限，优先在 350–700 之间的最后一个换行处分块；硬切长段落时下一块回退 80 字符重叠；记录 start/end_offset，可直接 `text[start:end]` 回查原文。当前不跨页、不做语义分块。
4. `embed_pages` 流式：一个 retrieval_worker 进程加载一次 Qwen3-Embedding，逐页输出向量；主进程**逐页 publish（独立事务）**——第 N 页失败不回滚已提交的 N-1 页。
5. `publish()` 原子性：`FOR UPDATE OF p, r` 锁基础页行 → 重比 source_revision → DELETE+INSERT 该页同版本 indexed_pages → 逐块写 evidence_chunks（1024 维，pgvector）。向量先过 `valid_vector`：维度、有限值、非全零。
6. 每页 publish 后调 checkpoint；worker 崩溃时已提交页存活，剩余页记 failed。

### 阶段 5：检索（retrieval.py search）

1. 先给覆盖摘要：项目总页数、未预检材料数、当前版本已索引页数——这是 UI 上"可检索 X/已知 Y 页"的来源。
2. 查询向量加 `Instruct: ...` 检索指令前缀（资料片段不加——Qwen3-Embedding 的非对称用法）。
3. **两阶段检索**：余弦相似度召回 top12 → rerank（Qwen3-Reranker 以 causal LM 比较 yes/no 首 token 的 softmax，输出 0–1 相关分，批 2 控制全词表 logits 内存）→ 取 top5。
4. 分数语义严格声明：similarity 和 rerank_score 都**不是事实为真的概率**，排序后仍需回读原件核对。
5. rerank 可能耗时数十秒，返回前再按 CURRENT 条件重查候选 chunk id，剔除来源失效的块——宁可少给不给出错。
6. `CURRENT` SQL 是关键不变量：`indexed_pages JOIN effective_document_pages ON pipeline_version + updated_at + source_revision 全部匹配且 status='succeeded'`——建库跳过和查询过滤共用同一条件，旧版本向量即使还在表里也永远不会被返回。

### 阶段 6：调度与暂停/恢复（pipeline.py）

1. **入队**：`pg_advisory_xact_lock(74332104)` 串行化 → 查活动任务（queued/running/pause_requested/paused 都算）→ 没有才插入 queued。部分唯一索引 `processing_job_active` 在数据库层兜底"每材料同时最多一个活动任务"。**暂停任务不能通过再点"自动处理"绕开**。
2. **执行**：后台线程每 3 秒 `drain()`；`pg_try_advisory_lock(74332103)` 是会话级锁，跟连接走，拿不到说明其他应用进程在跑，直接返回。
3. **孤儿收束**：拿到执行权后 `_recover_orphans()` 把所有 running/pause_requested 任务转为 paused 并写入 recovery 元数据（reason=worker_lost）。**判断依据是锁，不是任务多久没心跳**——慢任务不会被误杀。
4. **批处理**：每批 5 页，从第 1 页开始全量重检（不是从 processed_pages+1 续跑）——暂停期间源文件/模型版本可能变了，各层按版本自动跳过未变页，变了就重建。这就是"继续处理时 current_page 可能回到较早页"的原因。
5. **检查点协议**：`_checkpoint()` FOR UPDATE 查任务状态，pause_requested/paused → 抛 `ProcessingPaused`，服务停止 → `ProcessingStopped`；两者由 drain 单独捕获收束状态，**绝不能被识别失败的 except 吞掉**（control.py 的注释反复强调）。正在执行的单次模型计算不会被瞬间冻结——视觉区域受 300 秒超时约束自然结束。
6. **状态保护**：`_save()` 用 CASE——旧线程的 running 进度不能覆盖新的 paused 状态，但进度本身仍保存。
7. 正常关闭：`close()` 发协作停止信号 → 任务在安全点存为 paused（recovery=service_stopped）→ 重启后不默默重跑，等用户点"继续"。

## 九、架构特点详解

### 9.1 多进程隔离与 worker 协议

为什么每个模型一个 venv？Paddle、Transformers、PDFium 的依赖互相冲突且都重。主应用进程**只负责编排**，永不 import 模型库。统一协议在 `reading.worker_records()`：

- 输入：临时目录里的一个 JSON 清单（模式 + 模型路径 + 数据）；
- 输出：stdout 逐行 JSON（一行 = 一页/一区域结果），stderr 重定向到本地日志；
- 生命周期：1200 秒定时器到点 kill；finally 里 cancel 定时器、关闭管道、确认回收——**防止"文件锁已释放但旧模型进程还占着内存"的下一次调用内存叠加**；
- 环境白名单：只传 SYSTEMROOT/PATH/TEMP 等系统变量，**数据库凭据和业务配置不进 worker**；
- 不只信第一行输出：`LocalModels.one()` 要求进程完整退出且恰好一条结果，"成功打了一行然后崩溃"不算完成。

### 9.2 版本化体系（三层版本环环相扣）

| 版本 | 绑定内容 | 失效效果 |
| --- | --- | --- |
| 阅读版本 | 管线名 + OCR 4 个权重文件逐字节摘要 + 渲染比例 + force-ocr | 换 OCR 权重 → 全部页面重新阅读，旧页留档 |
| 增强版本 | 画像 JSON + 选择规则 + 视觉权重指纹 + 区域预算 | 换视觉模型/规则 → 机器结果不复用，重新增强 |
| 索引版本 | 分块规则版本 + Embedding 权重指纹 | 换分块/Embedding → 旧向量不被检索返回 |

链条：`evidence_chunks` 外键挂 `indexed_pages`，`CURRENT` 又要求 indexed_pages 的 pipeline_version/source_revision 与 effective_document_pages 一致——**从 OCR 权重到向量是一条可追溯的版本链**，任何一环变化，下游结果自动"退出版本"而不是错误混入。修订指纹（raw_revision = md5(管线版本:页更新时间)）是链上的铆钉。

### 9.3 并发模型（三种锁，各管一件事）

1. **进程内 mutex**（JobManager._mutex）：只防止本进程重复起后台线程。
2. **PostgreSQL 74332103**（会话级）：跨应用进程的队列执行租约，贯穿整个 drain()。多个应用进程共享一个处理队列的机制就在这里——连接断开数据库自动释放，天然处理进程崩溃。
3. **PostgreSQL 74332104**（事务级）：入队/暂停/继续这些"控制操作"的短事务串行化，防止并发控制请求产生交错状态。
4. **model_slot 文件锁**（retrieval.py）：保护本机 CPU/内存预算——视觉推理、Embedding、Rerank、查询共用。当前是**非阻塞拒绝**（拿不到就提示"稍后重试"），没有等待队列，这是体验上的已知短板。

### 9.4 事实保守主义（贯穿全链路的设计哲学）

- 机器两路文字一致 → 标 `machine_checked`，UI 明示"事实未核实"；
- 校验不过 → 整区域掩码为未知，**宁可丢信息也不让可能错误的姓名/金额进证据**（注释专门解释：字段值常延续到下一行，保留残留行会泄露被拒身份）；
- 未知区域最多试两轮，超预算明确标未知而不是假装已覆盖；
- rerank 分数、OCR 分数都只是排序/质量线索，代码和 UI 全部拒绝把它们解释为正确率；
- `quality.py`：reference=None 的字段永远不算通过，模型自评不算数。

### 9.5 安全边界（本机单人版的最低防线）

仅监听 127.0.0.1；TrustedHost 白名单；非 GET/HEAD/OPTIONS 请求校验 Origin 与 Sec-Fetch-Site（防其他网页跨站触发写操作）；CSP `default-src 'self'` 等全套响应头；storage.path 防目录穿越；worker 环境变量白名单；异常只返回错误码/友好文案，不泄露凭据与原始堆栈。代码注释明确：**这不能替代多用户授权体系，不要直接暴露成公共服务**。

### 9.6 PyCharm 运行说明（2026-10-23 补充）

本项目是 src 布局 + 包内相对导入，`__main__.py` 不能作为脚本直接右键运行（会报 `ImportError: attempted relative import`）。正确方式是按模块运行 `python -m dd_agent`。已在 `.idea/runConfigurations/dd_agent.xml` 预置两个配置：**dd-agent serve**（启动 Web）与 **dd-agent check**（健康检查），重载 PyCharm 后在右上角运行配置下拉里选择即可；解释器需指向 `.venv-dev`，工作目录为项目根。
