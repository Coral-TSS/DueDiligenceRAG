# 尽调材料工作区

本地尽调材料工作区：导入 PDF、分页阅读、基础 OCR、按需视觉增强，以及向量和关键词混合检索。检索结果是候选证据。仓库保持私有。

不要提交客户材料、识别结果、数据库内容、模型权重或密钥。

## 启动

在仓库根目录、已经写好 `.env` 之后：

```powershell
.\.venv-dev\Scripts\python.exe -m dd_agent init-db
.\.venv-dev\Scripts\python.exe -m dd_agent check
.\.venv-dev\Scripts\python.exe -m dd_agent
```

Linux / macOS 把解释器换成 `.venv-dev/bin/python`。网页只监听 `http://127.0.0.1:8765`。`init-db` 按文件名顺序执行 `src/dd_agent/migrations/`（当前到 `019_material_metadata.sql`，健康检查要求 schema 19），可以重复执行，不删除已有材料。

只读 MCP 给另一台机器上的客户端列项目、列材料和检索。它不上传、不做 OCR，也不提供原件下载。启动命令不变。

```powershell
$env:DDA_MCP_API_KEY = '只保存在本机的长随机密钥'
.\.venv-dev\Scripts\python.exe -m dd_agent mcp
```

Linux / macOS 用 `export DDA_MCP_API_KEY='...'`。未设置密钥时进程直接退出。默认监听 `127.0.0.1:8766`；只有局域网访问才把 `DDA_MCP_HOST` 设为 `0.0.0.0`。不要把该端口暴露到公网。

新的客户端连接 `http://127.0.0.1:8766/mcp`（Streamable HTTP）。每个请求带 `Authorization: Bearer <密钥>`，也可以用 `X-API-Key`；两个头都有时以 `Authorization` 为准。DeepSeek Harness 使用 `@deepseek-ai/dsh-mcp-client`，配置示例：

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

`expandTemplates` 为 true 时，`${credential:...}` 才会换成本机凭据。不要把密钥写进仓库或命令行参数。`http://127.0.0.1:8766/sse` 仍由同一个进程提供，那是旧的 HTTP + SSE（协议 2024-11-05）。新的 Harness 只使用 `/mcp`，不会回落到 `/sse`。工具和调用顺序见 [MCP 客户端说明](docs/mcp-client.md)。原有工具 `list_projects`、`list_documents`、`search_evidence` 保持不变；另外有 `coverage_gaps` 和 `read_evidence`，除了列项目以外都要带 `project_id`。默认检索同时走向量和关键词，再按 RRF 融合（`DDA_RETRIEVAL_MODE=hybrid`）。

## 本机环境

需要 Python 3.12（低于 3.13）和已安装 pgvector 的 PostgreSQL。复制 `.env.example` 为 `.env`，填写 `DDA_DATABASE_URL`。连接串、密码和 API 密钥只留在本机。其余开关见 `.env.example`。

主环境的依赖范围在 `pyproject.toml`：

```powershell
py -3.12 -m venv .venv-dev
.\.venv-dev\Scripts\python.exe -m pip install -U pip
.\.venv-dev\Scripts\python.exe -m pip install -e ".[dev]"
```

PDF、OCR、视觉和检索各用一个解释器，避免把 Paddle 和 PyTorch 装进同一个环境。下面是默认目录，也可以用 `.env` 里的 `DDA_*_PYTHON` 指向别处。

| 环境 | 默认解释器 | 安装 |
| --- | --- | --- |
| PDF | `.venv-docling` | `pypdfium2` 与 `Pillow`。目录名沿用现有配置；这个环境用 PDFium 提取文字和渲染页面 |
| OCR | `.venv-ocr` | `paddleocr==3.7.0` 与 CPU 版 `paddlepaddle==3.3.1`。GPU 轮子见下方说明 |
| 视觉 | `.venv-vision` | CPU：`torch==2.14.1`、`torchvision==0.29.1`（PyTorch CPU 索引），以及 `transformers==5.20.1`、`pillow==12.3.0`、`safetensors==0.8.0`、`huggingface-hub==1.33.0` |
| 检索 | `.venv-retrieval` | CPU：`torch==2.14.1+cpu` 与 `transformers==4.57.6`，用于 Qwen3 Embedding / Reranker 的离线推理 |

GPU 包装进对应的 OCR、视觉或检索环境，不要装进 `.venv-dev`。GTX 1070 使用 PyTorch cu126 且 `torch<2.15`，Paddle 使用 cu118。RTX 40 系使用 PyTorch cu128，Paddle 使用 cu126 或 cu129。命令、显存和核对方式见 [GPU 安装](specs/due-diligence-agent/gpu-setup.md)。视觉环境的 CPU 步骤见 [视觉模型安装](specs/due-diligence-agent/vision-model-installation-guide.md)。

## 模型

权重放在本机。`models/` 已被忽略。程序启动时不会为了补齐权重去联网下载。

| 用途 | 名称 | 默认位置 |
| --- | --- | --- |
| 基础 OCR | `PP-OCRv5_mobile_det`、`PP-OCRv5_server_rec`、`PP-LCNet_x1_0_doc_ori`、`PP-LCNet_x1_0_textline_ori` | `models/ocr/official_models/<名称>/`，目录内要有 `inference.pdiparams` |
| 区域视觉 | `PaddlePaddle/PaddleOCR-VL-1.6` | `models/vision/PaddleOCR-VL-1.6` |
| 向量 | `Qwen/Qwen3-Embedding-0.6B`（1024 维） | `DDA_EMBEDDING_DIR`；未设置时为 `D:/AgentData/dd-agent/models/Qwen3-Embedding-0.6B` |
| 重排序 | `Qwen/Qwen3-Reranker-0.6B` | `DDA_RERANKER_DIR`；未设置时为 `D:/AgentData/dd-agent/models/Qwen3-Reranker-0.6B` |

## 数据

业务原件、页面图和运行记录在 `DDA_DATA_DIR`（默认 `.local/data`）。数据库里只保留连接配置和迁移脚本，不保留转储或客户数据。请同时备份 PostgreSQL 和数据目录。

## 测试

```powershell
.\.venv-dev\Scripts\python.exe testing/run.py
```

测试使用隔离的 PostgreSQL，不读取业务连接。前端脚本回归需要 Node.js 22+：`node` 在 `PATH` 上，或设置 `DDA_TEST_NODE`。说明见 [testing/README.md](testing/README.md)。

`specs/` 保留行为说明和上面两份安装附录。任务状态以 [specs/01-evidence-to-report/tasks.md](specs/01-evidence-to-report/tasks.md) 为准。2026-11-07 的项目中心规格、测试设计和方案与它放在同一目录，确认前还没有对应功能。早期任务清单和方案记录不能代替本文的安装与启动步骤。
