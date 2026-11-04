# 扫描件入库：GPU 安装与测量

核对日期：2026-11-04。应用代码不写死 CUDA 版本。下面的索引只在安装对应虚拟环境时使用；没有 GPU 时保持各环境现有的 CPU 包，程序会自动用 CPU，KV cache 仍然打开。

本机两套目标：

| 机器 | GPU | 计算能力 | 视觉／向量 | OCR |
| --- | --- | --- | --- | --- |
| 当前工作站 | GTX 1070 8GB | Pascal，sm_61 | PyTorch 2.14.x，**cu126**，且 **torch&lt;2.15** | paddlepaddle-gpu 3.3.x，**cu118** |
| 另一台 | RTX 4070 12GB | Ada，sm_89 | PyTorch **cu128** 或更新的官方轮子 | paddlepaddle-gpu 3.3.x，**cu126** 或 **cu129** |

2.15 起的官方 Windows 轮子不再提供 cu126，GTX 1070 不能跟到 2.15。RTX 40 系不要装 cu126 的 2.14 限制。两台机器用同一套 `DDA_*` 配置：`auto` 在 Pascal 上保持 float32，在计算能力 8.0 及以上优先 bfloat16。

8GB 显存不能同时留下视觉和向量。视觉约 3.6GB，向量约 2.5GB，桌面约 1.6GB，OCR 峰值约 341MB。程序仍然只常驻一个模型，换模型时先让前一个进程退出并清空显存，再加载下一个。重排序只计算最后一个 token 的 yes／no，GPU 上一次只排一条，避免整张词表 logits 把显存顶到 8GB。

## 1. 推荐配置

`.env` 不入库。GTX 1070 与 RTX 4070 可以用同一组；差别只在虚拟环境里的轮子。

```dotenv
DDA_OCR_DEVICE=auto
DDA_VISION_DEVICE=auto
DDA_EMBEDDING_DEVICE=auto
DDA_RERANKER_DEVICE=auto
DDA_VISION_DTYPE=auto
DDA_EMBEDDING_DTYPE=auto
DDA_RERANKER_DTYPE=auto
DDA_VISION_REGIONS_PER_PAGE=1
DDA_VISION_MAX_NEW_TOKENS=128
DDA_VISION_LOW_SCORE=0.85
DDA_VISION_TIMEOUT_BUDGET=3
DDA_VISION_TIMEOUT_SECONDS=300
DDA_OCR_ENABLE_MKLDNN=0
DDA_OCR_CPU_THREADS=6
DDA_MODEL_CPU_THREADS=4
DDA_STAGE_WINDOW_PAGES=10
DDA_STAGE_BACKLOG_PAGES=40
```

CPU 回退时 OCR 线程可保持 6（i7-8700K 的物理核数）。oneDNN 默认关闭，原因见下一节。没有 GPU 时不要改这些开关去关掉 KV cache。

## 2. 为什么 oneDNN 默认关闭

`workers/ocr_worker.py` 原先写死 `enable_mkldnn=False`。仓库里的原因是性能基线：Windows CPU、FP32、4 线程，不在没有对照的情况下打开加速开关（见 `specs/01-evidence-to-report/performance-plan.md`）。记录里没有单独的崩溃工单。Paddle 3 在 Windows CPU 上曾出现 oneDNN 原语无法执行，所以默认仍然关闭，只提供 `DDA_OCR_ENABLE_MKLDNN=1`。该开关仅在实际设备是 CPU 时传给 Paddle；GPU 路径不传 `enable_mkldnn` 和 `cpu_threads`。若打开后 OCR 进程失败，改回 0。

## 3. GTX 1070：安装 GPU 包

在各自已有的虚拟环境里执行，不要装进主应用的 `.venv-dev`。下列命令只安装推理包，不下载模型权重。

视觉与检索（两个环境各做一次）。torch 必须低于 2.15：

```powershell
.\.venv-vision\Scripts\python.exe -m pip install --upgrade "torch>=2.14,<2.15" --index-url https://download.pytorch.org/whl/cu126
.\.venv-retrieval\Scripts\python.exe -m pip install --upgrade "torch>=2.14,<2.15" --index-url https://download.pytorch.org/whl/cu126
```

对应约束文件是 `requirements-torch-cu126.txt`。

OCR。`nvidia-cudnn-cu11==8.9.4.19` 在 PyPI 上已被标记为 yanked，依赖解析不会自动选它，但 paddlepaddle-gpu 3.3.1 需要它，精确版本仍可安装：

```powershell
.\.venv-ocr\Scripts\python.exe -m pip install "nvidia-cudnn-cu11==8.9.4.19"
.\.venv-ocr\Scripts\python.exe -m pip install "paddlepaddle-gpu>=3.3,<3.4" -i https://www.paddlepaddle.org.cn/packages/stable/cu118/
```

对应文件是 `requirements-paddle-cu118.txt`。若解析器因为 yanked 拒绝 cuDNN，先单独安装上面的精确版本，再装 Paddle。

核对：

```powershell
.\.venv-vision\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_capability())"
.\.venv-retrieval\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_capability())"
.\.venv-ocr\Scripts\python.exe -c "import paddle; paddle.utils.run_check()"
```

1070 上应看到 `+cu126`、`True` 和 `(6, 1)`。`run_check` 应报告 CUDA 可用。测试和 CI 不要求这些包。

## 4. RTX 40 系及更新的 GPU

不要使用上一节的 cu126／torch&lt;2.15 限制，也不要使用 cu118 的 Paddle。

```powershell
.\.venv-vision\Scripts\python.exe -m pip install --upgrade torch --index-url https://download.pytorch.org/whl/cu128
.\.venv-retrieval\Scripts\python.exe -m pip install --upgrade torch --index-url https://download.pytorch.org/whl/cu128
.\.venv-ocr\Scripts\python.exe -m pip install "paddlepaddle-gpu>=3.3,<3.4" -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

Paddle 也可以改用 cu129 索引：`https://www.paddlepaddle.org.cn/packages/stable/cu129/`。约束文件是 `requirements-torch-cu128.txt`、`requirements-paddle-cu126.txt`、`requirements-paddle-cu129.txt`。装完后用同一组核对命令；4070 的计算能力应为 `(8, 9)`。应用配置保持 `auto`，不要为这台机器改代码。

## 5. 版本戳：什么会重跑，什么不会

实测换 GPU、sdpa 和 KV cache 后，OCR 与视觉文本一致。因此这些因素不写入会让已成功页失效的版本：

| 戳 | 位置 | 决定 |
| --- | --- | --- |
| `pdfium-text-v2-ppocr5-cpu-v1` | `reading.py` 的 `PIPELINE_VERSION` | 字符串里的 cpu 是历史编号。设备、线程、oneDNN 都不追加，已完成的 OCR 页继续复用 |
| `vision-region-v1-cpu-fp32` | `review.py` 的权重指纹 | 不追加设备、精度、注意力或 cache。生成长度改的是截断，由自动增强版本单独绑定 |
| `vl-region-ocr-fp32-eager-v1` | 视觉候选里的 `model_version` | 同样保持历史前缀，只随权重、库版本和 `max_new_tokens` 变化 |
| `qwen-last-token-normalized-fp32-v1` | 向量指纹 | float32 不区分 CPU／GPU，已有索引继续有效。精度离开 float32（例如 Ada 上 auto 得到 bfloat16）才换新前缀，避免把不同数值的向量当成同一索引 |

会让视觉重新跑一遍的，是选择规则本身：低置信或数字行、每页区域数、生成长度、超时预算。这是 `automatic-regions-selective-v3`，不是设备开关。

## 6. 十页计时

登记材料后，在工程根目录：

```powershell
.\.venv-dev\Scripts\python.exe -m dd_agent process-pages --project-id <项目ID> --document-id <材料ID> --pages 1-10
```

性能文件写在数据目录的 `performance/`（默认 `.local/data/performance/*.json`）。看 `events` 里这些阶段：

| `stage` | 含义 |
| --- | --- |
| `page.ocr` | 一页 OCR，含排队 |
| `page.embedding` | 一页向量 |
| `page.vision` | 一页视觉增强 |
| `page.vision.region` | 一个区域；`region` 从 0 起 |
| `ocr.inference`／`vision.inference` | worker 内的模型时间，同样带 `page` |

顺序应是阅读和索引在前，视觉在后，视觉结束后再有一轮索引。`counters` 里的 `device.ocr.gpu`、`device.vision.cuda`、`dtype.vision.float32` 表示这次实际用的设备和精度。1070 上视觉精度应为 float32。文件不包含页内正文。

## 7. 这次实际改变了什么

- 有 GPU 就用 GPU；探测失败或没有 CUDA 时用 CPU。CPU 上 KV cache 仍然打开。
- PaddleOCR-VL 的 `generate` 固定 `use_cache=True`。注意力优先 sdpa；当前 torch 拒绝 sdpa 时才改用 eager，没有单独的慢速开关。
- GPU 上的 PaddleOCR 不接收 `cpu_threads` 和 `enable_mkldnn`。
- 视觉只打低置信行，以及带数字、日期或金额的关键行。文字少到无法定位时仍可整页兜底。
- 后台先把已读页嵌入，整卷 OCR 结束后再集中视觉，然后按新文本再嵌入。`process-pages` 对当前这批页使用同一顺序。
