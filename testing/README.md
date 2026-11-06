# 测试与验证统一目录

所有测试代码、试验脚本、样本记录和运行产物集中在这里。业务原件／数据库文件仍在配置的数据目录，模型和虚拟环境不属于测试资料。

| 目录 | 用途 | 是否影响应用运行 |
| --- | --- | --- |
| `tests/` | 当前正式自动测试，应保留 | 应用启动不依赖；修改代码后用它回归 |
| `scripts/quality/` | 图像质量试验脚本；样本输出只留本机，不入库 | 不参与日常业务流程 |
| `scripts/performance/` | 两页合成样本与真实／快速性能基线 | 仅独立测试，结果写 runs/ |
| `runs/` | 新测试产生的日志、XML、临时图片、隔离测试 PostgreSQL | 可重建；只应在对应测试库已停止后清理 |
| `cache/` | pytest 缓存与测试 Python 字节码 | 可重建，不是测试代码或客户原件 |
| `archive/` | 从旧 `.local` 迁入的历史 pytest 输出、已退役测试快照及移动清单 | 应用不读取；保留用于追溯 |

推荐从工程根目录运行：

```powershell
.\.venv-dev\Scripts\python.exe testing/run.py
```

同时验证已经安装的本地视觉／检索模型：

```powershell
.\.venv-dev\Scripts\python.exe testing/run.py --models
```

运行单个测试组：

```powershell
.\.venv-dev\Scripts\python.exe testing/run.py testing/tests/test_automatic.py
```

每次运行产物在 `runs/<时间-ID>/`，摘要写在该次目录的 `summary.json`。测试使用随机本机端口的隔离 PostgreSQL，不读取业务数据库连接。直接运行 `python -m pytest` 也可，临时目录默认在 `runs/current/`；不要并发使用同一个临时目录。

原先根目录 `.pytest_cache` 只是加速后续测试的缓存，不是应用文件。原先 `.local/pytest-*`、`retrieval-*`、`review-*` 等是历史测试产物，本次已保留到 `archive/runs/`，没有删除；这些目录不是应用必需文件。移动清单见 `archive/move-manifest.json`。运行中断时，先依据该次运行内 `postgres/<ID>/pgdata` 确认隔离测试库已停止，再清理该次产物；不要对业务数据目录执行测试清理。

私有样本、运行数据及归档不进入版本控制，也不要散落到 `.local` 或项目根目录。P1 早期自动验收在实现后曾到 16 passed；其后的数字以 `specs/01-evidence-to-report/tasks.md` 为准。

阶段基线可运行 `.\.venv-dev\Scripts\python.exe testing/scripts/performance/baseline.py`；加 `--models` 使用真实 OCR／视觉／向量／重排模型。仅处理两页合成样本并使用隔离库。2026-10-26 的结果是完整回归 88 passed／2 skipped，真实模型 1 passed；同版本复用时 OCR 调用为零。

阅读器行为回归位于 `tests/test_reader_regressions.py`，执行 `scripts/frontend/reader-regression.cjs`。需要 Node.js 22+：`node` 在 `PATH` 上，或设置 `DDA_TEST_NODE`。这组用真实前端脚本与受控 DOM／网络响应验证缓存失效、按需读取、失败切页和请求竞态，不需要安装浏览器或模型。
