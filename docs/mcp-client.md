# MCP 客户端调用

本机只读服务，Windows：

```powershell
$env:DDA_MCP_API_KEY = '只保存在本机的长随机密钥'
.\.venv-dev\Scripts\python.exe -m dd_agent mcp
```

Linux / macOS 把解释器换成 `.venv-dev/bin/python`，并用 `export DDA_MCP_API_KEY='...'`。未设置密钥时进程直接退出。默认监听 `127.0.0.1:8766`。不要把密钥写进仓库或命令行参数。

## Streamable HTTP

新的地址是 `http://127.0.0.1:8766/mcp`。它和下面的旧 SSE 共用同一个进程、同一个端口和同一个密钥变量 `DDA_MCP_API_KEY`。

DeepSeek Harness（`@deepseek-ai/dsh` 0.2.2-rc.1，其 MCP 插件依赖 `@modelcontextprotocol/client` 2.2.0）只认这一种 HTTP。配置示例：

```yaml
- id: mcp-dda
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: dda
    transport: streamable-http
    url: http://127.0.0.1:8766/mcp
    expandTemplates: true
    headers:
      Authorization: Bearer ${credential:DDA_MCP_API_KEY}
```

`expandTemplates: true` 时，请求头里的 `${credential:DDA_MCP_API_KEY}` 会换成凭据里的密钥。凭据名按本机实际名称填写。每个请求都要带 `Authorization: Bearer <密钥>`；也可以只带 `X-API-Key`。两个头都有时以 `Authorization` 为准。不对返回 401，并带 `WWW-Authenticate: Bearer`。带了 `Origin` 时必须和 `Host` 一致，否则 403。

传输约定：

- `POST /mcp` 的 `Accept` 要同时包含 `application/json` 和 `text/event-stream`，否则 406。响应是单个 `application/json`，不开 SSE 流。
- 请求体只能有一条 JSON-RPC。只有通知或只有响应时返回 202，没有正文。批量请求返回 400。
- `initialize` 的响应头带 `Mcp-Session-Id`（随机十六进制）。之后的请求要带这个头，缺了返回 400；会话不存在或已过期返回 404。
- 会话在进程内存里，空闲 30 分钟删除，同时最多 128 个。重启后全部失效。
- 客户端请求的协议版本若是 `2025-03-26` 或 `2025-06-18`，原样返回；其他版本（包括官方 SDK 默认的 `2025-11-25`）返回 `2025-06-18`。
- 之后的请求若带了不受支持的 `MCP-Protocol-Version`，返回 400。不带这个头时按 `2025-03-26` 处理。
- `GET /mcp` 返回 405，没有服务端推送流。`DELETE /mcp` 带会话标识时结束会话并返回 204；会话不存在返回 404。
- 还没完成 `initialize` 就调用 `tools/list` 或 `tools/call` 时，JSON-RPC 错误码是 `-32002`。在这条传输上，没有会话标识的调用会先得到 HTTP 400。

先 `initialize`，再 `notifications/initialized`，然后 `tools/list` 或 `tools/call`。`tools/call` 的参数放在 `arguments` 里。

## 旧的 HTTP + SSE

`http://127.0.0.1:8766/sse` 和 `POST /messages?session_id=...` 仍然可用。这是协议 `2024-11-05` 的旧传输，现有客户端还在用。DeepSeek Harness 不会从 `/mcp` 回落到这里。密钥和来源校验与 `/mcp` 相同。

## 工具

| 工具 | 必填 | 作用 |
| --- | --- | --- |
| `list_projects` | 无 | 项目标识和名称 |
| `list_documents` | `project_id` | 该项目的材料。可选类别、主体、日期 |
| `search_evidence` | `project_id`、`query` | 该项目内的候选片段 |
| `coverage_gaps` | `project_id` | 未处理、失败、未知、空文本、待重建。计数不能相加 |
| `read_evidence` | `project_id`，以及 `evidence_id` 或 `document_id` | 按证据标识或来源范围补读，带长度上限和续读位置 |

`list_projects`、`list_documents`、`search_evidence` 的原有参数仍然可用。新调用不要省略 `project_id`。成功时 `content[0].text` 是 JSON 文本；工具失败时 `isError` 为 true。用别的项目的证据标识或材料标识会得到和“不存在”相同的拒绝，响应里没有对方正文，也不说明该标识是否存在。

过期补读返回拒绝，不把新正文填进旧引用。`read_evidence` 续读时带上上次的 `source_revision`、`resume_page` 和 `offset`；没有物理页码的材料用 `resume_unit`。

这是仓库内的调用说明。用 dsh 连上本机并实际调用这些工具，仍要在 Windows 本机验收。仓库内的 Node 脚本只验证传输和 `list_projects` 的形状，见 `testing/scripts/mcp/streamable_http_client.mjs`。
