# Harness 运行基线（C-T01）

2026-10-22，本机已安装并通过启动、工具调用和跨进程会话恢复测试。无需重复安装。此阶段未接入业务 RAG、Word 或外网生成服务。

## 固定版本与目录

| 内容 | 版本／目录 |
| --- | --- |
| Python | 3.12，独立 `.venv-harness` |
| 官方 Python SDK／附带 runtime wheel | `0.2.1rc2`；完整 Python 依赖在根目录 `requirements-harness.lock.txt` |
| 实际执行的官方 CLI | `@deepseek-ai/dsh@0.2.1-rc.2`，`.local/harness/cli`；此目录的 package-lock.json 固定依赖 |
| 独立 Node | `v22.19.0`，`.local/harness/runtime/node-v22.19.0-win-x64` |
| profile | 官方 `sdk-minimal`＋`src/dd_agent/agent_profiles/dd-report.patch.yml` |
| 本地会话 | `.local/harness/home`，JSONL 持久化和官方 checkpoint-policy |
| 基线工作目录 | `.local/harness/workspace`，与 home 分开 |

本机实测：官方 Windows runtime wheel 能初始化，但首轮任务加载 snapshot 内的 `koffi.node` 失败。因此保留官方 Python SDK，通过 `dsh_bin` 指向同版本 npm CLI；不修改 SDK，不自建 Agent 循环，不升级系统 Node。CLI 依赖约 600 个包，安装目录属于运行环境，未放进测试目录。

## 运行与边界

从工程根目录运行：

```powershell
.\.venv-dev\Scripts\python.exe -m dd_agent harness-check
```

`status=ready` 仅表示版本、profile 和 SDK 启动／退出检查通过；该命令没有模型请求、不访问业务数据库，不代表报告功能已经完成。诊断输出显式返回 `report_tools_connected=false` 和 `report_generation_verified=false`。日志在配置数据目录的 `harness-diagnostics/`。

可选配置：`DDA_HARNESS_PYTHON`、`DDA_HARNESS_NODE`、`DDA_HARNESS_DSH_BIN`、`DDA_HARNESS_HOME`、`DDA_HARNESS_WORKSPACE`，留空采用上述默认目录。home／workspace 必须互不包含。

生产 patch 关闭通用 Bash、PowerShell、文件系统和 working-directory 模型工具，关闭 DeepSeek session-log 扩展和插件包清单。SDK 的工作目录元数据功能仍可用。未配置 MCP 时没有业务工具；配置 MCP 后，官方客户端另提供 `list_mcp_resources`、`list_mcp_resource_templates`、`read_mcp_resource`。后续只按明确清单添加项目绑定的证据／Word 工具。

后续嵌入使用官方 `DeepSeekHarness`／`HarnessClient` 子进程 SDK。启动时设置独立 Node 的 PATH、home、profile 和 patch，退出使用 SDK context manager；恢复使用相同 home／workspace 和 session_id。当前没有报告聊天页面、外网模型配置入口或尽调 skill，不能直接从材料页面生成报告。

## 在新机器重建

以下命令以 Windows x64、工程根目录为前提，安装时需联网；现有机器无需执行。Python 解释器可以换成已安装的 3.12 路径。Node 下载后先验证摘要，再解压；不要覆盖已有部署。

```powershell
uv venv --python 3.12 .venv-harness
uv pip sync --python .venv-harness\Scripts\python.exe requirements-harness.lock.txt
New-Item -ItemType Directory -Force .local/harness/runtime, .local/harness/cli | Out-Null
Invoke-WebRequest 'https://nodejs.org/dist/v22.19.0/node-v22.19.0-win-x64.zip' -OutFile .local/harness/runtime/node-v22.19.0-win-x64.zip
$harnessArchiveHash = (Get-FileHash .local/harness/runtime/node-v22.19.0-win-x64.zip -Algorithm SHA256).Hash
if ($harnessArchiveHash -ne 'ea3fad0e67a991d8477d8c01344b56e69c676ccb733f065b22436994b1253f86') { throw 'Node archive checksum mismatch' }
Expand-Archive .local/harness/runtime/node-v22.19.0-win-x64.zip -DestinationPath .local/harness/runtime
Copy-Item harness-runtime/package.json, harness-runtime/package-lock.json .local/harness/cli/
$harnessNodeRoot = (Resolve-Path .local/harness/runtime/node-v22.19.0-win-x64).Path
$harnessOriginalPath = $env:PATH
try {
    $env:PATH = "$harnessNodeRoot;$env:PATH"
    Push-Location .local/harness/cli
    try {
        & "$harnessNodeRoot/node.exe" "$harnessNodeRoot/node_modules/npm/bin/npm-cli.js" ci --ignore-scripts --omit=dev --no-audit --no-fund --registry https://registry.npmjs.org
        if ($LASTEXITCODE -ne 0) { throw 'Harness npm installation failed' }
    } finally { Pop-Location }
} finally { $env:PATH = $harnessOriginalPath }
.\.venv-dev\Scripts\python.exe -m dd_agent harness-check
```

Node SHA256 已与官方 SHASUMS256.txt 核对。npm lock 保存解析地址和 integrity；不要以 `latest` 替换固定版本。SDK 的附带 wheel 仍会作为依赖安装，但本机实际使用 npm 后端。新机器应重新运行 Harness 专项测试后再使用。

## 验证

```powershell
.\.venv-dev\Scripts\python.exe testing/run.py testing/tests/test_harness.py -q
```

真实 SDK／CLI＋本地 stdio MCP，模型端点为本机合成 Messages SSE 服务；检查工具发现、实际调用、返回续传、最小工具清单、未附加 `dsh_session_log`，以及新进程恢复目录且不再请求模型。它不证明真实外网服务兼容、报告事实正确或 Word 排版可用。

实现依据：[官方 Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)、[MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)。
