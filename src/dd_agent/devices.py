"""按模型族选择设备与精度。探测只发生在对应虚拟环境里，主进程不导入 Paddle／PyTorch。

auto：能用 CUDA 就用 GPU，否则回退 CPU。计算能力低于 8.0（如 Pascal GTX 1070）
保持 float32；8.0 及以上优先 bfloat16，不支持时用 float16。代码不写死 CUDA 版本，
实际轮子由各虚拟环境自己的安装决定。
"""
import json
import subprocess
import sys
from pathlib import Path

from .performance import count


PADDLE_DEVICES = ('auto', 'cpu', 'gpu:0')
TORCH_DEVICES = ('auto', 'cpu', 'cuda')
DTYPES = ('auto', 'float32', 'float16', 'bfloat16')
_PROBES = {}


def describe_paddle(requested, *, compiled_with_cuda, device_count, enable_mkldnn, threads):
    """Paddle 设备。oneDNN 只在 CPU 上有意义，GPU 路径保持关闭。"""
    requested = (requested or 'auto').strip().lower()
    if requested not in PADDLE_DEVICES:
        requested = 'auto'
    gpu = bool(compiled_with_cuda) and int(device_count or 0) > 0
    if requested == 'cpu' or not gpu:
        device = 'cpu'
    else:
        device = 'gpu:0'
    # MKLDNN／oneDNN 是 CPU 库。本仓库基线与 Windows 上 Paddle 3 的已知不稳定
    # 都要求默认关闭；只有显式打开且实际走 CPU 时才传给 PaddleOCR。
    mkldnn = bool(enable_mkldnn) and device == 'cpu'
    notes = []
    if requested in ('auto', 'gpu:0') and device == 'cpu':
        notes.append('gpu_unavailable')
    if enable_mkldnn and device != 'cpu':
        notes.append('mkldnn_cpu_only')
    return dict(device=device, mkldnn=mkldnn, threads=int(threads), requested=requested, notes=notes)


def paddle_runtime_kwargs(runtime):
    """GPU 上不传 cpu_threads／enable_mkldnn。这两项是 CPU 专用，传给 GPU 构建会干扰初始化。"""
    kwargs = {'device': runtime['device']}
    if runtime['device'] == 'cpu':
        kwargs['cpu_threads'] = int(runtime['threads'])
        kwargs['enable_mkldnn'] = bool(runtime['mkldnn'])
    return kwargs


def describe_torch(requested_device, requested_dtype, *, cuda_available, capability, bf16_supported):
    """Torch 设备与精度。capability 为 (major, minor)，CPU 或未知时为 None。"""
    requested_device = (requested_device or 'auto').strip().lower()
    requested_dtype = (requested_dtype or 'auto').strip().lower()
    if requested_device not in TORCH_DEVICES:
        requested_device = 'auto'
    if requested_dtype not in DTYPES:
        requested_dtype = 'auto'
    if requested_device == 'cpu' or not cuda_available:
        device = 'cpu'
    else:
        device = 'cuda'
    major = capability[0] if capability else 0
    notes = []
    if requested_device in ('auto', 'cuda') and device == 'cpu':
        notes.append('cuda_unavailable')
    if requested_dtype == 'float32':
        dtype = 'float32'
    elif requested_dtype == 'auto':
        if device == 'cpu' or major < 8:
            dtype = 'float32'
        else:
            dtype = 'bfloat16' if bf16_supported else 'float16'
    elif requested_dtype == 'bfloat16':
        if device != 'cpu' and bf16_supported:
            dtype = 'bfloat16'
        else:
            dtype = 'float32'
            notes.append('bf16_unsupported')
    else:
        # float16：Pascal 上偏慢，但显式配置仍尊重；CPU 上没有可靠的 fp16 内核。
        if device == 'cpu':
            dtype = 'float32'
            notes.append('fp16_cpu_fallback')
        else:
            dtype = 'float16'
    return dict(device=device, dtype=dtype, attention='sdpa', requested_device=requested_device,
                requested_dtype=requested_dtype, capability=list(capability) if capability else None, notes=notes)


def log_runtime(kind, choice):
    """写入 worker 日志（stderr）和性能计数。不记录材料正文。"""
    device = choice.get('device', 'cpu')
    dtype = choice.get('dtype', 'float32')
    extra = ''
    if 'mkldnn' in choice:
        extra = f"，oneDNN={'开' if choice['mkldnn'] else '关'}，线程 {choice.get('threads')}"
    note = '，'.join(choice.get('notes') or [])
    suffix = f'（{note}）' if note else ''
    print(f'模型运行时：{kind} 使用 {device}，精度 {dtype}{extra}{suffix}。', file=sys.stderr, flush=True)
    family = 'gpu' if device in ('cuda', 'gpu:0') else 'cpu'
    count(f'device.{kind}.{family}')
    count(f'dtype.{kind}.{dtype}')


def resolve_paddle(config):
    compiled = False
    devices = 0
    try:
        import paddle
        compiled = bool(paddle.device.is_compiled_with_cuda())
        if compiled:
            devices = int(paddle.device.cuda.device_count())
    except Exception:
        compiled = False
        devices = 0
    choice = describe_paddle(config.get('device', 'auto'), compiled_with_cuda=compiled, device_count=devices,
                             enable_mkldnn=config.get('enable_mkldnn', False), threads=config.get('threads', 4))
    log_runtime('ocr', choice)
    return choice


def resolve_torch(kind, requested_device, requested_dtype):
    cuda = False
    capability = None
    bf16 = False
    try:
        import torch
        cuda = bool(torch.cuda.is_available())
        if cuda:
            capability = tuple(torch.cuda.get_device_capability(0))
            bf16 = bool(torch.cuda.is_bf16_supported())
    except Exception:
        cuda = False
    choice = describe_torch(requested_device, requested_dtype, cuda_available=cuda, capability=capability, bf16_supported=bf16)
    log_runtime(kind, choice)
    return choice


def torch_dtype(name):
    import torch
    return {'float32': torch.float32, 'float16': torch.float16, 'bfloat16': torch.bfloat16}[name]


def load_with_attention(factory, attention, kind):
    """factory(attention_name) -> module。sdpa 不可用时回退 eager，不中断加载。"""
    try:
        return factory(attention), attention
    except Exception:
        if attention == 'eager':
            raise
        model = factory('eager')
        print(f'模型运行时：{kind} 注意力 {attention} 不可用，已改用 eager。', file=sys.stderr, flush=True)
        return model, 'eager'


def release_accelerators():
    """进程退出前丢掉加速器缓存。杀进程仍会由驱动回收显存；正常 shutdown 先走这里。"""
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    try:
        import paddle
        if paddle.device.is_compiled_with_cuda():
            paddle.device.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


def probe_torch(python):
    """用检索／视觉自己的解释器探测 CUDA，避免主环境导入 torch。结果在本进程内缓存。"""
    key = str(python)
    if key in _PROBES:
        return _PROBES[key]
    fallback = dict(cuda=False, capability=None, bf16=False)
    path = Path(python)
    if not path.is_file():
        _PROBES[key] = fallback
        return fallback
    code = (
        'import json\n'
        'cuda=False; cap=None; bf16=False\n'
        'try:\n'
        ' import torch\n'
        ' cuda=bool(torch.cuda.is_available())\n'
        ' if cuda:\n'
        '  cap=list(torch.cuda.get_device_capability(0))\n'
        '  bf16=bool(torch.cuda.is_bf16_supported())\n'
        'except Exception:\n'
        ' pass\n'
        'print("DDA_PROBE "+json.dumps(dict(cuda=cuda, capability=cap, bf16=bf16)))\n'
    )
    try:
        completed = subprocess.run([str(path), '-c', code], capture_output=True, text=True, timeout=120, check=False)
        line = next((item for item in reversed((completed.stdout or '').splitlines()) if item.startswith('DDA_PROBE ')), '')
        if completed.returncode == 0 and line:
            payload = json.loads(line[len('DDA_PROBE '):])
            if isinstance(payload, dict) and isinstance(payload.get('cuda'), bool):
                cap = payload.get('capability')
                if cap is not None and not (isinstance(cap, list) and len(cap) == 2 and all(isinstance(v, int) for v in cap)):
                    cap = None
                fallback = dict(cuda=payload['cuda'], capability=cap, bf16=bool(payload.get('bf16')))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, StopIteration):
        fallback = dict(cuda=False, capability=None, bf16=False)
    _PROBES[key] = fallback
    return fallback
