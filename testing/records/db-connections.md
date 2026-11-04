# RG-05h 短事务与连接

日期：2026-10-26。契约：P1-perf-v1／K03。任务状态只维护在 `specs/01-evidence-to-report/tasks.md`。

## 实现与连接预算

- `src/dd_agent/db_connections.py`：官方 `psycopg-pool==3.3.3` 的有界 provider，构造不连接，作用域启动后按需连接。普通池默认 min=0、max=4、等待 3 秒、等待队列 8；不按服务对象数量增加池。
- `Database.connect()` 保持上下文事务：正常提交、异常回滚。归还前清理会话 advisory 锁与设置，断开的连接由池丢弃重建。SQL 执行／取数沿用 RG-05a 观测；池提供 checkout／busy／等待耗时与 `stats()` 统计。`stats()` 在关闭后是最后快照，不代表当前存活连接数。
- Web 组合入口把同一个 Database 注入 Reader、Review、Automatic、Evidence、Pipeline、JobManager；CLI 的 `database_scope()` 负责显式关闭。未配置数据库仍可打开诊断页，不加载模型。
- 正常运行期每进程最多 4 条普通连接＋1 条队列执行租约；多进程预算相加。队列锁 74332103 走专用 `Database.lease()`，模型运行期间只保留会话锁，SQL 后提交事务；74332104 控制串行化保持不变。普通池重置不能释放专用连接上的执行锁。
- 停机先通知后台并等当前短暂收尾，再关池。已借出连接归还时关闭；在途模型迟到的安全检查点与作用域外兼容调用可使用短连接，不会偷偷重开池。这是正常池容量预算之外的停机例外。

## 事务与正确性

搜索现在依次执行短范围／覆盖查询、事务外 query Embedding、短召回查询、事务外重排序、返回前来源核对。向量计算不保留 `idle in transaction`。页索引发布仍在一个事务中锁当前材料与页、核对来源修订、替换页记录和片段；模型计算在进入发布事务前完成。没有跨页合并提交，没有降低 PostgreSQL 耐久设置，也没有修改 schema。

连接池耗尽／等待队列满／获取超时返回固定的可重试 503，不输出 DSN。归还清理由官方池异步执行，饱和后立即重试可能短暂再次繁忙；调用方应稍后重试，不能假设释放动作一发生连接便同步可用。

## 实测

只使用隔离 PostgreSQL、合成页文字／1024 维向量；模型采用 TinyModels 控制推理边界，不处理客户原件，不代表真实模型速度。

专项运行：[20261026-210529-92ada0](../runs/20261026-210529-92ada0/summary.json)，12 passed。其中连接行为 11 项＋基准 1 项。之后新增两项发布一致性检查，纳入全量回归。

该次基准每组 40 次轻量项目存在性读取，各执行 3 次：

| 对照 | 各次秒数 | 中位数 |
| --- | --- | --- |
| 每次新建短连接 | 1.988／1.386／1.801 | 1.801 秒 |
| 共享普通池 | 0.114／0.065／0.058 | 0.065 秒 |

池累计 120 次借用实际建立 2 条连接。首次试次包含建池冷启动，后两次复用；不能把这个轻量 SQL 对比倍数宣传为 OCR 或全链路加速倍数。原始证据：[summary.json](../runs/20261026-210529-92ada0/db-connections-baseline/summary.json)。

128 行向量写入的逐行／executemany 中位数分别为 0.0582／0.0580 秒；另一次全量运行中为 0.0570／0.0395 秒。收益受连接和磁盘波动影响，且样本不等于真实每页块数，目前没有足够证据改变生产发布策略，因此保留逐页事务内写入，微批仍归 RG-05j。对照不会作为易波动的自动测试速度阈值。

保存了当前有效索引查询的 [EXPLAIN ANALYZE／BUFFERS](../runs/20261026-210529-92ada0/db-connections-baseline/current-index-plan.json)：两页样本返回 2 行，执行约 0.28 ms，未发生临时块读写。该样本只能验证计划与来源过滤结构，不能证明大项目查询已优化；覆盖计数与元数据 N+1 仍归 RG-05i，不额外创建无规模证据的索引。

## 验证覆盖与运行命令

真实数据库检查：池复用与容量上限、排队上限／超时及恢复、异常回滚、会话设置／锁清理、断连接替换、专用租约不受归还影响、Web 共享与重复生命周期、借用中关闭、模型未返回时停止服务后保存暂停状态、API 503、CLI 退出无遗留会话。额外检查在 Embedding 期间另一会话修改原页后旧结果不能发布；片段插入失败后旧页及旧片段均完整保留。

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv-dev\Scripts\python.exe testing/run.py testing/tests/test_db_connections.py testing/tests/test_db_connections_baseline.py -q
.\.venv-dev\Scripts\python.exe testing/run.py -v -o faulthandler_timeout=60
```

全量回归：[20261026-210804-7ce7aa](../runs/20261026-210804-7ce7aa/summary.json)，**125 passed／2 skipped，236.22 秒**。本项 13 个连接／一致性用例及 1 个 SQL 基准均通过；另两项跳过的是默认未开启的真实 Qwen／视觉模型集成，本轮未重复跑模型质量测试。现有缓存、有界读取／增量刷新、双进程恢复和来源失效用例均在同一进程完整通过，此次未复现历史 TestClient 停滞。

本次 faulthandler 在停库阶段打印 60 秒等待栈；对应 server.log 明确记录 shutdown checkpoint 同步约 61.7 秒并正常结束，属于隔离集群落盘等待，没有测试失败。专项与全量测试库以及此前中断运行 20261026-143009-d30138 的隔离库均已确认停止；未改动测试治具或关闭 fsync。历史 portal 停滞原因不能仅凭这些通过结果进一步归因。

## 依赖与回退

主环境和开发环境已具备 psycopg-pool，声明及锁文件已同步。现有配置无须增加必填项，三个池配置均有默认值；模型环境无需安装该库。运行中的旧后端需重启才载入新代码。

无数据库迁移、无数据重建。回退时恢复原组合入口和服务构造方式、让 Database 使用独立短连接即可，保留已有页／向量数据；不要移除来源修订核对。锁顺序和逐页事务保持原实现，后续父子块发布再统一调整。

官方依据：[连接池生命周期与重置](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)、[容量、等待与关闭 API](https://www.psycopg.org/psycopg3/docs/api/pool.html)。
