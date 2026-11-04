# RG-05g1／g2：六项复审问题修复

日期：2026-10-26。关联 [复审发现与原始复现](rg05-code-review-20261026.md)，契约 K02／PA-03、04、06。任务状态只在唯一 tasks.md 维护。

## 修复范围

| 原问题 | 当前实现 |
| --- | --- |
| 增强完成仍显示旧文字 | 全卷 content_revision 变化后，只取选中页一条目录项核对 source_revision；该页变化才重取有效文字与摘要，其他页变化不下载当前页正文。 |
| 基础识别区混入增强文字 | 当前文字用 effective；基础详情展开才请求 raw＋行框，并绑定 raw_revision。两份文字各自缓存，增强摘要不再覆写当前正文。 |
| 切页失败保留上一页证据 | 切页立即清理旧图片、正文、标题、行框和历史；AbortController 加页代次阻止迟到响应回写，图片事件也验证当前页代次。 |
| 切页自动下载全历史 | 新增 `GET pages/{number}/summary`，只投影生效机器结果的 outcome／route／regions 及候选／人工历史数量，无正文／历史数组。旧 automatic／review 接口保留兼容，新阅读器不调用。机器历史展开后用既有分页接口每批 10 条、每条 500 字预览，下一批替换显示；正文、原文、摘要各限一页缓存。 |
| 多条历史同时标当前 | 单页 SQL 取得与有效视图相同的最新机器记录 ID；基础修订匹配后只有该 ID 的 is_current 为真，另设 base_matches_current 表示是否来自当前基础页。跨历史分页仍按实际生效记录判断，旧页或过期机器结果不能标当前。 |
| 轮询重入和迟到目录覆盖 | 在途 Promise 串行执行轮询，重复触发合并为完成后追加一轮；定时器仅在最后一轮结束后安排。目录请求另有代次，迟到旧目录不提交，切页不会被先前轮询强行切回。 |

涉及 `db.py`、`page_queries.py`、`app.py`、`static/reader.js`、`static/reader.html`。无迁移，不重建已有 OCR 或向量，不改模型及后台调度。页面可继续只读查看历史机器版本；原人工记录与候选数据未删除，旧只读接口保持可用。

## 测试驱动证据

- 修复前，[20261026-213721-f7db2b](../runs/20261026-213721-f7db2b/summary.json)：9 failed／9 passed。失败覆盖旧正文、原始文字误展示、失败切页、历史读取、轮询重入、迟到目录，以及后端历史当前标识／缺少摘要接口。
- 首轮修复后，[20261026-214108-8ebda4](../runs/20261026-214108-8ebda4/summary.json)：22 passed（新前端行为 8 项＋页读取 10 项＋原增量刷新 4 项）。
- 正式全量回归：[20261026-214347-e71943](../runs/20261026-214347-e71943/summary.json)，**135 passed／2 skipped，233.40 秒**。两项为默认未开启的真实 Qwen／视觉模型集成测试。原缓存、数据库连接、检索、双进程恢复与工作区用例均通过；隔离库正常停止。日志中的 60 秒等待栈对应停库 checkpoint 同步约 65 秒，不是测试失败。

新正式用例在 `testing/tests/test_reader_regressions.py` 和 `test_page_reads.py`。前端执行真实 reader.js，使用 Node 22 VM 和受控 DOM／网络；包括当前页更新、其他页更新不拉正文、原始文字按需读取、失败切页、历史懒加载、轮询串行、迟到正文与迟到目录隔离。后端使用真实隔离 PostgreSQL／FastAPI，30 条、每条 20,000 字的合成历史下，摘要响应仍小于 2,000 字节；同一基础页 3 次增强跨 3 个历史批次仅一个 is_current。还验证摘要来源过期为 409、跨项目为 404。

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv-dev\Scripts\python.exe testing/run.py testing/tests/test_reader_regressions.py testing/tests/test_page_reads.py testing/tests/test_page_refresh.py -q
.\.venv-dev\Scripts\python.exe testing/run.py -v -o faulthandler_timeout=60
```

## 使用与边界

后端运行中的旧进程需重启；浏览器刷新后载入新脚本。没有新增安装项；前端回归使用项目已有 Node 22，其他电脑可设置 DDA_TEST_NODE。

这次验证没有运行真实浏览器截图或重新进行 OCR／模型质量评测。受控 DOM／网络测试不能替代真实浏览器视觉验收，真实模型两项仍按原开关运行；修复范围是页读取、历史与刷新正确性，不宣称 OCR 推理提速。过长单页仍受既有 100,000 字符接口切片约束，UI 明示当前展示长度，完整续读接口保留。
