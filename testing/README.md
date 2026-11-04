# 测试与验证统一目录

所有测试代码、试验脚本、样本记录和运行产物集中在这里。业务原件／数据库文件仍在配置的数据目录，模型和虚拟环境不属于测试资料。

| 目录 | 用途 | 是否影响应用运行 |
| --- | --- | --- |
| `tests/` | 当前正式自动测试，应保留 | 应用启动不依赖；修改代码后用它回归 |
| `scripts/quality/` | 图像质量试验脚本，读取 `records/quality/` | 不参与日常业务流程 |
| `scripts/harness/` | 本机模拟模型端点配套的 SDK／MCP／恢复验证 | 不参与报告生成，不接触客户材料 |
| `scripts/performance/` | 两页合成样本与真实／快速性能基线 | 仅独立测试，结果写 runs/ |
| `scripts/legacy/` | 早期安装／样例／诊断脚本，供追溯 | 不参与日常业务流程 |
| `records/` | 验收文档、私有样本结果与最近运行入口 | 是验证证据，不是业务数据库 |
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

每次运行产物在 `runs/<时间-ID>/`，结果入口为 [最近运行](records/latest-run.md)。测试使用随机本机端口的隔离 PostgreSQL，不读取业务数据库连接。直接运行 `python -m pytest` 也可，临时目录默认在 `runs/current/`；不要并发使用同一个临时目录。

原先根目录 `.pytest_cache` 只是加速后续测试的缓存，不是应用文件。原先 `.local/pytest-*`、`retrieval-*`、`review-*` 等是历史测试产物，本次已保留到 `archive/runs/`，没有删除；这些目录不是应用必需文件。移动清单见 `archive/move-manifest.json`。运行中断时，先依据该次运行内 `postgres/<ID>/pgdata` 确认隔离测试库已停止，再清理该次产物；不要对业务数据目录执行测试清理。

私有样本、运行数据及归档默认不进入版本控制。`records/p1-verification.md` 保存历史验收事实，后续记录也放在 `records/`，不再向 `.local` 或项目根目录散落测试文件。

阶段基线可运行 `.\.venv-dev\Scripts\python.exe testing/scripts/performance/baseline.py`；加 `--models` 使用真实 OCR／视觉／向量／重排模型。仅处理两页合成样本并使用隔离库。口径与结果见 [性能基线记录](records/performance-baseline.md)。

阅读器行为回归位于 `tests/test_reader_regressions.py`，执行 `scripts/frontend/reader-regression.cjs`，默认使用项目已有的 `.local/harness/runtime/node-v22.19.0-win-x64/node.exe`；其他电脑可通过 `DDA_TEST_NODE` 指定 Node.js 22+。这组用真实前端脚本与受控 DOM／网络响应验证缓存失效、按需读取、失败切页和请求竞态，不需要安装浏览器或模型。`scripts/review/` 的旧探针记录修复前的错误行为，属于历史调查证据，不作为修复后验收入口。
