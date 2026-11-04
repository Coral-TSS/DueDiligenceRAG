# 尽调 Agent 编排方案评估（历史）

> 2026-10-22：报告层的旧选型建议已由 [Harness 架构决策](harness-report-agent.md) 替代。当前选择是复用 DeepSeek Harness，由模型经 RAG／Word 工具自主完成报告；首轮不建立完整事实库和自研报告状态机。以下内容保留 2026-10-20 的评估背景，不能作为当前报告实现要求。材料处理与报告会话继续分开，原有后台恢复问题仍按 PR-05 处理。


日期：2026-10-20。性质：结合现有代码和官方开源资料提出的选型建议，不是已完成的迁移或框架实测。本次不修改运行依赖或原开发计划的正式选型。

后续方向补充（2026-10-20）：用户明确本地资料处理、外网报告生成及按重要性使用小型视觉模型。总体计划已据此将 LangGraph／Agents SDK 从必需项调整为可选，分层路由与并行任务见 P1 的 B-R／B-P。此为方案更新，不代表已有代码完成这些能力；框架能否本地运行与生成接口是否外网调用分别判断。

## 建议

首版不强制使用 LangGraph，也不直接将整个工程改成 OpenAI Agents SDK。保留 FastAPI、PostgreSQL、独立 PDF／OCR／检索工作进程，自行定义尽调业务步骤和数据版本。模型调用通过独立适配器接入；出现明确的工具循环或 Agent 委派需求后，再选一个薄的 Agent SDK。

“自行开发”指业务步骤、状态、证据和采用规则由本项目掌握，不表示自行实现模型 SDK、通用分布式调度器或所有重试机制。后台任务规模增大后，评测成熟的持久任务组件；已有 PostgreSQL 的情况下，DBOS 值得与小型数据库任务方案一起比较。

LangGraph 对未来的补证循环、分支合流、长时间人工中断有价值。它是条件明确后可引入的编排层，不是先导入文件或写出首版报告的必要条件。已安装的包可以保留，安装完成不构成采用理由。

## 当前代码事实

- pyproject.toml 的运行依赖为 FastAPI、Uvicorn、psycopg、multipart 和 dotenv，没有 LangGraph 或 Agents SDK。
- src/dd_agent 中没有 LangGraph 导入或图定义。
- reading.py 的 Reader.read 负责检查页码、分流工作进程、保存分页结果与跳过当前版本已成功的页面；worker_records 负责子进程和超时。
- db.py 保存项目、材料、阅读元数据和分页结果。当前尚未实现持久任务队列、任务领取租约、心跳和 worker 崩溃后的自动重新调度。
- 因此目前不存在必须先拆除的 LangGraph 实现。当前有价值的资产是业务表、原件／页码定位及进程隔离，可被不同编排方案复用。

## 分开选择两层能力

| 层次 | 项目需要解决的问题 | 推荐责任边界 |
| --- | --- | --- |
| 材料与后台任务 | 649 页扫描件、批量向量化、限流、取消、重试、进度和断电／进程崩溃恢复 | 明确的文件／页／区域／索引任务；任务记录和工作进程管理。不让模型决定是否遍历完整材料。 |
| 模型与业务推理 | 根据问题选检索工具、补查证据、结构化事实、起草及校验章节 | 第一版使用明确步骤和结构化输出；需要动态工具循环时引入 Agent SDK 或图编排。 |

模型 SDK 的 session、图检查点、任务队列和尽调事实表不是同一个对象。聊天或运行状态保存后，仍需业务数据版本、幂等写入及外部操作重试设计；任何框架都不会自动替项目完成证据核对或提高 OCR 准确率。

## 候选比较

| 方案 | 已核实的主要能力 | 对本项目的适用判断 |
| --- | --- | --- |
| OpenAI Agents SDK | Agent 运行循环、工具、Agent 委派、结构化输出、人工工具审批、session、tracing；RunState 支持暂停后序列化及恢复；可集成 Temporal／DBOS／Restate 等持久执行组件 | 适合后续检索补证、章节起草与审查助手。可以用代码编排多步流程，支持其他模型适配；不能把 session 或 RunState 直接视作全卷任务调度。 |
| LangGraph | 显式图状态、条件边、检查点、人工中断／恢复与状态回溯 | 若经常出现“发现冲突→补查→等待核对→返回某步骤→重算关联章节”，比维护大量自定义分支有优势。目前 OCR 和索引链路本身不要求图编排。 |
| Pydantic AI | 类型与结构化输出、工具和模型适配；提供与持久执行引擎的集成 | 对工商字段、历史事件和证据 schema 值得借鉴。可作为模型层候选，不必为了数据校验同时引入一整套 Agent 框架。 |
| LlamaAgents／LlamaIndex Workflows | 面向文档工作流的事件驱动 Python 步骤，可分支、循环及并行，提供持久化与恢复接入方式 | 与文档处理业务相近，值得参考事件设计、工作进程和进度接口；接入其持久化后端的成本仍须实测。 |
| DBOS | 数据库支持的持久 Python 工作流，可与 Agents SDK 等组合 | 值得作为后台任务组件评估，有助于减少自行实现恢复语义的工作；Windows、本机依赖及当前 PostgreSQL 连接组合尚未验证。 |
| 小型自定义业务流程 | 业务步骤和数据关系直接以 Python、SQL 表达 | 当前的优先建议。范围严格限定为单机首版；当恢复／调度复杂度超过可维护范围时采用成熟组件，不继续扩写通用工作流引擎。 |

以上是根据文档及项目现状作出的工程判断，不是对框架性能、稳定性或本机兼容性的实测排名。

## 需要借鉴的模式

1. Agents SDK：工具有明确输入／输出 schema；模型上下文与业务对象分开；Agent 可作为受约束的工具调用，而不是默认让多个角色反复自由讨论。
2. LangGraph：显式步骤、状态和暂停点，避免流程藏在提示词里；图状态优先放项目、任务、证据和版本标识，不放整个文件或整份百页报告。
3. Pydantic AI：先验证字段类型和业务约束，再接受结果；模型通过 schema 不代表事实为真，仍需出处与核对状态。
4. LlamaAgents：以“页面识别完成”“候选有冲突”“人工修订已保存”等事件连接后台、界面和下一处理步骤。
5. 持久执行引擎：区分可重试计算和数据库／API 等外部操作；恢复过程中写入不得重复，模型调用结果可按适用版本复用，重试要有预算。

LangGraph 的 interrupt 恢复时会从当前节点开头重新执行，节点中的数据库写入或模型调用仍需合理划分并保证幂等；引入框架不意味着这些问题消失。Agents SDK 的长等待／崩溃恢复也有持久执行集成路线，不能笼统称其“不支持恢复”。

## 适合首版的结构

FastAPI 页面与接口 → 应用业务服务 → PostgreSQL 任务及证据记录 → 独立 PDF／OCR／检索工作进程。

应用业务服务再通过独立模型适配器调用本地或获准的远程模型。Agent SDK 仅在确有需要的模型工具循环中使用，数据表和文件存储不由 SDK 的聊天记录代管。原件、事实、证据和报告版本继续由业务数据层管理。

如果采用自定义任务方案，至少补齐以下行为后才称为可恢复后台任务：

- 任务输入、处理版本、依赖和幂等键持久化；结果分批提交。
- worker 原子领取任务，保存租约／心跳；进程退出后可识别过期任务并重新领取。
- 限定并发、超时、重试次数及取消边界；恢复后不重复写入原件或事实。
- 人工确认值和待核对候选分开；重试或模型更新不覆盖已确认修改。
- 服务重启后页面能重建进度、待核实与失败状态，不能只依赖进程内任务对象。

## 何时转向框架

- 保持当前建议：流程以明确步骤为主，人工修订是保存业务记录，首次报告采用有限次数的检索／起草／校验。
- 评估 LangGraph：多种专项形成大量条件分支与循环；多步骤需在人工核对后从指定状态继续；同时存在跨章节依赖及多轮补证。
- 评估 Agents SDK：需要模型自动选择本地工具、调用有限范围的专门 Agent、流式返回或统一模型运行记录；优先只接入模型层。
- 评估持久任务组件：开始多 worker、多项目并发，取消／崩溃恢复／重试代码明显膨胀。先做小型验证，再定 DBOS 或其他引擎；不同时叠加多个恢复层。

所有候选采用同一恢复场景验证：在处理中断进程、在业务写入后重启、等待人工修订后恢复、模型超时／限流、更新材料版本；核对已完成页是否跳过、事实是否重复、人工值是否受保护和进度是否准确。SDK 还需验证实际模型供应商的工具调用和结构化输出兼容性。

## 本地部署与外发

框架选择与模型服务位置分别配置。Agents SDK 可以使用其他模型适配，但其默认 tracing 会导出到 OpenAI 后端，且默认可能包含模型／工具输入输出；本项目采用时应默认禁用外部 tracing 或替换为受控本地记录。仅关闭 tracing 中正文采集不等于禁止全部遥测外发。LangGraph 的状态也须选择本机持久化后端，不能以开发期内存保存替代重启恢复。

本次未安装、卸载或测试候选框架，未调用外部模型、未上传客户材料；原计划中的 LangGraph 目前仍属于待重新决策的预选项。

## 查阅的官方资料

- [OpenAI Agents SDK 概览](https://developers.openai.com/api/docs/guides/agents/sdk) 与 [Python 仓库](https://github.com/openai/openai-agents-python)。
- [Agents SDK 运行与持久执行集成](https://github.com/openai/openai-agents-python/blob/main/docs/running_agents.md)、[人工工具审批及 RunState](https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md)、[tracing 默认行为](https://github.com/openai/openai-agents-python/blob/main/docs/tracing.md)。
- [LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[检查点](https://docs.langchain.com/oss/python/langgraph/checkpointers) 与 [中断语义](https://docs.langchain.com/oss/python/langgraph/interrupts)。
- [Pydantic AI 仓库](https://github.com/pydantic/pydantic-ai) 与 [持久执行说明](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)。
- [LlamaAgents／Workflows 仓库](https://github.com/run-llama/llama-agents)。原 workflows-py 仓库地址当前跳转至此。
- [DBOS Python 仓库](https://github.com/dbos-inc/dbos-transact-py)。
