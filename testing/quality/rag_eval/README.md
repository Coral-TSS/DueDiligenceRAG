# RAGAS / TruLens 本机评测

这是尽调检索质量的本机评测包。它在进程内调用现有 `EvidenceService.search`，再用 RAGAS 或 TruLens 打分。产品本身只做检索；生成答案只为答案类指标服务，并且只能根据检索片段用中文作答。

它不是下面两套东西：

- 不是 Harness 那条线的 dsh-eval。
- 不是 RG-04 工程固定集（`testing/quality/fixed_set/`，OCR / 召回 / 重排工程指标）。

黄金集由使用者在本机准备，可以放在仓库外。仓库里只有合成的 `dataset.example.json`。不要提交客户材料、真实问答、评测分数、密钥或 `.env`。

## 安装

使用独立的 Python 3.12 环境 `.venv-eval`。不要装进 `.venv-dev`，也不要写进产品的 `install_requires`。

在 `D:\CodeWork\DueDiligenceRAG` 下：

```powershell
py -3.12 -m venv D:\CodeWork\DueDiligenceRAG\.venv-eval
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe -m pip install -U pip
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe -m pip install -e D:\CodeWork\DueDiligenceRAG
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe -m pip install -r D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\requirements.txt
```

Linux / macOS 在仓库根目录执行同样的四条命令，把 `Scripts\python.exe` 换成 `.venv-eval/bin/python`。先 `pip install -e` 本仓库，再安装评测依赖。

检索仍使用仓库根目录 `.env` 里的 `DDA_DATABASE_URL` 和本地嵌入 / 重排模型。本包不改生产配置，不改索引版本，也不另建向量库。

## 裁判模型

只在生成答案和打分时使用。`--retrieve-only` 不读这些变量。

```powershell
$env:EVAL_LLM_API_KEY="只留在本机会话里的密钥"
$env:EVAL_LLM_BASE_URL="https://api.deepseek.com"
$env:EVAL_LLM_MODEL="deepseek-flash"
```

未设置 `EVAL_LLM_API_KEY` 时回退到 `OPENAI_API_KEY`。未设置 `EVAL_LLM_BASE_URL` 时回退到 `OPENAI_BASE_URL`。只使用 OpenAI 时，设置 `OPENAI_API_KEY` 即可。未设置 `EVAL_LLM_MODEL` 时，接口地址里包含 `deepseek` 则用 `deepseek-flash`，否则用 `gpt-4o-mini`。`deepseek-chat` 已在 2026-07-24 停用，不要再把它当作默认模型。显式设置 `EVAL_LLM_MODEL` 时仍用该名字。

`deepseek-flash` 默认开启思考。思考写在 `reasoning_content` 里，并占用补全额度。额度用尽时 `finish_reason` 为 `length`，`message.content` 为空，RAGAS 读不到结构化结果。生成答案、RAGAS 裁判和 TruLens 裁判在 DeepSeek 上都会关闭思考。请求通过 OpenAI 兼容的 `extra_body` 发送：

```json
{"thinking": {"type": "disabled"}, "reasoning_effort": "none"}
```

`thinking.type=disabled` 优先于 `reasoning_effort`。同一请求把 `max_tokens` 设为 8192，用来盖过 RAGAS 默认的 1024，并与 DeepSeek 非思考模式的默认补全额度同一量级。接口地址包含 `deepseek`，或模型名以 `deepseek` 开头时才附加这些字段；其他接口不改。请求不声明 `tools`，因此不会打开 `deepseek_web_search`、`deepseek_read_url` 或其他工具。`deepseek-flash` 在非思考模式下会忽略传入的 `temperature` 和 `top_p`。

RAGAS 通过 OpenAI 兼容客户端调用 `llm_factory(..., provider="openai")`，因此 DeepSeek 的 `base_url` 会保留。思考开关会同时放进 `llm_factory` 的参数，并绑在客户端的 `chat.completions.create` 上；instructor 若丢掉 `extra_body`，绑定仍会补上。TruLens 收到的是同一个已经绑好的客户端。它若先走 Responses API，则用 `reasoning.effort=none` 关闭思考。日志只打印模型名、是否已配置密钥，以及思考模式是否已关闭，不打印密钥。兼容接口若不接受 JSON `response_format`，可再设 `EVAL_RAGAS_INSTRUCTOR_MODE=md_json`。

## 数据集

`--dataset` 指向一个 JSON 文件，路径可以在仓库外。顶层字段：

| 字段 | 要求 |
| --- | --- |
| `project_id` | 必填 UUID。全零占位符 `00000000-0000-0000-0000-000000000000` 会被拒绝 |
| `items` | 非空数组 |
| `customer_data` | 可选。示例为 false。真实集不要提交 |

每条 `items`：

| 字段 | 要求 |
| --- | --- |
| `question` | 必填，1–500 个字符，与检索接口一致 |
| `id` | 可选。缺省时写成 `item-1` 这种序号 |
| `reference` | 可选字符串。空字符串视为没有参考答案 |
| `document_id` | 可选 UUID。不能和 `document_ids` 同时出现 |
| `document_ids` | 可选 UUID 数组。空数组表示不按材料过滤 |
| `categories` | 可选类别代码数组，须是当前材料目录中的代码，例如 `finance`。空数组表示不按类别过滤 |
| `subject_ids` | 可选主体 UUID 数组 |
| `date_point` | 可选 `YYYY-MM-DD`。不能和期间同时填写 |
| `period_start` / `period_end` | 可选期间，须同时给出，格式 `YYYY-MM-DD` |
| `page_from` / `page_to` | 可选页范围，须同时给出正整数，且起页不大于止页 |

过滤交给 `dd_agent.material_metadata.parse_filters`，再作为 `scope` 传入 `EvidenceService.search`。材料标识只放在 `scope` 里，不再同时传 `document_id` 关键字，避免两边一起出现时被检索层拒绝。RAGAS 和 TruLens 使用同一条检索路径，每条都可以带自己的过滤条件。

`EvidenceService.search` 会返回页级命中、父子命中，以及 Office 来源单元命中。页级路径在 `coverage.office_chunks` 非 0 时合并 `source_unit` / `source_unit_chunks`；混合路径也会查询这些 Office 片段。评测只抽取文本：扁平命中（含 `index_kind=source_unit`）用 `text`；父子命中用 `matched_children[].text`。`children` / `chunks` 只为旧结果形状保留。子块没有文本时才使用 `context.text`。

复制示例后改成本机项目，并放到仓库外，例如 `D:\EvalData\rag\dataset.local.json`：

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_ragas.py --dataset D:\EvalData\rag\dataset.local.json --retrieve-only
```

示例文件里的全零 `project_id` 不能直接跑，`--project-id` 也不能把全零 UUID 再传进去。

## 只检索

不生成答案，也不调用裁判模型。仍会连接 `.env` 中的数据库并加载本地检索模型。

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_ragas.py --dataset D:\EvalData\rag\dataset.local.json --retrieve-only
```

TruLens 脚本同样支持，此时不会打开 TruLens，也不会打分：

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_trulens.py --dataset D:\EvalData\rag\dataset.local.json --retrieve-only
```

## RAGAS

离线批量打分。Faithfulness 不需要 reference。只有每条都有非空 `reference` 时才计算 ContextRecall（当前集合指标，对应以前的 LLMContextRecall）和 FactualCorrectness；否则跳过这两项并在终端和 `ragas_scores.json` 里写明缺了哪些条目。

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_ragas.py --dataset D:\EvalData\rag\dataset.local.json
```

可用 `--project-id` 覆盖数据集中的项目 UUID，用 `--out` 指定结果目录。

检索和生成已经写进 `records.json` 之后，可以只打分。仍然需要裁判模型密钥，但不连接数据库，不加载本地检索模型，也不再调用生成。

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_ragas.py --score-from D:\EvalData\rag\output\records.json --out D:\EvalData\rag\output
```

这条命令只写 `ragas_scores.json` 和 `ragas_scores.csv`。它不改写 `records.json` 和 `traces.json`，即使 `--out` 就是这两份文件所在的目录。不能和 `--dataset`、`--retrieve-only` 或 `--project-id` 一起用。

记录文件是上面这条 RAGAS 命令写出的 JSON 数组，也可以是含非空 `records` 数组的对象。每条要有 `user_input`（或 `question`）、字符串数组 `retrieved_contexts`，以及非空 `response`（或 `answer`）。`reference` 可空；缺了的条目仍按上面的规则跳过 ContextRecall 和 FactualCorrectness。`--retrieve-only` 留下的空答案不能打分。

## TruLens

追踪 `retrieve`（RETRIEVAL）、`generate_completion`（GENERATION）和 `query`（RECORD_ROOT）。反馈是幻觉三角：Groundedness、Answer Relevance、Context Relevance。`query` 会把该条的过滤字段传给 `retrieve`。

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_trulens.py --dataset D:\EvalData\rag\dataset.local.json --app-version local
```

对比两次运行时换一个版本名，并使用同一个 `--out`，这样会写进同一个 `trulens.sqlite`：

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_trulens.py --dataset D:\EvalData\rag\dataset.local.json --app-version candidate --out D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\output
```

同一份 `records.json` 也可以交给 TruLens。它按 `user_input` 回放已经保存的片段和答案，再让裁判模型打幻觉三角。不调用 `EvidenceService`，也不再生成答案。问题文本不能重复。它会写 `trulens_answers.json`、能取到时的 `trulens_leaderboard.json`，以及 `trulens.sqlite`；同样不改写 `records.json` 或 `traces.json`。

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_trulens.py --score-from D:\EvalData\rag\output\records.json --out D:\EvalData\rag\trulens-score --app-version scored
```

评测结束后打开本机面板（会一直占用这个进程，直到面板关闭）：

```powershell
D:\CodeWork\DueDiligenceRAG\.venv-eval\Scripts\python.exe D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\run_trulens.py --dataset D:\EvalData\rag\dataset.local.json --app-version local --dashboard
```

## 输出

默认目录是 `D:\CodeWork\DueDiligenceRAG\testing\quality\rag_eval\output`。

| 文件 | 何时产生 |
| --- | --- |
| `records.json` | RAGAS 路径。含问题、检索片段、答案和 reference |
| `traces.json` | 检索状态、片段数、coverage 和本条过滤条件 |
| `ragas_scores.json` / `ragas_scores.csv` | 完整 RAGAS 打分。`--score-from` 只写这两个分数文件 |
| `trulens_answers.json` | TruLens 路径的答案、片段数和过滤条件 |
| `trulens_leaderboard.json` | 能取到排行榜时写出 |
| `trulens.sqlite` | TruLens 会话库，按 `--app-version` 累积 |

这些文件可能含检索到的材料原文，不要提交。

## 不要提交

- `.venv-eval/`
- `dataset.local.json` 以及其他 `*.local.json`
- `testing/quality/rag_eval/output/`
- 客户材料、真实公司或项目名称、参考答案、评测分数、密钥、`.env`

自动测试在 `testing/tests/test_rag_eval.py`。它只用合成夹具，不连接数据库，不加载模型，也不需要裁判密钥。未安装 ragas / trulens 时，依赖这两个包的导入检查会跳过，`.venv-dev` 的默认测试仍然可以跑。
