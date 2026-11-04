# 原子接纳局部重算清单

2026-10-31。任务状态唯一入口为 specs/01-evidence-to-report/tasks.md。

## 实现边界

014 新增当前谱系 head／position 映射及接纳校验账本。generation 是不可变收据身份，position 是物理窗口位置；替换中段后可能出现 1／4／3 的收据顺序。AssemblyRebuild 的扫描、传播、前驱、游标改用 position；manifest 保留旧参数名 after_generation，但应按 next_cursor 传回逻辑 position，不自行用 receipt_generation 拼游标。尚无外部 API 使用该内部接口。

AssemblyAdoption.validate_next 每次验证一份替换结果的 Python seal、身份／范围／尾段，并持久保存数据库 JSONB 摘要和游标。最终封口状态单独验证，不在 Python 中加载整个清单。数据库摘要用于确认发布时仍为已验证的 JSONB，不用数据库 JSONB 序列化替代 Python canonical 的内容校验。

commit 使用当前父任务代次和精确 assembly 领取，发布时复验冻结来源、基线、已验证结果及最终状态。用 SQL 追加 replace 收据，触发 outbox，保留 keep 收据／origin，切换谱系与末尾检查点，退役旧阶段领取，确认接纳和阶段成功；全在同一事务内。失败回滚整套写入。正文校验和模型计算不在发布 Python 回调中运行；SQL 摘要复验和写入成本随替换内容／窗口数增长，尚无整卷吞吐结论。

ReceiptHandoff 只交接当前谱系；覆盖和旧包有效性也使用同一谱系。已退役包的迟到 owner 不能发布；新收据重新引用同源工作时，仅将 origin_retired 取消的任务重新排队，必须重新领取，向量检查点保留。已完成工作沿用原有复用逻辑。独立包进度不再计入已退役 origin，页面数量口径不变。

显式 reset 令旧 head 失效；旧接纳确认只作历史确认，不恢复旧谱系。显式继续可恢复验证成果及确认的访问代次，旧 epoch 仍被拒绝。内部驱动的阻止状态包含检查点代次，使新谱系不继承旧谱系的停止原因。

本步只接纳到当前收据谱系，替换向量仍由既有有界交接／ParentWork 完成，不能将接纳成功当作全部可检索。没有默认开启新驱动、切换查询或执行客户全卷。原件、页面、向量检查点及 origin 业务账本未删除。

## 验证

- RED `20261031-120129-4c4141`：10 failed／110.83 秒，接纳模块缺失。
- 首轮接纳与准备兼容 `20261031-120433-2b57a8`：32 passed／228.35 秒，隔离库正常停止。
- 衔接边界 RED `20261031-120835-fb071a`：4 failed／2 passed／10 deselected，77.93 秒。确认领取后正文变化未立即报告、未变旧包重新挂接仍 cancelled、旧驱动 halt 未解除、显式继续后历史确认 epoch 未重绑；逐项修复后进入全量回归。损坏校验与零替换接纳已通过。

完整回归 `20261031-121010-a4d2ae`：**380 passed／12 skipped／1 failed**，393 项，1312.35 秒。16 项新增接纳及原 RAG 用例通过；唯一失败为 `test_harness_check_cli_does_not_need_database_or_model_credential`，runtime_initialization_failed，诊断 TimeoutError，与 OBS-02 一致。测试库正常停止，server.log 在本地 12:32:02 记录 database system is shut down。

未改代码单项复测 `20261031-123216-a0a5fb`：**1 passed／4.52 秒**。保留完整回归失败，不将单项复测当成全量零失败；Harness 启动根因仍待后续接入验收。OBS-03 的调度完成等待未在本轮复现，仍保留观察。

12 项跳过是 11 个默认关闭的真实 tokenizer／模型入口及 Windows 符号链接权限 1 项。本轮仅使用合成文本和合成向量，未新增真实模型推理或客户全卷测试；没有放宽初始化／连接／停库等待或模型超时。数据库日志另记录 69784 个文件、935.330 秒的时间检查点，随后最终停库同步 62.368 秒；测试库文件数量与初始化／落盘开销不代表业务模型吞吐，不据此宣称整卷性能改善。

## 部署

数据库目标 014。停止旧后端后在项目目录运行 `.\.venv-dev\Scripts\python.exe -m dd_agent init-db`，再重启。迁移可重复执行，已有成员 position 仅在缺失时回填；不重 OCR、不重建客户索引，无新依赖或模型权重。受控启用／回退和父子查询仍需后续独立验收。
