# RG-05j／j1：新任务复用不可变计划

日期：2026-11-04。只使用隔离 PostgreSQL 与合成材料，不读取客户卷宗，不修改本机配置、模型权重或业务数据。schema 仍为 015，无新增依赖。任务状态唯一来源仍为 tasks.md。

## 问题与改动

原 ReceiptHandoff._old_part 按 job_id 查询，仅新 epoch 可复用。历史真实两页验收同版本重跑为 6.597 秒，其中 tokenizer worker 为 5.654 秒；这是该输入的观察，不能推广为整卷占比。

现在按项目／材料、当前收据代次、assembly policy、index version 和分段起点查候选，跨 job 去重。先返回最多两份小元数据，歧义则重新准备；唯一候选才读取一份准备正文，避免在大 JSON 上 DISTINCT／排序。使用现有 work(document_id,index_version,id) 与 parts(build_id) 索引；未新增表或全局正文缓存，也不读取历史向量作为候选查找条件。

事务外复验完整父块、依赖、确定性 build/input 身份、登记与 seal、子块文字／坐标／身份和已保存 token 长度。正文非空缺口、错误预算、不一致子块不能复用。保留原切块器去除边缘空白和硬切／token 二分后的重叠规则，不为缓存命中改变分块边界。静态指标为 assembly.handoff.plan_reused／plan_prepared／plan_invalid／plan_ambiguous／plan_repaired，不记录正文或 SQL 参数。

登记仍持当前父任务、阅读与账本保护，重查当前 origin、来源、job／epoch 和游标；新任务重新登记／领取，不继承旧 owner 或成功状态。ParentWork 读取原向量检查点时仍验证 seal、数量、维度和来源。缺向量时只复用计划，再计算一次向量；已有有效向量时两者都不重算。来源过期不作为普通 cache miss 继续发布。

损坏计划按当前来源重新准备后，收据交接允许修复同一确定性身份下的准备副本；行锁、修复、阶段登记、part 和游标同事务。清除该损坏副本关联的向量检查点，已发布历史不修改。普通 ParentIndex.register 仍拒绝冲突；材料／版本／input 登记本身冲突明确拒绝，不猜测修复。计数反映路径执行，事务回滚时不应据计数推断已提交成果。

## 验证

- RED：20261104-120751-71363e，5 failed／4 passed／180.94 秒。新任务重复分词、损坏副本阻塞恢复及复用前发生 reset 的零分词断言失败，版本／来源失效用例已通过。
- 初版 GREEN：20261104-121134-b6de2b，38 passed／2 skipped／220.79 秒。覆盖新任务复用、原同任务恢复、来源／reset、原子交接与工作包保护。此时尚未加入最终子块一致性检查。
- 子块一致性 RED：20261104-121347-1fb335，1 failed／12 deselected／66.79 秒。仅 seal 不足以发现正文与子块身份不一致的重封口副本；已加入派生内容／坐标／身份复验。
- 受影响回归：20261104-121605-4a4073，83 passed／4 skipped／1 failed，504.06 秒。唯一失败为新增故障注入脚本提前检查历史 part，尚未到达要验证的 INSERT；改成 NOT VALID 后仍对所有后续写入强制约束，不放宽回滚断言。生产代码未因此变化，原回归中其余用例均已通过；修正脚本及真实模型复测见下。

最终复测：20261104-122445-dc185c，2 passed／0 skipped／0 failed，367.64 秒。包含上述修正后的事务回滚测试，以及实际 Reader／PaddleOCR／PaddleOCR-VL／Qwen Embedding 与 Reranker／tokenizer／pgvector 的完整两页合成验收。实际暂停、恢复、同版本新 job、后台与查询竞争通过；实际观察到交互排队且前后台均完成，未增加超时或改变质量配置。

本轮受影响的 84 项确定性用例最终均通过（83 项在前一运行，1 项修正脚本后复测），另有 1 项真实模型验收通过。4 个独立可选模型入口仍在上述确定性运行中跳过；本轮没有重跑全项目回归，最近全项目结果仍是 2026-11-03 的 463 passed／14 skipped，不能把该历史数字算作本轮完整回归。

真实新 job 重跑：1.345 秒，pages.read.reused=2、pages.automatic.reused=2、pages.index.reused=2、assembly.handoff.plan_reused=1、parent_work.restored_vectors=1；没有 worker 启动、OCR 或向量计算请求。历史同场景为 6.597 秒，其中 tokenizer worker 5.654 秒；本轮没有清空 OS 缓存或冻结外部负载，首次／恢复阶段也明显更慢，故只证明实际消除了本次重复分词，不据此宣称整卷倍数、吞吐或客户质量。详细 trace 和 summary 均保存在本次运行的 parent-real-gate/ 下。

用例／脚本／原始产物统一在 testing/。没有新增页面或 API，本轮无独立 UI／无障碍变化；并发、来源、错误、空收据、恢复与事务回滚为主要验收点。j2 单工作包微批、j3 持久内容向量缓存、j4 跨包合批尚不属于本次交付。
