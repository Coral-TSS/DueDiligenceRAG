# C-T01 Harness 运行基线验收

日期：2026-10-22。范围：固定运行环境、profile、工具发现／执行、启动／退出／跨进程恢复；不含实际业务证据或外网生成服务。

## 实现与实测

- 官方 Python SDK／runtime wheel `0.2.1rc2`，实际后端使用官方 npm CLI `0.2.1-rc.2`＋独立 Node `22.19.0`。安装在 `.venv-harness` 与 `.local/harness`，没有升级系统 Node 或改动已有模型环境。
- Windows packed runtime 首轮调用报 snapshot 内 `koffi.node` 无法加载，不能仅凭初始化成功当成可用。改用同版本 npm CLI 的真实文件依赖后，完整工具回合成功。
- `sdk-minimal`＋生产 patch；关闭通用 shell／文件模型工具、工作目录模型工具、DeepSeek session-log 扩展、插件清单；启用官方会话 checkpoint-policy。配置 MCP 后官方资源工具三项仍保留，专项测试核对实际发送的工具定义。
- 启动工作进程使用既有环境清理机制，不传入主应用的数据库／云端凭据。home 与 workspace 分开，使用 SDK context manager 退出。`harness-check` 可在无数据库连接和模型凭据时运行，模型请求数为 0。
- 实际 stdio MCP 发现 `baseline_status`，模型协议使用本机合成 Messages SSE；Harness 执行工具，返回结果后得到 `HARNESS_OFFLINE_OK`。测试端点收到恰好两次请求，无 `dsh_session_log` 扩展。
- 关闭第一个 Harness 后，新进程从相同 home 恢复 session_id，保留已设置的工作目录；恢复未产生额外模型请求。此项验证会话持久化，不是 OCR／embedding 工作进程恢复。

## 测试记录

测试先行：`20261022-172909-fb5b9e` 初次 3 项失败，原因为运行适配与 profile 缺失。后续暴露 packed runtime 原生模块错误，并校准模拟服务为该版本实际使用的 Messages 协议。最终专项：

- `20261022-173829-140c6a`：**3 passed in 7.49s**。
- [专项日志](../runs/20261022-173829-140c6a/console.log) · [专项结果](../runs/20261022-173829-140c6a/summary.json)。

全套应用回归 `20261022-173849-ff721e`：**67 passed, 2 skipped in 161.23s**，无失败／错误。两项跳过为真实 VLM／Qwen 模型集成；本轮未重新运行它们，其最近全套实测为 2026-10-21 的 66 passed。隔离 PostgreSQL 日志确认 `database system is shut down`，正常停止。

[全套日志](../runs/20261022-173849-ff721e/console.log) · [全套结果](../runs/20261022-173849-ff721e/summary.json) · [最近运行](latest-run.md)。测试代码、模拟服务和输出均位于 testing/。

## 完成边界与下一步

C-T01 完成，C-T02–07 待实施。基线命令 `report_tools_connected=false`、`report_generation_verified=false` 是预期值。没有真实生成凭据验证、报告事实验收或 Word 产物；未继续客户 649 页材料处理，也未修改残留 pause_requested 状态。PR-05 仍待单独修复。

下一步 C-T02：项目绑定的材料覆盖、分页清单、搜索＋重排、指定证据与页范围补读 MCP；补齐真实来源、版本／过期状态和跨项目拒绝测试。安装与重建说明见 [harness-runtime/README.md](../../harness-runtime/README.md)。
