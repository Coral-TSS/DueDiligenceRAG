# RG-06b-b2 受保护组装发布

2026-10-29。任务状态只维护在 tasks.md。

## 实现

AssemblyStore.prepare 在无持锁事务中读取检查点并执行原文组装，生成最多 20 输入页／200000 字符、8 MiB 结果的清单；整个封装另限 12 MiB。摘要／副本校验发生在阶段保护之前。commit 接受调用方连接，内部已验证提交入口复用 generation CAS、来源与物理末页校验及旧收据重放。旧 advance 保留兼容。

AssemblyWork.register 将组装请求、来源和边界依赖绑定 assembly 阶段、父任务及 expected_epoch。run 绑定 task／owner／epoch／scope／input_revision，在同一 publish_scope 中保存 tail、receipt、触发的 outbox 和阶段成功；任一 SQL 或阶段完成失败，一起回滚。暂停归还领取且不消耗失败预算；来源失效／代次冲突取消，其他发布异常有限重试。清单不持久复制成另一份全文库，中断后从已有页面／tail 重新准备未提交窗口。

StageJobs.claim 可按 task_id 精确领取，防止处理器误领另一窗口。只有 assembly 允许显式 readiness／quality 依赖：ready 仍要求成功，failed 要求当前失败来源，pending 只保留修订与质量相符的边界，missing 绑定当前读取版本且要求行不存在。reading 行锁保护缺页插入。vision／embedding 拒绝这类条件，不能把缺口混进向量原文。

组装重置后，旧收据仍作历史保留；受保护 run 在持锁区核对最新 reset，不因原文未变而重新采用旧收据。当前请求从重置代次重新准备后才能推进。旧显式 advance 的历史重放契约不变。

## 验证

- RED `20261029-101707-dac96d`：15 failed，受保护发布模块缺失。
- 第一轮 GREEN `20261029-101917-2b3030`：40 passed，131.19 秒，覆盖新增 15 项及原 assembly_store／stage_jobs 回归。
- 重置重放 RED `20261029-153135-a9c28d`：1 failed，旧收据在 reset 后仍被重新采用，已加入持锁 reset 检查。

- 扩展专项 `20261029-153227-fe1aee`：60 passed／1 skipped／1 failed，196.85 秒。唯一失败为测试辅助函数错误地对空封口调用 snapshots([])；生产接口正确拒绝空读取范围。改为直接向 prepare 传空输入，不修改生产限制。reset、领取、回滚及锁外校验均通过。

- 空封口复测 `20261029-153627-2aacfe`：1 passed，21.05 秒。本轮新增 21 个行为均已通过分组验证。

最终完整回归 `20261029-153718-fc150a`：**315 passed／11 skipped／1 failed**，695.78 秒。唯一失败 `test_slow_vision_does_not_block_native_reader_and_backlog_is_bounded`：释放慢视觉后，后台线程未在测试的 join(20) 内结束；本轮新增 21 个用例均通过。隔离库正常关闭。

原代码单项复测 `20261029-154940-d79707`：**1 passed／26.52 秒**（含测试库建立与关闭）。两次 performance 中 job trace 分别约 23.931 秒与 6.036 秒，SQL 累计约 8.928 秒／5376 调用和 3.102 秒／5193 调用；累计值包含多线程，不当作单一墙钟。完整回归测试库另有一次 Windows PostgreSQL 共享内存映射保留失败日志；未证明它导致该用例失败，不能据此排除应用回归。没有修改等待时间、删除测试或改变生产代码来获得复测结果。

整组原代码调度器复测 `20261029-155044-16ca8f`：**11 passed／75.23 秒**，隔离库正常停止。完整回归仍保留一项失败，OBS-03 继续观察；没有更改生产代码或等待时间。

## 接入与部署边界

这是任务卡中“拆分 prepare／commit 并保护保存”的第一子步。默认 StageScheduler 尚未自动生产 assembly 窗口；下一步才接约 10 页就绪判定、窗口推进、收据对账、暂停与背压及全体封口／交接排空条件。局部边界传播和受控启用／回退仍在后续。当前未改变活动索引，也未向用户新增 API 或开关。

业务库 012 已只读确认就绪；无新迁移、依赖或权重，不要求再次执行 init-db。测试全部使用隔离 PostgreSQL 和合成内容；未读客户原文或跑整卷，不提供吞吐收益结论。本轮无界面变化，浏览器视觉、读屏及云端模型验证不适用。
