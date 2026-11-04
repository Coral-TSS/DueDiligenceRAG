# RG-05c：持久阶段待办

交付日期：2026-10-27。契约 P1-perf-v1／K04。状态、负责人只维护在唯一 tasks.md。

## 已接入的真实路径

1. `JobManager.enqueue()` 在同一事务锁定或创建父任务，并建立 preflight 待办。重复入队不产生第二份任务；对已存在的父任务先取行锁，避免选择后任务恰好结束、播种阶段失败的竞态。
2. `Reader` 的 `Database.save_page()` 写基础页，007 的数据库触发器在同一事务记录 vision 来源事件；`AutomaticProcessor` 追加机器结果时，由触发器为真正生效且基础修订匹配的结果记录 embedding 来源事件。outbox 不含正文。
3. `save_page(..., conn=conn)` 可加入调用者的发布事务，不自己提交或关闭外部连接。页写入和 outbox 随外层事务一起提交／回滚。同一事务多次改写同一页使用单调更新时刻，避免 `now()` 固定在事务开始时间导致来源修订碰撞。
4. `StageJobs.reconcile()` 将来源事件转为有版本约束的阶段任务，每次最多 100 条；缺少真实模型／策略版本时等待，不推测版本或跳过事件。投递账本与任务创建同事务提交。
5. 明确继续父任务时递增 run_epoch，取消旧代次未完成任务，结束对应尝试记录；新代次重新对账。同父任务、同实际输入的成功阶段可复用，run_epoch 不混入成果内容版本。父任务进度保存、检查点和最后暂停收束也校验代次，旧父执行者不能覆盖新运行。

目前仍使用原五页串行处理器。新仓储／可靠生产者已经接入，阶段消费者、约十页触发下游、独立暂停和背压由 **RG-05d** 接入；本项不宣称已经并行或加速 OCR，也不能把新阶段表里 pending 的数量当作当前旧处理器的完成覆盖。

## 模块与状态

新增 `src/dd_agent/stage_jobs.py`、`migrations/007_stage_jobs.sql`，适配 `db.py`／`pipeline.py`。原模型环境、原文件、机器页、片段向量均保留。

| 持久对象 | 用途 |
| --- | --- |
| processing_jobs.run_epoch | 父任务执行许可代次；resume paused 时递增，重复 resume queued 不递增 |
| page_outbox | 基础页／机器页成功提交产生的下游来源事件 |
| stage_deliveries | 父任务＋代次＋事件 ID 的投递账本；保证晚提交事件不会被游标漏过 |
| stage_jobs | 范围、策略／来源输入摘要、上游阶段与页依赖、状态、owner、重试预算、成果引用 |
| stage_attempts | 每次领取的 owner、开始／结束及分类错误，保留重试证据 |

状态：`pending → running → succeeded`；临时失败且预算未尽回到 pending，永久失败／预算耗尽为 failed，旧代次／旧输入为 cancelled。等待前置阶段时仍是 pending，depends_on 记录明确依赖，claim 不领取未满足项；暂停父任务也不领取。默认最多 3 次尝试，允许显式 1–5 次，重试等待为 0–300 秒。

input_revision 绑定阶段、规范化范围、来源、policy_version、逐页版本依赖，以及前置阶段的输入修订。vision／assembly／embedding 必须绑定全部范围页；策略变化或前置输入变化会替换旧非终态任务。前置任务必须属于同一父任务，复合外键绑定父任务／项目／材料。

## 并发与发布契约

- `claim()` 短事务使用 `FOR UPDATE SKIP LOCKED`，返回唯一 owner_token；事务结束后才推理。父任务 SHARE 锁允许子任务并发领取，但阻止 resume 在领取中改变代次。
- `complete()` 按 parent → reading → 升序页 → stage 加锁，复核 owner、代次和逐页修订，再调用短 SQL 发布回调。发布与 succeeded 一起提交；回调抛错则一起回滚。旧 owner、旧代次或旧来源不会执行发布回调。
- 回调只能使用传入的 conn 做数据库写入，不能在其中推理，也不能调用会自己另开连接的旧发布方法。RG-05d 必须将模型计算放在事务外，并通过这个提交入口接入真实阶段消费者。
- 只有实际父队列租约退出后，既有恢复逻辑才把中断父任务收束为 paused；不凭任务年龄强行抢占。resume 取消旧子任务，旧结果迟到不发布。
- 序列 ID 分配不是事务提交顺序。stage_cursor 仅作观测，不用 `id > cursor` 排除事件；stage_deliveries 逐事件去重，已拿到较大编号后，较小编号晚提交仍会在下一次对账发现。
- `reconcile.created` 当前表示有效事件已解析到任务的次数（含复用），不是新生成成果数或可检索页数；覆盖统计继续以业务成果和当前来源为准。
- `claim()` 每次最多清理 100 个失效候选；返回 None 也可能是前置未就绪、暂停或清理批次耗尽。RG-05d 不能仅凭一次 None 就宣布整份材料完成，应结合持久待办及成果状态判断。

## 验证

全部使用独立 PostgreSQL／合成材料，不读取业务库或调用模型。

- 初始红灯：[20261026-215352-fd9bbd](../runs/20261026-215352-fd9bbd/summary.json)，8 failed；缺少阶段仓储。
- 初次实现：[20261026-215817-d827c4](../runs/20261026-215817-d827c4/summary.json)，8 passed。
- 提交顺序、来源依赖与前置策略的红灯：[20261026-215922-05f145](../runs/20261026-215922-05f145/summary.json)，3 failed；随后阶段／暂停／恢复针对性运行 [20261026-220053-13adbe](../runs/20261026-220053-13adbe/summary.json)，26 passed。
- 同事务修订与父任务代次的红灯：[20261026-220246-05293e](../runs/20261026-220246-05293e/summary.json)，3 failed；修复后全量 [20261026-220442-93f3ec](../runs/20261026-220442-93f3ec/summary.json)，150 passed／2 skipped。
- 最后补测重复入队竞争：[20261026-220837-24000a](../runs/20261026-220837-24000a/summary.json)，1 failed；父任务锁已补。
- 最终全量：[20261027-095422-f4f6dd](../runs/20261027-095422-f4f6dd/summary.json)，**151 passed／2 skipped，322.59 秒**。新增 16 项阶段用例全部通过；跳过的两项是默认未开启的真实 Qwen／视觉模型集成。隔离 PostgreSQL 正常停止，日志的 60 秒等待栈对应 checkpoint 同步及停库过程，不是应用或测试失败。业务库未执行迁移。

正式阶段测试集中在 `testing/tests/test_stage_jobs.py`，共 16 项，覆盖事务提交／回滚、跨仓储实例恢复、有界对账、两个领取者竞争、依赖等待、有限重试、旧 owner、来源变化、代次变化、跨代次成功复用、发布异常原子回滚、缺失策略、晚提交事件、历史事件补齐、同事务版本区分及父任务入队竞争。

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv-dev\Scripts\python.exe testing/run.py testing/tests/test_stage_jobs.py -q
.\.venv-dev\Scripts\python.exe testing/run.py -v -o faulthandler_timeout=60
```

## 升级、兼容与后续接入

迁移版本从 6 变为 **7**。先停止旧后端，运行 `python -m dd_agent init-db`，再 `check` 和启动。007 是增量表／字段／触发器，并为历史成功页补齐去重事件；反复迁移不产生重复事件，不删除或重做已有识别／向量成果。迁移已在空库和缺失历史事件的合成库验证，未对用户业务库执行。

本次没有新增依赖。暂不提供自动降级脚本，以免删除恢复账本；如需回退代码，应保留新增表和成果，使用兼容 schema 7 的健康检查，或在停机备份后单独制定降级迁移。不要仅为通过旧健康检查删除版本号或业务数据。

RG-05d 接入时：由协调器传入真实 policy_versions，循环有界对账／规划 reading，领取阶段后释放事务再调用对应模型；发布使用 complete 的 conn。reading 成功复用时还须核对所需图像是否存在，文件修复沿用后续图像成果生命周期，不把 journal.succeeded 单独当作磁盘成果可用证明。跨新父任务的复用继续依赖原业务成果表，本仓储的跨代次复用限同一父任务。前端独立阶段进度与资源消费者仍未实现。
