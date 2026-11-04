# C-T02-a 材料分页基础

2026-10-22：本轮只实现 C-T02 的材料分页子项，MCP、证据覆盖／补读仍待后续任务。

- 新增项目绑定的 MaterialCatalog；只读 HTTP 入口 `GET /api/projects/{project_id}/material-catalog`。
- 默认每批 50、最多 100 项，UUID 升序游标，返回 next_cursor；跨项目／不存在游标拒绝，未知项目返回 404，无效边界返回 422。
- 返回文件名、登记状态、大小、已知页数、当前读取版本；不返回存储路径／全文。未知页数为 null，重复上传别名不重复材料；不将登记状态当作有效索引覆盖。遍历不是冻结快照。

测试先行 `20261022-194910-c4c122`：两项失败，接口尚不存在。实现后全套回归 `20261022-194957-25bdb2`：**69 passed, 2 skipped in 159.22s**，跳过项为本轮未重新执行的真实 VLM／Qwen 集成。隔离 PostgreSQL 日志确认正常关闭。测试和运行产物均在 testing/。

[测试日志](../runs/20261022-194957-25bdb2/console.log) · [测试结果](../runs/20261022-194957-25bdb2/summary.json)。

后端启动后验证：`http://127.0.0.1:8765/` 返回 200；`/api/health` 为 ready；现有项目材料分页返回 1／1 条。后台将原有 pause_requested 在检查点收束为 paused，未运行整卷 OCR／embedding。没有修改任务恢复机制，PR-05 不因本次正常启动而视为完整验收。运行日志及进程信息在 `.local/server/`。
