"""Local CPU region OCR. stdout is JSONL; diagnostics remain in a local log."""
# 困难区域的视觉 OCR worker：读取本地页面图 -> 裁剪 -> PaddleOCR-VL 推理 -> 输出候选。
# 不读取报告答案，不决定最终采用值，不写业务数据库；选择与版本校验由主进程负责。
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dd_agent.performance import span, timed, count

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'


def crop_region(image, bounds):
    # 左上向下取整、右下向上取整，尽量不因坐标换算丢掉边缘字符。
    # 返回像素框供追溯，输入仍是与渲染尺寸无关的 0–1 坐标。
    if len(bounds) != 4 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError('Invalid normalized bounds')
    width, height = image.size
    box = [math.floor(bounds[0]*width), math.floor(bounds[1]*height), math.ceil(bounds[2]*width), math.ceil(bounds[3]*height)]
    return image.crop(box), box


def peak_memory_mb():
    # 记录本工作进程的峰值内存，不是整台机器或整个应用的内存占用。
    if os.name != 'nt':
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ['PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
                                              'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage',
                                              'PagefileUsage','PeakPagefileUsage']]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    value = Counters(); value.cb = ctypes.sizeof(value)
    return value.PeakWorkingSetSize / 1024**2 if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb) else None


@timed('model.vision.initialize')
def load_engine(payload):
    from PIL import Image
    import torch
    import transformers
    from transformers import AutoConfig, AutoProcessor, AutoModelForImageTextToText
    torch.set_num_threads(payload.get('threads',4))
    path = Path(payload['models'])
    from dd_agent.devices import load_with_attention, resolve_torch, torch_dtype
    runtime = resolve_torch('vision', payload.get('device', 'auto'), payload.get('dtype', 'auto'))
    digest = hashlib.sha256()
    # 前缀里的 fp32-eager 是历史候选指纹，不是当前设备或注意力实现。
    # 实测换 GPU／sdpa／KV cache 的文本一致，不能因为这些开关改变 model_version 而让旧候选失效。
    # 生成长度会改变截断，因此仍然写入。
    digest.update(f'vl-region-ocr-fp32-eager-v1|{torch.__version__}|{transformers.__version__}|{payload["max_new_tokens"]}'.encode())
    weights = sorted(path.glob('*.safetensors'))
    if not weights:
        raise ValueError('Local vision model weights missing')
    with span('fingerprint.vision.worker'):
        for file in sorted(list(path.glob('*.json')) + weights):
            digest.update(file.name.encode())
            with file.open('rb') as stream:
                for block in iter(lambda: stream.read(4*1024*1024), b''):
                    digest.update(block)
                    count('fingerprint.vision.worker.bytes', len(block))
    with span('model.vision.load'):
        processor = AutoProcessor.from_pretrained(path, local_files_only=True)
        config = AutoConfig.from_pretrained(path, local_files_only=True)
        # Respect the checkpoint's explicit untied embeddings through v5 config conversion.
        if hasattr(config, 'text_config'):
            # 尊重该模型不共享输入／输出嵌入的配置，处理 Transformers 配置转换兼容性。
            config.text_config.tie_word_embeddings = False
        config.tie_word_embeddings = False
        def factory(attention):
            # 直接放到目标 GPU，避免先在显存里放一份再 .to() 复制第二份。
            kwargs=dict(config=config, dtype=torch_dtype(runtime['dtype']), attn_implementation=attention,
                        local_files_only=True, low_cpu_mem_usage=True)
            if runtime['device']!='cpu':
                try:
                    return AutoModelForImageTextToText.from_pretrained(path, device_map={"": 0}, **kwargs)
                except Exception:
                    model=AutoModelForImageTextToText.from_pretrained(path, **kwargs)
                    return model.to(runtime['device'])
            return AutoModelForImageTextToText.from_pretrained(path, **kwargs).to('cpu')
        model, attention = load_with_attention(factory, runtime['attention'], 'vision')
        model.eval()
        if runtime['device']!='cpu' and torch.cuda.is_available():
            torch.cuda.empty_cache()
        runtime = dict(runtime, attention=attention)
    count('model.vision.loads')
    return processor, model, digest.hexdigest(), runtime


@timed('vision.worker')
def recognize(payload, engine=None):
    from PIL import Image
    import torch
    start = time.monotonic()
    loaded = engine if engine is not None else load_engine(payload)
    processor, model, model_version = loaded[:3]
    with span('vision.image'), Image.open(payload['image']) as source:
        # 当前对输入整张图片设像素上限，再裁剪；小裁剪不代表任意大原图都可接收。
        if source.width * source.height > 12000000:
            raise ValueError('Image exceeds local pixel budget')
        picture, box = crop_region(source.convert('RGB'), payload['bounds'])
        image_size = list(source.size)
        # 大图会让视觉编码器的激活把 8GB 卡顶满。区域文字在长边 1280 以内已经可读。
        long_side=max(picture.size)
        if long_side>1280:
            scale=1280/long_side
            picture=picture.resize((max(1, round(picture.width*scale)), max(1, round(picture.height*scale))))
    data = io.BytesIO(); picture.save(data, format='PNG')
    messages = [{'role':'user', 'content':[{'type':'image','image':picture}, {'type':'text','text':'OCR:'}]}]
    # 提示仅要求 OCR，不附带基础 OCR 猜测或旧报告答案，减少答案反向污染识别。
    inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True,
                                          return_dict=True, return_tensors='pt').to(model.device)
    page = payload.get('page_number') if type(payload.get('page_number')) is int else None
    region = payload.get('region') if type(payload.get('region')) is int else None
    # 检查点默认 use_cache=False。打开后文本与关闭时逐字节一致，CPU 约 55 秒降到约 4 秒，GPU 约 6 秒降到约 1 秒。
    with torch.inference_mode(), span('vision.inference', page=page, region=region):
        output = model.generate(**inputs, max_new_tokens=payload['max_new_tokens'], do_sample=False, use_cache=True)
    tokens = output[0, inputs['input_ids'].shape[-1]:].detach()
    del output, inputs, picture
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    # 只解码生成部分；达到长度上限且未遇结束 token 时，明确报告 truncated。
    # 主进程会把截断候选当作不完整结果，而不是把缺尾的识别文字直接采用。
    eos = model.generation_config.eos_token_id
    eos = eos if isinstance(eos, list) else [eos]
    return dict(text=processor.decode(tokens, skip_special_tokens=True),
                model_version=model_version, prompt='OCR:', max_new_tokens=payload['max_new_tokens'],
                image_sha256=hashlib.sha256(data.getvalue()).hexdigest(), crop_box=box, image_size=image_size,
                truncated=bool(len(tokens) >= payload['max_new_tokens'] and int(tokens[-1]) not in eos),
                elapsed_seconds=round(time.monotonic()-start, 3), peak_memory_mb=peak_memory_mb())


if __name__ == '__main__':
    from dd_agent.devices import release_accelerators
    config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    try:
        print(json.dumps(recognize(config), ensure_ascii=False), flush=True)
    finally:
        # 一次性进程必须先交还显存再退出，否则下一个常驻进程会和它叠在一起。
        release_accelerators()
