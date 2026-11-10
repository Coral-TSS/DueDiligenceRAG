"""安装计划：参数、显卡轮子、.env 合并、模型校验、数据库选择。

这里只做纯函数，不创建虚拟环境，不下载，不连接数据库。
已有的非空配置不会被覆盖。连接串和密钥只出现在返回值里，调用方负责不把它们打印出来。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit


class SetupError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


SECRET_KEYS = frozenset({"DDA_DATABASE_URL", "DDA_MCP_API_KEY"})
RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})
OCR_NAMES = (
    "PP-OCRv5_mobile_det",
    "PP-OCRv5_server_rec",
    "PP-LCNet_x1_0_doc_ori",
    "PP-LCNet_x1_0_textline_ori",
)
OCR_REQUIRED = ("inference.pdiparams", "inference.json", "inference.yml")
ENV_DIRS = {
    "dev": ".venv-dev",
    "pdf": ".venv-docling",
    "ocr": ".venv-ocr",
    "vision": ".venv-vision",
    "retrieval": ".venv-retrieval",
}
PYTORCH_CPU = "https://download.pytorch.org/whl/cpu"
PYTORCH_CU126 = "https://download.pytorch.org/whl/cu126"
PYTORCH_CU128 = "https://download.pytorch.org/whl/cu128"
PADDLE_CU118 = "https://www.paddlepaddle.org.cn/packages/stable/cu118/"
PADDLE_CU126 = "https://www.paddlepaddle.org.cn/packages/stable/cu126/"
PADDLE_CU129 = "https://www.paddlepaddle.org.cn/packages/stable/cu129/"
VISION_EXTRA = (
    "transformers==5.20.1",
    "pillow==12.3.0",
    "safetensors==0.8.0",
    "huggingface-hub==1.33.0",
)
POSTGRES_IMAGE = "pgvector/pgvector:0.8.7-pg17"
POSTGRES_CONTAINER = "dda-pgvector"
DEFAULT_POSTGRES_PORT = 54329
MIN_WEIGHT_BYTES = 4096
HF_ENDPOINT = "https://huggingface.co"
HF_MIRROR = "https://hf-mirror.com"
MODELSCOPE = "https://www.modelscope.cn"

_SAFE_VALUE = re.compile(r"[A-Za-z0-9_./:\\-]+")
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_CAPABILITY = re.compile(r"(\d+)\.(\d+)")
_SOFFICE_VERSION = re.compile(r"LibreOffice\s+(\d+)\.(\d+)", re.IGNORECASE)
_PASCAL_NAME = re.compile(r"gtx\s*10[3-8]0|gt\s*1030|gtx\s*9[6-8]0", re.IGNORECASE)
_MODERN_NAME = re.compile(
    r"\brtx\b|gtx\s*16|geforce\s*16|\ba100\b|\ba6000\b|\bl40\b|\bh100\b|\bh200\b|quadro\s*rtx",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    optional: bool = False


@dataclass(frozen=True)
class Profile:
    id: str
    reason: str
    paddle_cuda: str
    vision_torch: tuple[str, ...]
    retrieval_torch: tuple[str, ...]
    torch_index: str


@dataclass(frozen=True)
class ModelJob:
    kind: str
    repo: str
    dest: Path
    component: str


@dataclass(frozen=True)
class RemoteFile:
    relative: str
    size: int | None
    sha256: str | None
    url: str = ""


@dataclass(frozen=True)
class Assessment:
    ok: bool
    missing: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class EnvMerge:
    text: str
    written: tuple[str, ...]


@dataclass(frozen=True)
class DatabasePlan:
    action: str
    url: str
    message: str


@dataclass(frozen=True)
class Status:
    name: str
    state: str
    detail: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup",
        description="安装尽调材料工作区的本机环境。已完成的步骤会跳过。",
    )
    parser.add_argument("--device", choices=("auto", "cpu", "gtx10", "rtx40"), default="auto")
    parser.add_argument("--paddle-cuda", choices=("cu126", "cu129"), default="cu126",
                        help="仅 RTX 40 系及更新的 GPU 使用。GTX 10 系固定为 Paddle cu118。")
    parser.add_argument("--model-source", choices=("huggingface", "hf-mirror", "modelscope"))
    parser.add_argument("--database-url", default="")
    parser.add_argument("--postgres", choices=("ask", "docker", "existing", "skip"), default="ask")
    parser.add_argument("--postgres-port", type=int, default=DEFAULT_POSTGRES_PORT)
    parser.add_argument("--skip-vision", action="store_true")
    parser.add_argument("--skip-ocr", action="store_true")
    parser.add_argument("--skip-retrieval", action="store_true")
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    parser.add_argument("--skip-libreoffice", action="store_true")
    parser.add_argument("--skip-database", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--no-mcp-key", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gpu-name", default="")
    parser.add_argument("--compute-capability", default="")
    parser.add_argument("--root", type=Path, default=None)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    validate_port(args.postgres_port)
    if args.database_url.strip():
        args.database_url = validate_database_url(args.database_url.strip())
    if args.compute_capability.strip():
        parsed = parse_capability(args.compute_capability)
        if parsed is None:
            raise SetupError(
                f"无法识别计算能力 {args.compute_capability!r}。",
                "请使用 nvidia-smi 里的形式，例如 6.1 或 8.9，或改用 --device。",
            )
    return args


def require_python(version: tuple[int, ...]) -> None:
    if len(version) < 2 or version[:2] != (3, 12):
        shown = ".".join(str(part) for part in version[:3]) or "未知"
        raise SetupError(
            f"需要 CPython 3.12，当前是 {shown}。",
            "不能使用 3.13，也不要调用 PATH 上的裸 pip。Windows 请运行 scripts/setup.ps1，它会用 uv 或 winget 安装 3.12。",
        )


def validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise SetupError("数据库端口无效。", "请使用 1 到 65535 之间的端口。默认是 54329。")
    if port == 5432:
        raise SetupError(
            "不会占用 5432。",
            "5432 通常是电脑上已经在用的 PostgreSQL。请保留默认 54329，或另选一个端口，以免碰到现有实例。",
        )


def parse_capability(text: str) -> tuple[int, int] | None:
    match = _CAPABILITY.search(text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def profile_from_name(name: str) -> str | None:
    if _PASCAL_NAME.search(name):
        return "gtx10"
    if _MODERN_NAME.search(name):
        return "rtx40"
    return None


def select_profile(device: str, gpu_name: str = "", capability: tuple[int, int] | None = None,
                   paddle_cuda: str = "cu126") -> Profile:
    if device not in {"auto", "cpu", "gtx10", "rtx40"}:
        raise SetupError("设备只能是 auto、cpu、gtx10 或 rtx40。")
    if paddle_cuda not in {"cu126", "cu129"}:
        raise SetupError("Paddle CUDA 只能是 cu126 或 cu129。")
    if device == "auto":
        chosen, reason = _auto_profile(gpu_name, capability)
    else:
        chosen = device
        reason = f"使用参数指定的 {device}，不再根据显卡探测结果改选。"
    return _profile(chosen, reason, paddle_cuda)


def _auto_profile(gpu_name: str, capability: tuple[int, int] | None) -> tuple[str, str]:
    if capability is not None:
        major, minor = capability
        if major < 7:
            return "gtx10", (
                f"计算能力 {major}.{minor} 低于 7.0，按 GTX 10 系安装："
                "PyTorch cu126 且 torch<2.15，Paddle cu118。"
                "更老的 Maxwell 卡可能仍不兼容，失败时改用 --device cpu。"
            )
        return "rtx40", (
            f"计算能力 {major}.{minor}，按 RTX 40 系及更新的官方轮子安装："
            "PyTorch cu128，Paddle cu126（可用 --paddle-cuda cu129）。"
        )
    if gpu_name.strip():
        guessed = profile_from_name(gpu_name)
        if guessed == "gtx10":
            return "gtx10", f"根据显卡名称“{gpu_name.strip()}”按 GTX 10 系安装 PyTorch cu126 与 Paddle cu118。"
        if guessed == "rtx40":
            return "rtx40", f"根据显卡名称“{gpu_name.strip()}”按 RTX 40 系安装 PyTorch cu128 与 Paddle cu126。"
        raise SetupError(
            f"无法从显卡名称“{gpu_name.strip()}”判断该用哪套 CUDA 轮子。",
            "请重新运行并加上 --device cpu、gtx10 或 rtx40。gtx10 是 GTX 10 系（Pascal，torch<2.15，Paddle cu118）；rtx40 是计算能力 7.0 及以上（PyTorch cu128）。",
        )
    return "cpu", "没有检测到 NVIDIA GPU，安装 CPU 轮子。程序仍会在有 CUDA 时自动使用 GPU，但这台机器的环境里没有 CUDA 包。"


def _profile(chosen: str, reason: str, paddle_cuda: str) -> Profile:
    if chosen == "cpu":
        return Profile(
            id="cpu", reason=reason, paddle_cuda="",
            vision_torch=("torch==2.14.1", "torchvision==0.29.1"),
            retrieval_torch=("torch==2.14.1",),
            torch_index=PYTORCH_CPU,
        )
    if chosen == "gtx10":
        return Profile(
            id="gtx10", reason=reason, paddle_cuda="cu118",
            vision_torch=("torch>=2.14,<2.15", "torchvision>=0.29,<0.30"),
            retrieval_torch=("torch>=2.14,<2.15",),
            torch_index=PYTORCH_CU126,
        )
    paddle = "cu129" if paddle_cuda == "cu129" else "cu126"
    return Profile(
        id="rtx40", reason=reason, paddle_cuda=paddle,
        vision_torch=("torch", "torchvision"),
        retrieval_torch=("torch",),
        torch_index=PYTORCH_CU128,
    )


def venv_python(root: Path, component: str, windows: bool) -> Path:
    folder = root / ENV_DIRS[component]
    relative = Path("Scripts/python.exe") if windows else Path("bin/python")
    return folder / relative


def bootstrap_commands(python: Path) -> tuple[Command, ...]:
    executable = str(python)
    return (
        Command((executable, "-m", "ensurepip", "--upgrade")),
        Command((executable, "-m", "pip", "install", "-U", "pip")),
    )


def pip_fallback_command(uv: str, python: Path) -> Command:
    return Command((uv, "pip", "install", "--python", str(python), "pip"))


def package_commands(python: Path, component: str, profile: Profile) -> tuple[Command, ...]:
    executable = str(python)
    if component == "dev":
        return (Command((executable, "-m", "pip", "install", "-e", ".[dev]")),)
    if component == "pdf":
        return (Command((executable, "-m", "pip", "install", "pypdfium2", "Pillow")),)
    if component == "ocr":
        return _ocr_commands(executable, profile)
    if component == "vision":
        return _torch_env_commands(executable, profile.vision_torch, profile.torch_index, VISION_EXTRA)
    if component == "retrieval":
        return _torch_env_commands(executable, profile.retrieval_torch, profile.torch_index, ("transformers==4.57.6",))
    raise SetupError(f"未知环境 {component}。")


def _pip(executable: str, *args: str, optional: bool = False) -> Command:
    return Command((executable, "-m", "pip", *args), optional=optional)


def _ocr_commands(executable: str, profile: Profile) -> tuple[Command, ...]:
    commands = [_pip(executable, "install", "paddleocr==3.7.0")]
    if profile.id == "cpu":
        commands.append(_pip(executable, "uninstall", "-y", "paddlepaddle-gpu", optional=True))
        commands.append(_pip(executable, "install", "paddlepaddle==3.3.1"))
        return tuple(commands)
    commands.append(_pip(executable, "uninstall", "-y", "paddlepaddle", optional=True))
    if profile.id == "gtx10":
        commands.append(_pip(executable, "install", "nvidia-cudnn-cu11==8.9.4.19"))
        index = PADDLE_CU118
    else:
        index = PADDLE_CU129 if profile.paddle_cuda == "cu129" else PADDLE_CU126
    commands.append(_pip(executable, "install", "paddlepaddle-gpu>=3.3,<3.4", "-i", index))
    return tuple(commands)


def _torch_env_commands(executable: str, specs: tuple[str, ...], index: str,
                        extra: tuple[str, ...]) -> tuple[Command, ...]:
    torch = _pip(executable, "install", "--upgrade", *specs, "--index-url", index)
    # 最后再装一次 torch，避免后面的 PyPI 包把 CUDA 轮子换成 CPU 轮子。
    return (torch, _pip(executable, "install", *extra), torch)


def selected_components(skip_pdf: bool, skip_ocr: bool, skip_vision: bool, skip_retrieval: bool) -> tuple[str, ...]:
    components = ["dev"]
    if not skip_pdf:
        components.append("pdf")
    if not skip_ocr:
        components.append("ocr")
    if not skip_vision:
        components.append("vision")
    if not skip_retrieval:
        components.append("retrieval")
    return tuple(components)


def command_fingerprint(profile: Profile, commands: tuple[Command, ...], extra: str = "") -> str:
    parts = [profile.id, profile.paddle_cuda, profile.torch_index, extra]
    for command in commands:
        parts.append(" ".join(command.argv))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def should_reinstall(probe_ok: bool, stamp: dict | None, fingerprint: str) -> bool:
    """探测通过且没有过期戳记时跳过。探测失败则重装，即使戳记还在。"""
    if not probe_ok:
        return True
    if stamp and stamp.get("fingerprint") != fingerprint:
        return True
    return False


def stamp_document(fingerprint: str, profile_id: str) -> dict:
    return {"fingerprint": fingerprint, "profile": profile_id}


def probe_source(component: str) -> str:
    if component == "dev":
        names = ("dd_agent", "fastapi", "pytest", "psycopg")
        return _import_probe(names)
    if component == "pdf":
        return _import_probe(("pypdfium2", "PIL"))
    if component == "ocr":
        return r"""
import json
out = {}
try:
    import paddle
    out["paddle"] = paddle.__version__
    try:
        out["paddle_cuda"] = paddle.version.cuda() or ""
    except Exception:
        out["paddle_cuda"] = ""
except Exception as exc:
    out["error"] = type(exc).__name__
try:
    import importlib.metadata as metadata
    out["paddleocr"] = metadata.version("paddleocr")
except Exception:
    out["paddleocr"] = ""
print("DDA_SETUP_PROBE " + json.dumps(out))
"""
    if component in {"vision", "retrieval"}:
        return r"""
import json
out = {}
try:
    import torch
    out["torch"] = torch.__version__
except Exception as exc:
    out["error"] = type(exc).__name__
try:
    import torchvision
    out["torchvision"] = torchvision.__version__
except Exception:
    out["torchvision"] = ""
try:
    import importlib.metadata as metadata
    out["transformers"] = metadata.version("transformers")
except Exception:
    out["transformers"] = ""
print("DDA_SETUP_PROBE " + json.dumps(out))
"""
    raise SetupError(f"未知环境 {component}。")


def _import_probe(names: tuple[str, ...]) -> str:
    listed = ", ".join(repr(name) for name in names)
    return (
        "import json\n"
        f"names = ({listed})\n"
        "out = {}\n"
        "for name in names:\n"
        "    try:\n"
        "        __import__(name)\n"
        "        out[name] = True\n"
        "    except Exception:\n"
        "        out[name] = False\n"
        "print('DDA_SETUP_PROBE ' + json.dumps(out))\n"
    )


def parse_probe(output: str) -> dict:
    for line in reversed(output.splitlines()):
        if line.startswith("DDA_SETUP_PROBE "):
            try:
                payload = json.loads(line[len("DDA_SETUP_PROBE "):])
            except json.JSONDecodeError as exc:
                raise SetupError("环境检查结果无法解析。", "可以重新运行。若虚拟环境损坏，删掉对应的 .venv-* 目录后再运行。") from exc
            if isinstance(payload, dict):
                return payload
    raise SetupError("环境检查没有返回结果。", "可以重新运行。若虚拟环境损坏，删掉对应的 .venv-* 目录后再运行。")


def observed_satisfies(component: str, profile: Profile, observed: dict) -> bool:
    if not isinstance(observed, dict) or observed.get("error"):
        return False
    if component == "dev":
        return all(observed.get(name) is True for name in ("dd_agent", "fastapi", "pytest", "psycopg"))
    if component == "pdf":
        return observed.get("pypdfium2") is True and observed.get("PIL") is True
    if component == "ocr":
        paddleocr = str(observed.get("paddleocr") or "")
        paddle = str(observed.get("paddle") or "")
        cuda = str(observed.get("paddle_cuda") or "")
        if not paddleocr.startswith("3.7.0") or not paddle.startswith("3.3."):
            return False
        if profile.id == "cpu":
            return cuda.strip().lower() in {"", "false", "none"}
        if profile.id == "gtx10":
            return "11.8" in cuda
        expected = "12.9" if profile.paddle_cuda == "cu129" else "12.6"
        return expected in cuda
    if component == "vision":
        return _torch_matches(profile, observed.get("torch")) and _vision_torchvision(profile, observed.get("torchvision")) and observed.get("transformers") == "5.20.1"
    if component == "retrieval":
        return _torch_matches(profile, observed.get("torch")) and observed.get("transformers") == "4.57.6"
    return False


def _torch_matches(profile: Profile, version: object) -> bool:
    text = str(version or "")
    if profile.id == "cpu":
        return text.startswith("2.14.1") and "+cu" not in text
    if profile.id == "gtx10":
        return text.startswith("2.14.") and "+cu126" in text
    return "+cu128" in text


def _vision_torchvision(profile: Profile, version: object) -> bool:
    text = str(version or "")
    if not text:
        return False
    if profile.id in {"cpu", "gtx10"}:
        return text.startswith("0.29.")
    return True


def parse_env(text: str) -> list[tuple[str, str | None, str]]:
    """返回 (原文, 键或 None, 值或 None)。注释和空行的键为 None。"""
    rows = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            rows.append((raw, None, None))
            continue
        match = _ENV_LINE.match(stripped)
        if match is None:
            rows.append((raw, None, None))
            continue
        rows.append((raw, match.group(1), _decode_value(match.group(2).strip())))
    return rows


def env_map(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for _, key, value in parse_env(text):
        if key is not None and value is not None:
            values[key] = value
    return values


def _decode_value(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace(r"\"", '"').replace(r"\\", "\\")
        return inner
    return raw


def format_assignment(key: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise SetupError(f"{key} 含有换行，不能写入 .env。")
    if value == "":
        return f"{key}="
    if key not in SECRET_KEYS and _SAFE_VALUE.fullmatch(value):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', r"\"")
    return f'{key}="{escaped}"'


def merge_env(existing: str | None, example: str, fills: dict[str, str], *, newline: str | None = None) -> EnvMerge:
    """缺失或为空的键才写入。非空值一律保留，包括和 fills 不同的值。"""
    for key, value in fills.items():
        if not isinstance(value, str):
            raise SetupError(f"{key} 的值无效。")
    source = example if existing is None else existing
    if source.startswith("\ufeff"):
        source = source[1:]
    if newline is None:
        newline = "\r\n" if "\r\n" in source else "\n"
    rows = parse_env(source)
    example_keys = [key for _, key, _ in parse_env(example) if key]
    seen: set[str] = set()
    written: list[str] = []
    last_index: dict[str, int] = {}
    for index, (_, key, _) in enumerate(rows):
        if key is not None:
            seen.add(key)
            last_index[key] = index
    output = [raw for raw, _, _ in rows]
    for key, index in last_index.items():
        current = rows[index][2] or ""
        if key in fills and current == "" and fills[key] != "":
            output[index] = format_assignment(key, fills[key])
            written.append(key)
    missing = []
    for key in example_keys:
        if key in seen or key in missing:
            continue
        missing.append(key)
        value = fills[key] if key in fills and fills[key] != "" else ""
        # 新增键沿用示例里的非空默认；示例为空时才用本次解析出的值。
        if value == "":
            for _, example_key, example_value in parse_env(example):
                if example_key == key and example_value:
                    value = example_value
                    break
        if value == "" and key in fills:
            value = fills[key]
        output.append(format_assignment(key, value))
        if key in fills and fills[key] != "" and value == fills[key]:
            written.append(key)
        seen.add(key)
    text = newline.join(output)
    if text and not text.endswith(("\n", "\r\n")):
        text += newline
    return EnvMerge(text, tuple(written))


def conventional_fills(root: Path, windows: bool) -> dict[str, str]:
    def interpreter(folder: str) -> str:
        name = "Scripts/python.exe" if windows else "bin/python"
        return str(root / folder / name)
    return {
        "DDA_PDF_PYTHON": interpreter(".venv-docling"),
        "DDA_OCR_PYTHON": interpreter(".venv-ocr"),
        "DDA_OCR_MODELS_DIR": str(root / "models" / "ocr" / "official_models"),
        "DDA_RETRIEVAL_PYTHON": interpreter(".venv-retrieval"),
        "DDA_EMBEDDING_DIR": str(root / "models" / "embedding" / "Qwen3-Embedding-0.6B"),
        "DDA_RERANKER_DIR": str(root / "models" / "reranker" / "Qwen3-Reranker-0.6B"),
        "DDA_VISION_PYTHON": interpreter(".venv-vision"),
        "DDA_VISION_MODELS_DIR": str(root / "models" / "vision" / "PaddleOCR-VL-1.6"),
    }


def describe_writes(written: tuple[str, ...]) -> tuple[str, ...]:
    lines = []
    for key in written:
        if key in SECRET_KEYS:
            lines.append(f"{key}：已写入（值不显示）")
        else:
            lines.append(f"{key}：已写入")
    return tuple(lines)


def model_jobs(root: Path, values: dict[str, str], *, skip_models: bool, skip_ocr: bool,
               skip_vision: bool, skip_retrieval: bool) -> tuple[ModelJob, ...]:
    if skip_models:
        return ()
    jobs = []
    if not skip_ocr:
        base = Path(values.get("DDA_OCR_MODELS_DIR") or root / "models" / "ocr" / "official_models")
        for name in OCR_NAMES:
            jobs.append(ModelJob("ocr", f"PaddlePaddle/{name}", base / name, "ocr"))
    if not skip_vision:
        dest = Path(values.get("DDA_VISION_MODELS_DIR") or root / "models" / "vision" / "PaddleOCR-VL-1.6")
        jobs.append(ModelJob("hf", "PaddlePaddle/PaddleOCR-VL-1.6", dest, "vision"))
    if not skip_retrieval:
        embedding = Path(values.get("DDA_EMBEDDING_DIR") or root / "models" / "embedding" / "Qwen3-Embedding-0.6B")
        reranker = Path(values.get("DDA_RERANKER_DIR") or root / "models" / "reranker" / "Qwen3-Reranker-0.6B")
        jobs.append(ModelJob("hf", "Qwen/Qwen3-Embedding-0.6B", embedding, "retrieval"))
        jobs.append(ModelJob("hf", "Qwen/Qwen3-Reranker-0.6B", reranker, "retrieval"))
    return tuple(jobs)


def listing_url(source: str, repo: str) -> str:
    if source == "modelscope":
        return f"{MODELSCOPE}/api/v1/models/{repo}/repo/files?Revision=master&Recursive=true"
    endpoint = HF_MIRROR if source == "hf-mirror" else HF_ENDPOINT
    return f"{endpoint}/api/models/{repo}/tree/main?recursive=1"


def file_url(source: str, repo: str, relative: str) -> str:
    quoted = quote(relative, safe="/")
    if source == "modelscope":
        return f"{MODELSCOPE}/models/{repo}/resolve/master/{quoted}"
    endpoint = HF_MIRROR if source == "hf-mirror" else HF_ENDPOINT
    return f"{endpoint}/{repo}/resolve/main/{quoted}"


def skip_remote_path(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    if not name or name.startswith("."):
        return True
    lower = name.lower()
    if lower.endswith((".md", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        return True
    if relative.startswith(".eval_results/") or "/.eval_results/" in relative:
        return True
    return False


def parse_hf_tree(payload: object) -> tuple[RemoteFile, ...]:
    if not isinstance(payload, list):
        raise SetupError("Hugging Face 文件列表格式无法识别。", "可以改用 --model-source modelscope 后重新运行。")
    files = []
    for item in payload:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        relative = str(item.get("path") or "")
        if not relative or skip_remote_path(relative):
            continue
        lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
        sha = str(lfs.get("oid") or "")
        if sha.startswith("sha256:"):
            sha = sha.split(":", 1)[1]
        size = lfs.get("size", item.get("size"))
        files.append(RemoteFile(relative, _as_size(size), sha or None))
    return tuple(files)


def parse_modelscope_files(payload: object) -> tuple[RemoteFile, ...]:
    data = payload.get("Data") if isinstance(payload, dict) else None
    rows = data.get("Files") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SetupError("ModelScope 文件列表格式无法识别。", "可以改用 --model-source hf-mirror 后重新运行。")
    files = []
    for item in rows:
        if not isinstance(item, dict) or item.get("Type") != "blob":
            continue
        relative = str(item.get("Path") or "")
        if not relative or skip_remote_path(relative):
            continue
        sha = str(item.get("Sha256") or "") or None
        files.append(RemoteFile(relative, _as_size(item.get("Size")), sha))
    return tuple(files)


def _as_size(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def with_urls(files: tuple[RemoteFile, ...], source: str, repo: str) -> tuple[RemoteFile, ...]:
    return tuple(RemoteFile(item.relative, item.size, item.sha256, file_url(source, repo, item.relative)) for item in files)


def assess_model(dest: Path, kind: str) -> Assessment:
    if _has_incomplete(dest):
        return Assessment(False, ("incomplete",), "目录里有未完成的下载，需要继续。")
    if kind == "ocr":
        missing = []
        for name in OCR_REQUIRED:
            path = dest / name
            if name == "inference.pdiparams":
                if not _weight_ok(path):
                    missing.append(name)
            elif not _text_ok(path):
                missing.append(name)
        if missing:
            return Assessment(False, tuple(missing), "OCR 权重不完整，需要 " + "、".join(OCR_REQUIRED) + "。")
        return Assessment(True, (), "OCR 权重已在本地。")
    config = dest / "config.json"
    if not _json_object(config):
        return Assessment(False, ("config.json",), "缺少可用的 config.json。")
    weights = [path for path in _files(dest) if path.suffix == ".safetensors"]
    if not any(_weight_ok(path) for path in weights):
        return Assessment(False, ("*.safetensors",), "缺少可用的 safetensors 权重。")
    return Assessment(True, (), "模型文件已在本地。")


def files_needed(dest: Path, remote: tuple[RemoteFile, ...], remembered: dict | None = None) -> tuple[RemoteFile, ...]:
    store = remembered or {}
    needed = []
    for item in remote:
        path = dest / item.relative
        if remembered_ok(store, item, path) or file_matches(path, item.size, item.sha256):
            continue
        needed.append(item)
    return tuple(needed)


def remembered_ok(store: dict, item: RemoteFile, path: Path) -> bool:
    row = store.get(item.relative) if isinstance(store, dict) else None
    if not isinstance(row, dict) or not path.is_file():
        return False
    try:
        actual = path.stat().st_size
    except OSError:
        return False
    if item.size is not None and actual != item.size:
        return False
    if row.get("size") != actual:
        return False
    if item.sha256 and row.get("sha256") != item.sha256:
        return False
    if not item.sha256 and not row.get("sha256"):
        return False
    return not _looks_like_html(path)


def file_matches(path: Path, size: int | None, sha256: str | None) -> bool:
    if not path.is_file() or _looks_like_html(path):
        return False
    try:
        actual = path.stat().st_size
    except OSError:
        return False
    if size is not None and actual != size:
        return False
    if path.suffix in {".safetensors", ".pdiparams"} and actual < MIN_WEIGHT_BYTES:
        return False
    if sha256:
        return _sha256(path) == sha256.lower()
    return actual > 0


def _files(dest: Path) -> list[Path]:
    if not dest.exists():
        return []
    return [path for path in dest.rglob("*") if path.is_file() and ".cache" not in path.parts and path.name != ".dda-checksums.json"]


def _has_incomplete(dest: Path) -> bool:
    if not dest.exists():
        return False
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".partial") or path.name.endswith(".incomplete"):
            return True
    return False


def _weight_ok(path: Path) -> bool:
    if not path.is_file() or _looks_like_html(path):
        return False
    try:
        return path.stat().st_size >= MIN_WEIGHT_BYTES
    except OSError:
        return False


def _text_ok(path: Path) -> bool:
    if not path.is_file() or _looks_like_html(path):
        return False
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _json_object(path: Path) -> bool:
    if not _text_ok(path):
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload)


def _looks_like_html(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(64).lstrip().lower()
    except OSError:
        return False
    return head.startswith((b"<!doctype", b"<html", b"<?xml"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_database_url(url: str) -> str:
    text = url.strip()
    if not text or any(char.isspace() for char in text):
        raise SetupError("数据库连接串为空或含有空白。", "格式示例：postgresql://用户:密码@127.0.0.1:5432/专用库名")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in {"postgresql", "postgres"}:
        raise SetupError("只接受 PostgreSQL 连接串。", "请使用 postgresql:// 或 postgres:// 开头的专用库地址。")
    if not parts.hostname:
        raise SetupError(
            "连接串没有主机名。",
            "请写明主机，例如 127.0.0.1。脚本不会使用默认的本机套接字，以免连到未指明的实例。",
        )
    database = parts.path[1:] if parts.path.startswith("/") else parts.path
    if not database or "/" in database:
        raise SetupError("连接串里没有明确的数据库名。")
    if database.lower() in RESERVED_DATABASES:
        raise SetupError(
            f"不会使用 {database} 这个库。",
            "postgres、template0、template1 是数据库自带的库。请新建一个只给本项目用的空库，再把那个库的连接串填进来。",
        )
    return text


def plan_database(mode: str, existing_url: str, provided_url: str, skip_database: bool) -> DatabasePlan:
    # skip 不初始化数据库，也不改连接串。keep 才会在已有连接串上运行 init-db。
    if skip_database or mode == "skip":
        if existing_url:
            return DatabasePlan("skip", existing_url, "已跳过数据库步骤。.env 里的连接串保持不变，没有运行 init-db。")
        return DatabasePlan("skip", "", "已跳过数据库。启动前需要在 .env 填写 DDA_DATABASE_URL，再运行 python -m dd_agent init-db。")
    if existing_url:
        if mode == "docker":
            return DatabasePlan("keep", existing_url, ".env 已有 DDA_DATABASE_URL，不会另起 Docker 数据库，也不会改写连接串。")
        if provided_url and provided_url != existing_url:
            return DatabasePlan("keep", existing_url, ".env 已有 DDA_DATABASE_URL，保持原值。要更换请先手动编辑 .env。")
        return DatabasePlan("keep", existing_url, "沿用 .env 中的 DDA_DATABASE_URL。")
    if provided_url:
        return DatabasePlan("write", validate_database_url(provided_url), "将把专用库连接串写入 .env 的空缺项。")
    if mode == "docker":
        return DatabasePlan("docker", "", "将启动只绑定 127.0.0.1 的项目专用 pgvector 容器，数据放在仓库 .local/postgres。")
    if mode == "existing":
        raise SetupError(
            "选择了已有数据库，但没有连接串。",
            "请加上 --database-url，或在提示里输入专用库连接串。脚本不会扫描或修改其他库。",
        )
    if mode == "ask":
        return DatabasePlan("ask", "", "")
    raise SetupError("无法决定数据库安装方式。", "请使用 --postgres docker、existing 或 skip。")


def classify_volume(*, nonempty: bool, has_env_file: bool) -> str:
    """fresh 可以新建；reuse 继续用已有口令文件；refuse 表示目录不是本脚本留下的。"""
    if has_env_file:
        return "reuse"
    if nonempty:
        return "refuse"
    return "fresh"


def render_compose() -> str:
    return f"""services:
  db:
    image: {POSTGRES_IMAGE}
    container_name: {POSTGRES_CONTAINER}
    labels:
      com.dd-agent.setup: "1"
    environment:
      POSTGRES_USER: dda
      POSTGRES_PASSWORD: ${{DDA_SETUP_PG_PASSWORD}}
      POSTGRES_DB: dda
    ports:
      - "127.0.0.1:${{DDA_SETUP_PG_PORT}}:5432"
    volumes:
      - ${{DDA_SETUP_PG_DATA}}:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dda -d dda"]
      interval: 2s
      timeout: 5s
      retries: 30
"""


def render_pg_env(password: str, port: int, data_dir: str) -> str:
    if not password or any(char.isspace() for char in password):
        raise SetupError("生成的数据库口令无效。")
    validate_port(port)
    return (
        f"DDA_SETUP_PG_PASSWORD={password}\n"
        f"DDA_SETUP_PG_PORT={port}\n"
        f"DDA_SETUP_PG_DATA={compose_volume(data_dir)}\n"
    )


def parse_pg_env(text: str) -> dict[str, str]:
    values = env_map(text)
    if not values.get("DDA_SETUP_PG_PASSWORD") or not values.get("DDA_SETUP_PG_PORT"):
        raise SetupError(
            "项目数据库的本地口令文件不完整。",
            "不会重置 .local/postgres。若这是上次安装留下的库，请把原来的 DDA_DATABASE_URL 填回 .env。",
        )
    return values


def build_local_database_url(password: str, port: int) -> str:
    validate_port(port)
    return f"postgresql://dda:{quote(password, safe='')}@127.0.0.1:{port}/dda"


def compose_volume(path: str) -> str:
    return path.replace("\\", "/")


def postgres_help_text() -> str:
    return (
        "数据库需要带 pgvector 的 PostgreSQL。脚本不会连接、也不会修改你已经在用的业务库。\n"
        "可选做法：\n"
        "  1. 你自己准备一个空的专用库（不要用 postgres、template0、template1），把连接串交给脚本。\n"
        "  2. 若已安装 Docker，脚本可以新建只监听 127.0.0.1:54329 的 pgvector 容器，数据在仓库 .local/postgres。\n"
        "Windows 安装器本身不带 pgvector。官方扩展需要用 Visual Studio 的 nmake 按 PostgreSQL 版本编译，"
        "或使用与主版本匹配的社区二进制；这两种都要你确认装到哪一个实例。脚本不会替你挑一个已有实例去安装扩展。\n"
        "init-db 只会在你确认的那个库里创建 dd_agent 结构，并且可以重复执行。"
    )


def parse_soffice_version(text: str) -> tuple[int, int] | None:
    match = _SOFFICE_VERSION.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def soffice_is_new_enough(version: tuple[int, int] | None) -> bool:
    return version is not None and version >= (24, 2)


def winget_libreoffice() -> tuple[str, ...]:
    return (
        "winget", "install", "--id", "TheDocumentFoundation.LibreOffice",
        "-e", "--accept-package-agreements", "--accept-source-agreements",
        "--disable-interactivity",
    )


def windows_soffice_candidates(local_appdata: str = "") -> tuple[Path, ...]:
    roots = [
        Path(r"C:\Program Files\LibreOffice\program"),
        Path(r"C:\Program Files (x86)\LibreOffice\program"),
    ]
    if local_appdata.strip():
        roots.append(Path(local_appdata) / "Programs" / "LibreOffice" / "program")
    return tuple(root / name for root in roots for name in ("soffice.com", "soffice.exe"))


def redact(text: str, secrets: tuple[str, ...] | list[str]) -> str:
    redacted = text
    for secret in sorted((item for item in secrets if item and len(item) >= 4), key=len, reverse=True):
        redacted = redacted.replace(secret, "***")
    return redacted


def format_summary(rows: list[Status], secrets: tuple[str, ...] | list[str] = ()) -> str:
    labels = {"ready": "可用", "skipped": "已跳过", "failed": "失败"}
    lines = ["安装结果"]
    for row in rows:
        state = labels.get(row.state, row.state)
        detail = f"  {row.detail}" if row.detail else ""
        lines.append(f"  {row.name}：{state}{detail}")
    return redact("\n".join(lines) + "\n", secrets)


def format_plan(profile: Profile, commands: dict[str, tuple[Command, ...]], jobs: tuple[ModelJob, ...],
                database: DatabasePlan, writes: tuple[str, ...]) -> str:
    lines = [
        "安装计划（未执行）",
        f"显卡方案：{profile.id}。{profile.reason}",
        f"PyTorch 索引：{profile.torch_index}",
        f"Paddle：{profile.paddle_cuda or 'CPU 版 paddlepaddle==3.3.1'}",
    ]
    for name, group in commands.items():
        lines.append(f"[{name}]")
        for command in group:
            mark = "（可忽略失败）" if command.optional else ""
            lines.append("  " + " ".join(command.argv) + mark)
    if jobs:
        lines.append("[模型]")
        for job in jobs:
            lines.append(f"  {job.repo} -> {job.dest}")
    else:
        lines.append("[模型] 跳过")
    lines.append("[数据库] " + database.message)
    lines.append("[.env] " + ("、".join(writes) if writes else "没有新的空缺项"))
    return "\n".join(lines) + "\n"
