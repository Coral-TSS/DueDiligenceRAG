# 尽调报告 Agent

本地尽调材料工作区，当前实现 PDF 导入、分页阅读、基础 OCR、按报告需求自动视觉增强、本地向量检索与来源回看。首轮业务范围为 **公司基本情况与历史沿革**。报告生成、Word 导出与法律分析尚未实现。

## 启动

在工程根目录运行（现有环境无需重复安装）：

```powershell
.\.venv-dev\Scripts\python.exe -m dd_agent init-db
.\.venv-dev\Scripts\python.exe -m dd_agent check
.\.venv-dev\Scripts\python.exe -m dd_agent
```

打开 `http://127.0.0.1:8765`。默认只监听本机，按 Ctrl+C 停止。修改 `.env` 后重启。数据库当前迁移目标 **015**，初始化可重复执行，不删除原件、项目、识别结果或历史人工记录。升级时先停止旧后端，再运行上述 `init-db`；007 新增阶段任务／投递账本并为历史成功页补齐来源事件，008 新增分阶段暂停和独立失败计数；不重新运行 OCR 或模型。009–015 的内部跨页能力见下文。

首次部署：使用 Python 3.12 创建独立主环境，安装 `requirements-dev.lock.txt`，再以 `pip install -e . --no-deps` 安装工程；运行依赖也可用 `pip install -e .` 安装。模型使用各自独立环境，避免混合 Paddle／Transformers 依赖。具体环境说明见 [安装准备](specs/due-diligence-agent/windows-installation-guide.md)。

## 处理材料

1. 创建项目，导入 PDF。同项目相同内容去重，不同来源名留档；原件始终保留。
2. 打开“自动处理与识别结果”，点击“自动处理该材料”。后台按阶段调度，默认约 10 页触发下游，尾批或等待超时提前处理；可关闭页面，应用进程需保持运行。
3. 系统先提取可用原生文字；扫描页使用基础 OCR，再按章节画像匹配重要信息、自动选区调用本地视觉模型。
4. 结果自动进入有效文字与索引。数值、身份文字等差异无法消解时，该区域标为未知，继续其他区域／页面；不要求用户圈选、改字或采用候选。
5. 在“证据检索”中查询项目资料并回看来源。机器文字一致或自动增强不表示事实已经核实。

后台任务写入 PostgreSQL；同一材料不重复排入活动任务，多个应用进程通过数据库锁共享一个处理队列。2026-10-23 起，确认无执行者的中断任务在启动后保留成果并转为暂停，需要点击“继续处理”，不会因重启默默重跑；仍在排队的任务正常调度。判断依据是数据库执行锁，不仅看任务多久没更新。页面分开显示基础阅读已保存量、已保存整批进度与当前检查位置，后两者不等于有效索引覆盖。

页面支持“暂停处理／继续处理”，一次控制 OCR、视觉增强和向量化整条链路。点击后先显示“正在暂停”，当前页／视觉区域保存后才变为“已暂停”；正在执行的单次模型计算不会立刻中断，视觉区域仍受 300 秒等现有超时约束。暂停任务重启后仍保持暂停，点击继续才恢复。按钮控制应用中的后台任务；直接运行的 CLI 诊断命令不受这些按钮控制。继续按来源／策略版本从前页重检，优先复用同版本成果，保留批次进度和向量；未形成完整页的已成功视觉候选也可复用。仅完成一部分但尚未发布的单页向量会重算。

还可分别暂停／继续基础阅读、视觉增强和向量化。暂停某阶段后，其他阶段在依赖与积压预算内继续推进；当前操作保存后才停。页面分别展示成功、失败、等待资源等状态，页数表示成果数量。纯原生文字读取可以与下游重叠，OCR／视觉／向量仍共享一个重计算许可。

默认开启模型常驻复用：OCR、视觉、Embedding 和重排序共用一个驻留名额，相同模型的连续请求复用实例，换模型先释放前一个，空闲 30 秒释放。驻留期间保留跨进程资源锁；等待超时明确返回繁忙。设置 `DDA_MODEL_RUNTIME_ENABLED=0` 可回退一次性进程。此取舍控制内存，反复切换模型仍需重新加载，不保证每次检索都更快。

正常退出在安全检查点停止，并关闭模型运行时。Windows 常驻进程在加载前纳入所属作业对象，父进程突然退出、请求取消或超时会清理实际解释器。一次性回退保留原有清理边界。失败任务可再次启动；未知区域同一来源／策略最多尝试两次，超过区域预算的信息会明确标为未知。

扫描页先完成基础 OCR 和索引，材料可以先检索；视觉增强在整卷 OCR 之后集中进行，并回写、再嵌入发生变化的页。默认每页最多 1 个视觉区域，只处理低置信行或带数字、日期、金额的关键行，不再因整页出现关键词就把整页送进视觉模型。每个区域最多等待 300 秒，同一处理里超时达到 3 次后其余区域直接标未知。视觉生成固定打开 KV cache。安装与 GTX 1070／RTX 40 的推荐配置见 [GPU 安装](specs/due-diligence-agent/gpu-setup.md)。当前未完成客户整卷验收，建议先用下面的 10 页命令测量。

2026-10-23 已完成 RG-01／PR-05 中断恢复修复，回归 76 项通过、2 项真实模型测试跳过。实际材料持久化数量以应用和数据库当前状态为准，tasks.md 只维护开发进度，前三页仅指早期试点；完整当前覆盖与缺口清单是下一项 RG-02。验收和已知观察项见 [恢复记录](testing/records/recovery.md)。

## 小批命令

以下 ID 换成材料页面 URL 中的实际值：

```powershell
.\.venv-dev\Scripts\python.exe -m dd_agent process-pages --project-id <项目ID> --document-id <材料ID> --pages '1-3'
.\.venv-dev\Scripts\python.exe -m dd_agent search --project-id <项目ID> --query '注册资本及历次变更'
```

`process-pages` 可以一次指定整份材料，例如 `--pages 1-300`。它按阶段跑完：全部 OCR，再全部嵌入，再全部视觉，最后再嵌入一次。同一阶段只加载一次模型。网页上的“自动处理”走后台任务，同样按这个阶段顺序处理整卷，不需要拆成多次命令。`read-pages` 和 `index-pages` 仍是单批最多 20 页的诊断命令。`read-pages --force-ocr` 使用独立版本，用于开发诊断。

直接运行的 `search` 每次都是新进程，会重新加载向量和重排序模型，所以冷查询大约要十几秒。用 `python -m dd_agent` 启动的服务会把模型留在进程里，空闲约 30 秒后才释放，连续查询不用每次重载。

## 父子混合查询

默认 `DDA_SEARCH_INDEX_MODE=mixed`。未指定查询材料名单时，自动发现当前项目已封口的候选材料，每次最多检查 20 份；只有当前父子索引覆盖完整的材料才整份使用跨页查询，其余继续页级查询。超出上限会在页面提示，其余材料仍参与页级检索。没有父子成果时直接沿用页级查询，不额外加载父子 tokenizer／模型。

```dotenv
DDA_SEARCH_INDEX_MODE=mixed
# 留空自动发现；也可填最多 20 个材料 UUID，用英文逗号分隔以限定范围。
DDA_SEARCH_PARENT_DOCUMENT_IDS=
```

两路候选统一重排，最多 12 个候选、5 条结果。页面区分直接命中与连续上下文，展开时核对引用版本；来源变化会提示重新查询。模式选择不会启动 OCR、创建建库任务或改写已有索引。

建库由另一组配置控制：`DDA_PARENT_INDEX_MODE=shadow` 和 `DDA_PARENT_INDEX_DOCUMENT_IDS=<材料UUID>`，启动／继续对应材料后创建父子成果。后台建库仍默认 off，两组名单互不代替。材料 UUID 可从阅读页地址的 `document=` 获取。

如需完全回退页级查询，设置 `DDA_SEARCH_INDEX_MODE=page` 并重启；父子成果和有效引用保留，无需重建数据。模式配置在重启后生效，切换前先让在途后台安全保存。当前仍只有语义召回，关键词检索和全卷质量／性能验收保留后续。默认调整见 [验证记录](testing/records/default-mixed.md)，初版接入见 [历史验收](testing/records/search-rollout.md)。

## 配置和数据

`.env` 只在本机保存，不纳入版本控制，也不要在问题记录中粘贴密码。所有材料处理和检索均为本地执行，不会自动联网下载模型或调用云端模型。外网报告生成接口留待 P1-C。

| 配置 | 默认值／用途 |
| --- | --- |
| `DDA_DATABASE_URL` | 专用 PostgreSQL 数据库连接；需先安装 pgvector |
| `DDA_DB_POOL_SIZE` | 每个 Web／CLI 进程普通连接上限，默认 4，允许 1–32 |
| `DDA_DB_POOL_TIMEOUT` | 等待普通连接的秒数，默认 3，允许 0.05–30 |
| `DDA_DB_POOL_MAX_WAITING` | 等待普通连接的请求上限，默认 8，允许 1–128；耗尽时接口返回 503 |
| `DDA_STAGE_WINDOW_PAGES` | 下游处理窗口，默认 10，允许 1–20；OCR 成果仍逐页保存 |
| `DDA_STAGE_BACKLOG_PAGES` | 基础阅读尚未入索引的积压上限，默认 40，允许窗口大小–200 |
| `DDA_STAGE_FLUSH_SECONDS` | 不满窗口时的等待上限，默认 3 秒，允许 0.1–30 |
| `DDA_MODEL_RUNTIME_ENABLED` | 默认 1；0 回退原一次性进程，无需重装模型或重建向量 |
| `DDA_MODEL_IDLE_SECONDS` | 模型空闲释放时间，默认 30 秒 |
| `DDA_MODEL_LOAD_TIMEOUT` | 加载及握手上限，默认 300 秒；推理另沿用原各模型超时 |
| `DDA_MODEL_QUEUE_TIMEOUT` | 等待共享运行时上限，默认 3 秒 |
| `DDA_MODEL_MAX_WAITING` | 最多 8 个等待请求，达到上限立即返回繁忙 |
| `DDA_MODEL_CPU_THREADS` | 单模型计算线程数，默认 4，允许 1–16；不是模型并发数 |
| `DDA_DATA_DIR` | 工程 `.local/data`，业务原件、页面图和工作记录 |
| `DDA_MAX_UPLOAD_MB` | 默认 100；本机样例配置为 500 |
| `DDA_PORT` | 8765 |
| `DDA_PDF_PYTHON` | `.venv-docling/Scripts/python.exe`，目前用 PDFium 提取／渲染 |
| `DDA_OCR_PYTHON` | `.venv-ocr/Scripts/python.exe` |
| `DDA_OCR_MODELS_DIR` | `models/ocr/official_models` |
| `DDA_RETRIEVAL_PYTHON` | `.venv-retrieval/Scripts/python.exe` |
| `DDA_EMBEDDING_DIR` | 本机 Qwen3-Embedding-0.6B 权重目录 |
| `DDA_RERANKER_DIR` | 本机 Qwen3-Reranker-0.6B 权重目录 |
| `DDA_VISION_PYTHON` | `.venv-vision/Scripts/python.exe` |
| `DDA_VISION_MODELS_DIR` | `models/vision/PaddleOCR-VL-1.6` |
| `DDA_VISION_TIMEOUT_SECONDS` | 300 |
| `DDA_VISION_REGIONS_PER_PAGE` | 1，允许 1–10 |
| `DDA_VISION_MAX_NEW_TOKENS` | 128，允许 16–512 |
| `DDA_VISION_LOW_SCORE` | 0.85；低于该分的 OCR 行才触发视觉 |
| `DDA_VISION_TIMEOUT_BUDGET` | 3，允许 0–50；用尽后不再调用视觉 |
| `DDA_OCR_DEVICE` | `auto`；Paddle 用 `cpu` 或 `gpu:0`。没有 GPU 时用 CPU |
| `DDA_VISION_DEVICE`／`DDA_EMBEDDING_DEVICE`／`DDA_RERANKER_DEVICE` | `auto`；PyTorch 用 `cpu` 或 `cuda` |
| `DDA_VISION_DTYPE`／`DDA_EMBEDDING_DTYPE`／`DDA_RERANKER_DTYPE` | `auto`：CPU 或计算能力低于 8.0 用 float32；8.0 及以上优先 bfloat16，否则 float16 |
| `DDA_OCR_ENABLE_MKLDNN` | 默认 0。Windows 上 oneDNN 不稳定，仅 CPU 试跑时可设为 1 |
| `DDA_OCR_CPU_THREADS` | 0 表示沿用 `DDA_MODEL_CPU_THREADS`；GPU 上不传给 Paddle |
| `DDA_PDF_RENDER_SCALE` | 2，允许 1–4，变化后产生新的阅读版本 |

原件按内容摘要保存在 `documents/` 下，原文件名在数据库中。请同时备份 PostgreSQL 和业务数据目录。历史原始 OCR、视觉候选、人工版本保留；主流程不再写入或使用人工版本。机器有效文字单独追加，原始文字不被覆盖。来源改变立即排除旧索引，受影响页需要重新处理。不同项目资料不能交叉检索。

普通连接由 `psycopg-pool` 复用，Web 生命周期或 CLI 命令结束时关闭。后台队列另外持有最多一条执行租约连接；默认运行期预算为每进程 4＋1，多个后端进程会各自占用预算。服务停止后，尚在等待模型返回的后台任务可用短连接保存暂停检查点；此停机窗口不受普通池容量约束。具体事务与验证记录见 [RG-05h](testing/records/db-connections.md)。

本机单人版没有多用户权限体系、断点上传和自动孤立文件清理。非 PDF 暂时只登记并保存原件，不解析 Word、Excel 或 PPT；样例报告 DOCX 只用来分析章节，不作为原始证据混入。

## 测试和记录

全部集中到 [testing/](testing/README.md)，后续不再向根目录或 `.local` 散落测试文件：

```powershell
.\.venv-dev\Scripts\python.exe testing/run.py
.\.venv-dev\Scripts\python.exe testing/run.py --models
```

正式测试在 `testing/tests/`；每次运行的临时数据库、图片、日志和结果在 `testing/runs/<时间-ID>/`；质量试验与验收记录在 `testing/records/`；缓存在 `testing/cache/`；旧 pytest 目录已移动到 `testing/archive/`。旧产物可用于追溯，应用运行不依赖它们。移动清单保留，未删除客户原件或历史记录。

测试使用随机本机端口的隔离 PostgreSQL，不读取业务连接。默认 PostgreSQL 工具路径为 `D:/Apps/PostgreSQL/17/bin`，其他电脑设置 `DDA_TEST_POSTGRES_BIN`。中断测试后，清理产物前需确认对应测试 PostgreSQL 已停止。

## 当前边界与后续

2026-10-22 已完成 C-T01 Harness 运行基线：独立 `.venv-harness`、官方 SDK 0.2.1rc2、同版本 npm CLI 与独立 Node 22.19.0。启动检查可运行：

```powershell
.\.venv-dev\Scripts\python.exe -m dd_agent harness-check
```

该检查不调用模型或业务数据库。工具发现、实际 MCP 调用和跨进程会话恢复已通过本机合成模型端点测试；业务 RAG／Word 工具和报告生成仍待接入，材料页面尚不能生成报告。安装、配置、版本锁和边界见 [Harness 运行说明](harness-runtime/README.md)。下一步为 C-T02：将现有 RAG 和证据补读包装成项目绑定的 MCP 工具。

C-T02 首个小步已提供只读材料分页：`GET /api/projects/{项目ID}/material-catalog?limit=50`。将返回的 `next_cursor` 传入下一次的 `after`，直到为 null；每批最多 100 项。返回登记信息、已知页数与当前读取版本，未解析页数为 null；它不是有效索引覆盖统计，尚未注册为 MCP 工具。

自动路由目前是首轮章节关键词、少量 OCR 容错别名与文字不足兜底；自动局部选择使用行坐标和上下文窗口，机器选择采取保守的一致性／日期／数值／单位检查。它还不等于完整的语义理解、字段关系校验或真实材料质量验收。混合页图片覆盖、表格字段对应和正文跨页关联仍待加强。

开发侧已有 20 页候选样本、5 页独立 OCR 基线、6 个字段参考初稿；尚未达到至少 60 字段、冻结保留集或 95% 等质量验收条件。跨页组装、父子发布及查询已提供受控接线；默认查询仍为页级，启用混合模式的方式见上方“父子混合查询”。OCR 多实例并行池、Word 原生提取、报告生成和法律分析仍在后续任务中。常驻复用的实际边界和小样本测量见 [模型运行时记录](testing/records/model-runtime.md)。请勿将小样本模型运行成功解释为全卷准确率已达标。

迁移 009 保存组装检查点，010 保存独立父子索引，011 保存准备清单及向量检查点，012 新增收据待办与交接账本，`013_assembly_rebuild.sql` 新增独立的局部重算准备表，014 保存当前谱系与原子接纳校验账本，015 保存自动编排的阶段／所有权／预算；新版数据库目标为 015。升级前结束后端服务并保存任务进度，在项目目录运行 `.\.venv-dev\Scripts\python.exe -m dd_agent init-db`，随后重启后端；同一次命令会按顺序执行尚需升级的迁移。升级不自动运行组装或切换当前索引，008 的暂停阶段配置保留，无需新依赖或新模型。后台与查询分别按材料名单受控启用，配置见“父子混合查询”。

RG-06b-b1 提供父子原文／引用映射、真实 tokenizer 预算、独立向量存储，以及阶段 owner／代次保护下的原子发布；旧页级检索仍可使用。自动后台、局部重建及父块查询已有受控接线；默认开关关闭，只有指定且覆盖完整的材料进入新查询。范围与测试见 [父子发布记录](testing/records/parent-index-publication.md)。

`ParentWork` 增加单个工作包的受保护恢复：阶段登记与准备清单同事务保存，向量计算完成后先存检查点再响应暂停；新领取可在核对来源／模型／代次后复用向量，避免重做 token 切分和 Embedding。后台消费者区分页级任务与父子工作包；当前名单内的 shadow 调度会自动推进组装与交接，默认关闭。详见 [工作包恢复记录](testing/records/parent-work-recovery.md)。

`ReceiptHandoff` 将组装收据可靠交接给工作包：收据、尾段与交接待办一起提交，分段登记与游标一起确认；中断可重新对账，既有分段在新代次中可复用。重置后的旧来源不能继续发布，当前收据明确采用的相同工作可以复用。工作包消费者、受保护组装生产者、自动对账和局部重建均已在受控后台接入，详见 [收据交接记录](testing/records/assembly-handoff.md)。

- [阶段规格](specs/01-evidence-to-report/spec.md)
- [报告信息画像](specs/01-evidence-to-report/report-information-profile.md)
- [实施计划](specs/01-evidence-to-report/plan.md)
- [任务进度](specs/01-evidence-to-report/tasks.md)
- [验收记录](testing/records/p1-verification.md)
- [最近测试结果](testing/records/latest-run.md)


暂停功能的范围、状态和验证见 [暂停与继续规格](specs/01-evidence-to-report/pause-resume.md)。Qwen3-Embedding-0.6B 为 1024 维。`auto` 在有 GPU 时用 CUDA，否则用 CPU float32；计算能力 8.0 及以上才自动改用 bfloat16 或 float16。同一时间只常驻一个模型，换模型先结束前一个进程并释放显存。GTX 1070 上视觉约 3.6GB、向量约 2.5GB，不能同时常驻。备选模型见 [选型记录](testing/records/embedding-options.md)。设备、注意力和 KV cache 不改变已完成页的版本，不会因此重跑 OCR 或重建 float32 索引。

查询与后台处理共享一个模型计算许可，各自按到达顺序等待；连续最多 3 次查询后，给仍在等待的后台一次机会，在当前页／区域完成后切换。默认最多等待 8 个请求、排队 3 秒；后台繁忙会保留成果并重新排队，不消耗识别失败重试次数。公平规则限同一应用进程，其他进程仍通过文件租约避免重复加载。

搜索页面可取消请求。模型繁忙返回 503、查询取消 499、超时 504；总查询预算由 `DDA_SEARCH_TIMEOUT_SECONDS` 配置，默认 120 秒，在处理阶段边界检查，冷指纹和数据库操作仍遵循各自超时。重复查询的向量／重排结果可在内存复用（最多 64 项、序列化值 16 MiB、有效期 300 秒），每次仍重新召回并核对来源和覆盖，新增材料无需等缓存过期。默认不缓存整份检索响应。

性能记录默认开启：后台任务、CLI 处理与 API 操作完成后，摘要保存在配置数据目录的 `performance/`，包含耗时、计数和资源信息，不保存原文或查询内容。可在 `.env` 设置 `DDA_PERFORMANCE_ENABLED=0` 后重启关闭。两页合成样本的基线、复用验证和内存口径见 [性能基线记录](testing/records/performance-baseline.md)。

后台工作包分派沿用向量阶段暂停／继续，页面单列跨页包数，不将包数加进页数；待处理／运行中的当前工作包阻止父任务提前完成，工作包失败或来源失效使任务部分完成。无新开关或迁移，见 [分派验证记录](testing/records/parent-dispatch.md)。

`AssemblyWork` 为组装保存增加阶段领取保护：原文计算与摘要校验在锁外，提交时同时核对 owner／代次、来源、组装重置及封口范围；尾段、收据、交接待办和阶段成功一起提交。失败／缺页只作为边界保留，仍不进入向量原文。约 10 页自动窗口已由下方协调器接入，默认关闭，按材料名单试运行。本步沿用迁移 012，无新依赖，见 [组装保存验证](testing/records/assembly-work.md)。

`AssemblyIngestion` 已完成内部调度接线：约 10 页连续就绪文字推进一次组装，保留尾段、优先交接收据，并限制未结束工作包。实际调度器可恢复暂停位置，末尾还会核对封口、交接、工作包和全部物理页来源；页面将组装位置与向量页数分开显示。当前支持材料名单内影子运行，默认关闭；局部重建见下方模块，父子查询由独立查询配置选择。此模块使用数据库 012，最新整体目标为 015，见 [自动窗口验证](testing/records/assembly-ingestion.md)。

`AssemblyRebuild` 为局部重建提供独立准备接口：分批定位来源变化的窗口，从前一有效尾段重算，到完整状态再次一致时停止传播，并跳过未变的中间／后缀窗口。扫描与结果保存可在新实例及显式继续后恢复，不调用 OCR 或向量模型。013 保存待替换结果和分页清单；实际接纳由下方 Adoption／Repair 接续，当前默认关闭。实现与验证见 [局部重建记录](testing/records/assembly-rebuild.md)。

`AssemblyAdoption` 提供内部原子接纳入口：逐份持久校验后，受当前领取／代次／来源保护地追加替换收据，保留未变窗口，切换当前收据谱系和检查点，并退役旧包。交接和覆盖校验只读取当前谱系，同源旧工作可在重新领取后复用。014 新增谱系及接纳账本；接纳并不表示替换向量已全部完成，后台建库默认关闭，查询默认混合并按成果资格选择。验证见 [原子接纳记录](testing/records/assembly-adoption.md)。

`AssemblyRepair`／`RepairingAssemblyIngestion` 提供内部自动编排：分步等待就绪、扫描、重算、校验和接纳，进度与有限预算保存在 015 调度账本中；暂停／新实例恢复后复用成果，失去执行者仍需沿用现有显式继续流程。接纳后等待交接、向量及当前覆盖校验，不把修复完成冒充材料已全部可检索。后台可通过材料名单受控试运行，默认关闭；查询默认混合，自动选择完整父子成果或回退页级。见 [自动编排验证](testing/records/assembly-repair.md)。

测试仍为每个用例建立独立 PostgreSQL 数据库并从空库迁移；成功且所有连接已关闭后及时回收，失败、连接占用及集群收尾时保留。回收账本为对应 `testing/runs/<运行编号>/database-lifecycle.jsonl`，不包含连接凭据；不强断连接，不清理业务库或历史运行目录。该改动无需安装依赖或升级数据库，见 [生命周期验证](testing/records/test-database-lifecycle.md)。

受控试运行：设置 `DDA_PARENT_INDEX_MODE=shadow` 和 `DDA_PARENT_INDEX_DOCUMENT_IDS=<材料UUID>`（逗号分隔，最多 20 个）后重启服务，启动／继续指定材料。回退设为 `off` 后重启，检查点和成果保留；模式切换前先暂停并等待保存。材料 UUID 可从阅读页地址的 `document=` 获取。检索模式另由 DDA_SEARCH_INDEX_MODE 配置，默认混合、未就绪材料沿用页级；后台配置本身不创建任务，已排队任务沿用既有后台调度。详细恢复规则、验收和边界见 [受控接线记录](testing/records/parent-rollout.md)。

后台模型计算期间，带总截止时间的交互查询按剩余查询预算排队，不再被后台默认 3 秒等待提前截断；队列满仍返回繁忙，取消、总查询超时及后台短等待保持有效。真实合成样本、排队修复与测量限制见 [验收记录](testing/records/parent-real-gate.md)。
