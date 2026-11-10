"""模型文件下载。用可替换的 opener 做断点续传，不在这里打印密钥。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.request import Request, urlopen

from .plan import MIN_WEIGHT_BYTES, SetupError, file_matches


USER_AGENT = "dd-agent-setup/1"


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body)
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fetch_bytes(url: str, opener=None, timeout: int = 60, headers: dict | None = None) -> tuple[int, bytes]:
    request = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    open_url = opener or urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            return int(status), response.read()
    except SetupError:
        raise
    except Exception as exc:
        raise SetupError(f"下载失败：{url}", "网络中断后可以重新运行，已下完的文件会跳过，未下完的文件会续传。") from exc


def fetch_json(url: str, opener=None):
    import json
    status, body = fetch_bytes(url, opener=opener)
    if status != 200:
        raise SetupError(f"文件清单请求失败（HTTP {status}）。", "可以换 --model-source huggingface、hf-mirror 或 modelscope 后重新运行。")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError("文件清单不是有效的 JSON。", "可以更换模型来源后重新运行。") from exc


def download(url: str, dest: Path, size: int | None, sha256: str | None, opener=None) -> str:
    """把文件下到 dest。已通过大小和校验的文件直接跳过。返回 skipped 或 downloaded。"""
    if file_matches(dest, size, sha256):
        return "skipped"
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(dest) + ".partial")
    if partial.exists() and _payload_ok(partial, size, sha256, dest.name):
        partial.replace(dest)
        return "downloaded"
    if partial.exists() and size is not None and partial.stat().st_size > size:
        partial.unlink()
    have = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have and size is not None and have < size else {}
    request = Request(url, headers={"User-Agent": USER_AGENT, **headers})
    open_url = opener or urlopen
    try:
        with open_url(request, timeout=120) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            if status not in {200, 206}:
                raise SetupError(f"下载 {dest.name} 失败（HTTP {status}）。", "可以重新运行，已完成的文件会跳过。")
            mode = "ab" if status == 206 and have else "wb"
            with partial.open(mode) as handle:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    handle.write(block)
    except SetupError:
        raise
    except Exception as exc:
        raise SetupError(f"下载中断：{dest.name}", "可以重新运行。未完成的文件会从已下载的位置继续。") from exc
    if not _payload_ok(partial, size, sha256, dest.name):
        partial.unlink(missing_ok=True)
        raise SetupError(
            f"{dest.name} 下载后校验失败。",
            "不保留这次的文件。请重新运行；若连续失败，换一个模型来源。",
        )
    partial.replace(dest)
    return "downloaded"


def _payload_ok(path: Path, size: int | None, sha256: str | None, final_name: str) -> bool:
    if not path.is_file():
        return False
    actual = path.stat().st_size
    if size is not None and actual != size:
        return False
    if final_name.endswith((".safetensors", ".pdiparams")) and actual < MIN_WEIGHT_BYTES:
        return False
    if sha256 and _sha256(path) != sha256.lower():
        return False
    return actual > 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
