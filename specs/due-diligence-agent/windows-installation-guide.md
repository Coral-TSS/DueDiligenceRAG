# Windows 原生安装：版本与目录建议

核对日期：2026-10-18  
关联文档：[准备清单](./preparation-checklist.md) · [产品规格](./spec.md)

状态：已核对公开发行资料，尚未在本机安装、解析完整依赖或做运行测试。下表为首次安装验证的候选版本，不是已验证的完整依赖锁文件。安装通过后记录全部实际依赖和模型修订版本。

## 1. 项目目录可以保留

代码继续放在 `D:\CodeWork\尽调报告Agent`，不需要重命名。Python 库通过包管理器安装到独立环境，不需要把 GitHub 仓库逐个解压到代码目录。

建议原生程序、数据库数据、模型缓存使用独立的短英文路径，便于控制空间、备份，以及降低第三方构建工具遇到路径问题的可能。这里的目录均为建议，没有创建或迁移任何文件。

## 2. 首次安装验证版本

| 组件 | 候选版本／型号 | 下载或安装内容 | 建议位置 |
| --- | --- | --- | --- |
| Python | CPython **3.12.15，Windows x86-64** | 用 uv 获取维护版本；官方该补丁发布页仅提供源码，不是可双击的 Windows 安装包 | uv 管理的解释器根目录设为 `D:\Tools\Python` |
| PostgreSQL | **17.11，Windows x86-64** | 从 PostgreSQL 官网入口获取 Windows 安装程序；同一 17 系列有更新安全补丁时复核后采用 | 程序 `D:\Apps\PostgreSQL\17`；数据 `D:\AgentData\dd-agent\postgres` |
| pgvector 服务端扩展 | **0.8.7** | 安装与 PostgreSQL 17 匹配的扩展；官方原生 Windows 路径为源码构建 | 由构建安装步骤写入 PostgreSQL 的 `lib` 和 `share\extension` 等对应位置 |
| LangGraph | **1.2.16** | Python 包 `langgraph==1.2.16`；持久化组件另按依赖兼容性解析并锁定 | 项目 `.venv` |
| docxtpl | **0.20.2** | Python 包 `docxtpl==0.20.2`，其依赖由包管理器处理 | 项目 `.venv` |
| Docling | **2.140.0** | Python 包 `docling==2.140.0`，另预下载所选解析流水线的模型 | 项目 `.venv-docling`；模型另存 |
| PaddleOCR | **3.7.0** | Python 包 `paddleocr==3.7.0`；使用文档版面和表格流水线时补齐相应可选依赖 | 项目 `.venv-ocr` |
| PaddlePaddle | **3.3.1 CPU 版** | Python 包 `paddlepaddle==3.3.1`；与 PaddleOCR 同环境，首轮不装 GPU 版 | 项目 `.venv-ocr` |
| 向量模型 | **Qwen/Qwen3-Embedding-0.6B** | 完整模型快照：权重、分词器、配置；不是只下载 GitHub 示例代码 | `D:\AgentData\dd-agent\models\Qwen3-Embedding-0.6B` |
| 重排序模型 | **Qwen/Qwen3-Reranker-0.6B** | 完整模型快照；不用自行训练 | `D:\AgentData\dd-agent\models\Qwen3-Reranker-0.6B` |
| 检索推理环境 | PyTorch CPU＋Transformers，按上述模型解析兼容依赖并锁定 | 不预装 CUDA、FlashAttention 或 vLLM；这些不是本地 CPU 推理的前提 | 项目 `.venv-retrieval` |

版本来源：[Python 3.12.15](https://www.python.org/downloads/release/python-31215/)、[PostgreSQL 版本表](https://www.postgresql.org/support/versioning/)、[pgvector](https://github.com/pgvector/pgvector)、[LangGraph](https://pypi.org/project/langgraph/)、[docxtpl](https://pypi.org/project/docxtpl/)、[Docling](https://pypi.org/project/docling/)、[PaddleOCR](https://pypi.org/project/paddleocr/)、[PaddlePaddle](https://pypi.org/project/paddlepaddle/)。

官方模型入口：[Embedding](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、[Reranker](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)。模型名称确定后，下载阶段还应记录具体修订标识，不能仅依靠始终变化的主分支名称复现环境。

## 3. 推荐目录布局

```text
D:\CodeWork\尽调报告Agent\
  specs\                 已有规格和计划
  src\                   后续应用代码
  templates\             报告模板
  .venv\                 主应用：LangGraph、docxtpl、数据库客户端等
  .venv-docling\         Docling 独立环境
  .venv-ocr\             PaddleOCR 和 PaddlePaddle 独立环境
  .venv-retrieval\       Embedding 和 Reranker 的推理依赖

D:\Tools\Python\         由 uv 管理的基础解释器
D:\Apps\PostgreSQL\17\   数据库程序和 pgvector 服务端扩展

D:\AgentData\dd-agent\
  postgres\              数据库数据，由数据库服务管理
  models\                模型权重
  cache\                 软件包和模型下载缓存
  documents\             原始材料及解析结果
  reports\               生成报告
  backups\               开发期备份；正式备份另有独立介质或位置
```

每个环境内的库实际由安装工具放在其 `Lib\site-packages` 中，不需要手动选择每个包的子目录。项目应排除虚拟环境、密钥、客户材料和模型权重进入版本控制。

基础解释器是创建环境的来源，不是应用依赖安装位置。四个环境可以使用同一个 Python 版本；后续应用通过独立进程或本机服务调用解析和检索工作进程，不能跨环境直接导入对方的包。安装验证后，如依赖兼容且合并确有价值，再合并环境。

模型和材料也可以放在项目目录中，但必须排除版本控制并单独备份。此处选择外部数据目录，是为了让代码更新和环境重建不影响材料或数据库。

## 4. Python：避免下载错文件

机器现有 3.11.2 可以暂时用于查看环境；当前默认的 3.12.0a6 是预览版，两者均不作为新工程长期固定基线。建议为本项目另备 3.12 的维护补丁版，不卸载现有解释器。

Python 3.12.15 官方发布页没有 Windows 安装器；不要把源码压缩包当成安装程序。使用 uv 时，它从 python-build-standalone 项目提供的构建获取解释器，并非从 Python 官方获取一个不存在的安装器。[Python 发布说明](https://www.python.org/downloads/release/python-31215/)、[uv 的 Python 管理说明](https://docs.astral.sh/uv/concepts/python-versions/)

在安装好 uv 后，可使用如下方式指定解释器根目录并建立环境。以下只是供后续执行的说明，本次没有运行这些安装命令：

```powershell
$env:UV_PYTHON_INSTALL_DIR = 'D:\Tools\Python'
$env:UV_CACHE_DIR = 'D:\AgentData\dd-agent\cache\uv'
uv python install 3.12.15
Set-Location -LiteralPath 'D:\CodeWork\尽调报告Agent'
uv venv --python 3.12.15 .venv
uv venv --python 3.12.15 .venv-docling
uv venv --python 3.12.15 .venv-ocr
uv venv --python 3.12.15 .venv-retrieval
```

这些环境变量仅在当前终端生效；后续安装脚本须显式设置路径。若工具未列出目标构建，先更新 uv 并核对可下载版本，不悄悄使用现有预览解释器。uv 的安装方式见[官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)。

## 5. PostgreSQL 和 pgvector：需要独立准备

1. 使用 [PostgreSQL 官方 Windows 下载入口](https://www.postgresql.org/download/windows/) 获取 17 系列 x86-64 安装器，按表中路径设置程序与数据目录。
2. 本机开发使用本机连接、独立的项目数据库及项目账户；管理员密码只保存在本地，不写入仓库。端口默认可用 5432，已有实例占用时改为其他端口。
3. pgvector 源码构建使用官方要求的 Visual Studio C++ 构建工具及 x64 开发者命令环境，设置 PostgreSQL 的实际安装路径。构建工具是否齐全尚未检查；已有 Visual Studio 不等于一定有 C++ 工具链。
4. 按官方说明构建并安装 pgvector 0.8.7 后，在目标数据库启用 `vector` 扩展。
5. 验证数据库中报告的扩展版本，并进行写入和相似查询测试。仅安装 Python `pgvector` 包不能代替服务端扩展。

pgvector 的构建目录建议用短英文路径，例如 `D:\Build\pgvector`；不需要将其源码放进尽调 Agent 的业务代码目录。除非以后主动选择容器部署，否则这条路线不需要 Docker 或 WSL。[pgvector Windows 安装说明](https://github.com/pgvector/pgvector#windows)

## 6. 安装顺序和验证边界

1. 准备稳定 Python 和独立环境。
2. 配置 PostgreSQL 与 pgvector；先证明能保存并搜索一小组测试向量。
3. 安装主应用依赖，验证 LangGraph 状态保存与 docxtpl 输出。
4. 准备本地检索模型，先在 CPU 上测试中文向量化和重排序。
5. 分别准备 Docling、PaddleOCR 与所需权重，验证文字 PDF、扫描件和表格；本地模型路径要显式配置，避免默认缓存继续写 C 盘。
6. 通过依赖解析检查、导入测试和真实小样本推理后，再记录完整锁文件；GPU 加速是后续独立验证项。

本机 GTX 1070 8 GB 的驱动已被识别。GPU 轮子、RTX 40 系的另一套索引，以及十页计时，见 [GPU 安装](./gpu-setup.md)。不要同时把 OCR、解析和检索模型都装进全局 Python，也不要因为某一步安装失败就升级整套依赖。
