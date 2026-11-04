# 小型文档视觉模型安装指南（Windows CPU）

核对日期：2026-10-20。候选：PaddleOCR-VL-1.6，官方称 0.9B。

状态更新（2026-10-20）：用户已按步骤安装并实际试读，反馈识别准确但较慢，确定仅在困难场景使用。此前已核对本机存在独立环境、权重和试读脚本。下列步骤保留为安装参考；用户试用反馈不替代 B-Q 固定样本质量／性能验收，模型尚未正式接入应用。

## 选择及用途

- 代码仓库：https://github.com/PaddlePaddle/PaddleOCR
- 官方模型与 Transformers 示例：https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6
- 硬件、CPU 与 Windows 说明：https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md

选择理由：专用于文档识别，模型小，支持文本、表格等元素识别，且官方有 x64 CPU 推理路线。适合本项目对重要扫描区域进行二次识别。GLM-OCR 同为可评估候选，但本轮先验证一种，减少部署和对比成本；此选择不表示已证明它在本项目上效果最好。

本方案采用 Transformers 直接识别裁剪图片，不安装完整 PaddleOCR-VL 版面流水线。官方明确该示例用于元素识别／文本定位；完整页面布局和阅读顺序仍由项目的解析、基础 OCR 与区域选择流程处理。

本机 i7-8700K、32GB 内存，GTX 1070：先用 CPU。官方列出的 NVIDIA 推理要求为计算能力至少 7.0，1070 不满足该条件。无需 Docker、WSL、CUDA、flash-attn，也无需自行训练。

## 目录与版本

| 内容 | 位置／版本 |
| --- | --- |
| Python | 现有 3.12.15，无需重新安装 |
| 新环境 | `D:\CodeWork\尽调报告Agent\.venv-vision` |
| 权重 | `D:\CodeWork\尽调报告Agent\models\vision\PaddleOCR-VL-1.6` |
| 下载缓存 | 项目 `.local\hf-vision` |
| CPU PyTorch / torchvision | 2.14.1 / 0.29.1 |
| Transformers | 5.20.1（官方模型示例要求 5.x） |
| Pillow / safetensors / huggingface-hub | 12.3.0 / 0.8.0 / 1.33.0 |

上列依赖版本取自本机已有 Docling 环境，且该 Transformers 已包含 PaddleOCRVL 模型映射；这是选定版本的依据，不代表已经验证新环境中的模型推理。新建独立环境，不修改 `.venv-ocr`、`.venv-retrieval` 或 `.venv-docling`。

## 1. 新建独立环境

在 PowerShell 逐段执行；某段报错先停止，保留完整错误，不继续后续步骤。

```powershell
Set-Location -LiteralPath 'D:\CodeWork\尽调报告Agent'
& 'D:\Tools\Python\cpython-3.12.15-windows-x86_64-none\python.exe' -m venv .venv-vision
```

不需要激活环境，后续始终使用该环境的 Python，避免 PowerShell 激活策略和环境混用。

## 2. 安装依赖

```powershell
.\.venv-vision\Scripts\python.exe -m pip install --upgrade pip
.\.venv-vision\Scripts\python.exe -m pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu
.\.venv-vision\Scripts\python.exe -m pip install transformers==5.20.1 pillow==12.3.0 safetensors==0.8.0 huggingface-hub==1.33.0
.\.venv-vision\Scripts\python.exe -m pip check
```

检查：

```powershell
.\.venv-vision\Scripts\python.exe -c "import torch, transformers; from transformers import PaddleOCRVLForConditionalGeneration, AutoProcessor; print('torch:', torch.__version__); print('transformers:', transformers.__version__); print('CUDA:', torch.cuda.is_available()); print('依赖导入成功')"
```

本方案 `CUDA: False` 是预期结果。导入成功只验证软件依赖，不证明模型能运行。

## 3. 下载官方预训练权重

GitHub 提供代码；权重来自官方 Hugging Face 模型仓库，无需克隆整个 GitHub 仓库。安装和下载需要联网；下载动作不上传尽调材料。不配置第三方镜像。

```powershell
$env:HF_HOME = Join-Path $PWD '.local\hf-vision'
.\.venv-vision\Scripts\python.exe -c "from huggingface_hub import snapshot_download; print(snapshot_download(repo_id='PaddlePaddle/PaddleOCR-VL-1.6', local_dir=r'models\vision\PaddleOCR-VL-1.6'))"
```

预留至少 10GB 空间供环境、权重及缓存使用；这不是模型实际下载大小。网络中断后可重跑下载命令。下载失败不应改用上传客户材料的在线演示。

首次试用下载官方仓库默认版本；正式集成时记录下载的仓库提交版本及权重摘要，配合依赖锁定，保证可复现。

## 4. 离线图片试读

先准备一张清晰的小块文字截图，保存为项目 `testing\records\vision-test.png`。第一张用可辨认的印刷文字验证运行，第二张再用原件中的手写疑难区域。不要直接把 649 页 PDF 输入模型。

将下列示例保存为 `testing\scripts\legacy\vision-smoke.py`。这是安装诊断探针；应用目前通过独立 vision_worker 接入自动区域增强，日常处理不需要运行该脚本。

```python
import torch
from PIL import Image
from transformers import AutoProcessor, AutoModelForImageTextToText

torch.set_num_threads(4)
path = r"models\vision\PaddleOCR-VL-1.6"
processor = AutoProcessor.from_pretrained(path, local_files_only=True)
model = AutoModelForImageTextToText.from_pretrained(
    path, dtype=torch.float32, attn_implementation="eager",
    local_files_only=True,
).to("cpu").eval()
with Image.open(r"testing\records\vision-test.png") as source:
    picture = source.convert("RGB")
messages = [{"role": "user", "content": [
    {"type": "image", "image": picture},
    {"type": "text", "text": "OCR:"},
]}]
inputs = processor.apply_chat_template(
    messages, add_generation_prompt=True, tokenize=True,
    return_dict=True, return_tensors="pt",
)
with torch.inference_mode():
    generated = model.generate(**inputs, max_new_tokens=256, do_sample=False)
new_tokens = generated[0, inputs["input_ids"].shape[-1]:]
print(processor.decode(new_tokens, skip_special_tokens=True))
```

执行：

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
.\.venv-vision\Scripts\python.exe testing\scripts\legacy\vision-smoke.py
```

生成长度 256 仅用于小块文字冒烟检查；触顶导致截断时须记录，不能当作完整页面结果。CPU 首次加载与生成可能较慢，尚无本机速度测量。此探针不包含任何云端识别调用。

## 5. 接入前验收

安装成功、模型能输出文字、识别准确率达标是三个不同检查点。先完成小图片离线试读，再按 `specs/01-evidence-to-report/tasks.md` 的 B-Q01／B-Q04 做同一原图对比，记录姓名、日期、金额等字段错误、遗漏、耗时和内存。结果作为候选保留，冲突不自动覆盖基础 OCR；不能用参考报告答案修补识别输出。

VLM 初始只跑一个实例／一个任务；基础 OCR 的进程并发另外评测。不要一开始并发加载多个视觉模型。

验收后再保存依赖清单，并把模型提交版本、权重摘要、推理参数纳入应用处理版本：

```powershell
.\.venv-vision\Scripts\python.exe -m pip freeze | Set-Content -Encoding utf8 testing\records\requirements-vision.observed.txt
```
