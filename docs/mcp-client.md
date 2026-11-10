# MCP 客户端调用

本机只读服务：

```text
python -m dd_agent mcp
```

先设置环境变量 `DDA_MCP_API_KEY`。未设置时进程直接退出。默认监听 `127.0.0.1:8766`。客户端连接 `http://127.0.0.1:8766/sse`，每个请求带 `Authorization: Bearer <密钥>`。不要把密钥写进仓库或命令行参数。

传输是 HTTP 和 SSE。先 `initialize`，再 `notifications/initialized`，然后 `tools/list` 或 `tools/call`。`tools/call` 的参数放在 `arguments` 里。

工具：

| 工具 | 必填 | 作用 |
| --- | --- | --- |
| `list_projects` | 无 | 项目标识和名称 |
| `list_documents` | `project_id` | 该项目的材料。可选类别、主体、日期 |
| `search_evidence` | `project_id`、`query` | 该项目内的候选片段 |
| `coverage_gaps` | `project_id` | 未处理、失败、未知、空文本、待重建。计数不能相加 |
| `read_evidence` | `project_id`，以及 `evidence_id` 或 `document_id` | 按证据标识或来源范围补读，带长度上限和续读位置 |

`list_projects`、`list_documents`、`search_evidence` 的原有参数仍然可用。新调用不要省略 `project_id`。用别的项目的证据标识或材料标识会得到和“不存在”相同的拒绝，响应里没有对方正文，也不说明该标识是否存在。

过期补读返回拒绝，不把新正文填进旧引用。`read_evidence` 续读时带上上次的 `source_revision`、`resume_page` 和 `offset`；没有物理页码的材料用 `resume_unit`。

这是仓库内的调用说明。教练 Bot 那边 Harness 的冻结调用还没有在本仓库确认，对接时不要把合成测试写成真实材料已经外发验收。
