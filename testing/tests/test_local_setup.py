"""本地安装脚本的纯逻辑测试。不下载模型，不连接数据库，也不写仓库里的 .env。"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import local_setup.installer as installer
from local_setup.fetch import download
from local_setup.installer import main
from local_setup.plan import (
    HF_MIRROR,
    MIN_WEIGHT_BYTES,
    MODELSCOPE,
    PADDLE_CU118,
    PADDLE_CU129,
    PYTORCH_CPU,
    PYTORCH_CU126,
    PYTORCH_CU128,
    SetupError,
    assess_model,
    bootstrap_commands,
    build_local_database_url,
    classify_volume,
    command_fingerprint,
    compose_volume,
    conventional_fills,
    describe_writes,
    env_map,
    file_url,
    files_needed,
    format_assignment,
    format_summary,
    listing_url,
    merge_env,
    model_jobs,
    observed_satisfies,
    package_commands,
    parse_args,
    parse_hf_tree,
    parse_modelscope_files,
    parse_soffice_version,
    plan_database,
    postgres_help_text,
    profile_from_name,
    redact,
    render_compose,
    render_pg_env,
    require_python,
    select_profile,
    should_reinstall,
    soffice_is_new_enough,
    stamp_document,
    validate_database_url,
    validate_port,
    with_urls,
)


EXAMPLE = """# 注释保留
DDA_DATABASE_URL=
DDA_PORT=8765
DDA_OCR_PYTHON=
DDA_EMBEDDING_DIR=
DDA_MCP_API_KEY=
"""


def _profile(device="cpu", **kwargs):
    return select_profile(device, **kwargs)


def _python(name="python"):
    return Path(name)


def test_require_python_accepts_only_312():
    require_python((3, 12, 3))
    with pytest.raises(SetupError, match="3.13"):
        require_python((3, 13, 1))
    with pytest.raises(SetupError, match="3.11"):
        require_python((3, 11, 9))


def test_parse_args_defaults_and_overrides():
    args = parse_args([])
    assert args.device == "auto"
    assert args.paddle_cuda == "cu126"
    assert args.postgres == "ask"
    assert args.postgres_port == 54329
    assert args.model_source is None
    assert args.no_mcp_key is False
    chosen = parse_args([
        "--device", "gtx10", "--skip-vision", "--skip-libreoffice", "--model-source", "modelscope",
        "--no-mcp-key", "--paddle-cuda", "cu129", "--non-interactive", "--postgres", "skip",
    ])
    assert chosen.device == "gtx10"
    assert chosen.skip_vision and chosen.skip_libreoffice and chosen.no_mcp_key
    assert chosen.model_source == "modelscope"
    assert chosen.paddle_cuda == "cu129"
    with pytest.raises(SystemExit):
        parse_args(["--device", "gpu"])
    with pytest.raises(SetupError, match="5432"):
        parse_args(["--postgres-port", "5432"])


def test_gpu_profile_selection_and_name_fallback():
    cpu = select_profile("auto")
    assert cpu.id == "cpu" and cpu.torch_index == PYTORCH_CPU and cpu.paddle_cuda == ""
    pascal = select_profile("auto", capability=(6, 1))
    assert pascal.id == "gtx10" and pascal.torch_index == PYTORCH_CU126 and pascal.paddle_cuda == "cu118"
    ada = select_profile("auto", "NVIDIA GeForce RTX 4070", (8, 9), "cu129")
    assert ada.id == "rtx40" and ada.torch_index == PYTORCH_CU128 and ada.paddle_cuda == "cu129"
    # 有计算能力时以计算能力为准，即使名称像另一代卡。
    mixed = select_profile("auto", "NVIDIA GeForce RTX 4070", (6, 1))
    assert mixed.id == "gtx10"
    assert select_profile("auto", "NVIDIA GeForce GTX 1070").id == "gtx10"
    assert select_profile("auto", "NVIDIA GeForce RTX 4070 Laptop GPU").id == "rtx40"
    assert select_profile("auto", "NVIDIA GeForce GTX 1650").id == "rtx40"
    assert profile_from_name("GT 1030") == "gtx10"
    forced = select_profile("cpu", "NVIDIA GeForce RTX 4070", (8, 9))
    assert forced.id == "cpu"
    with pytest.raises(SetupError) as caught:
        select_profile("auto", "NVIDIA Something Unknown")
    assert "--device" in caught.value.hint


def test_package_commands_follow_readme_wheels():
    cpu = _profile("cpu")
    gtx = _profile("gtx10")
    rtx = select_profile("rtx40", paddle_cuda="cu129")
    py = _python(".venv-dev/Scripts/python.exe")
    dev = package_commands(py, "dev", gtx)
    assert dev[0].argv[1:4] == ("-m", "pip", "install")
    assert ".[dev]" in dev[0].argv
    assert not any("torch" in part or "paddle" in part for command in dev for part in command.argv)
    pdf = package_commands(py, "pdf", cpu)
    assert "pypdfium2" in pdf[0].argv and "Pillow" in pdf[0].argv
    ocr_cpu = package_commands(py, "ocr", cpu)
    assert any("paddlepaddle==3.3.1" in command.argv for command in ocr_cpu)
    assert all("paddlepaddle-gpu" not in part or command.optional for command in ocr_cpu for part in command.argv)
    ocr_gtx = package_commands(py, "ocr", gtx)
    joined = [" ".join(command.argv) for command in ocr_gtx]
    assert any("nvidia-cudnn-cu11==8.9.4.19" in line for line in joined)
    assert any(PADDLE_CU118 in line and "paddlepaddle-gpu>=3.3,<3.4" in line for line in joined)
    assert joined.index(next(line for line in joined if "cudnn" in line)) < joined.index(next(line for line in joined if "paddlepaddle-gpu>=" in line))
    ocr_rtx = package_commands(py, "ocr", rtx)
    assert any(PADDLE_CU129 in command.argv for command in ocr_rtx)
    assert all(PADDLE_CU118 not in command.argv for command in ocr_rtx)
    vision = package_commands(py, "vision", gtx)
    text = " ".join(" ".join(command.argv) for command in vision)
    assert "torch>=2.14,<2.15" in text and "torchvision>=0.29,<0.30" in text
    assert PYTORCH_CU126 in text and "transformers==5.20.1" in text
    assert text.index("transformers==5.20.1") < text.rindex(PYTORCH_CU126)
    retrieval = package_commands(py, "retrieval", gtx)
    retrieval_text = " ".join(" ".join(command.argv) for command in retrieval)
    assert "transformers==4.57.6" in retrieval_text and "torchvision" not in retrieval_text
    cpu_vision = " ".join(" ".join(command.argv) for command in package_commands(py, "vision", cpu))
    assert "torch==2.14.1" in cpu_vision and "torchvision==0.29.1" in cpu_vision and PYTORCH_CPU in cpu_vision
    modern = " ".join(" ".join(command.argv) for command in package_commands(py, "retrieval", select_profile("rtx40")))
    assert PYTORCH_CU128 in modern and "<2.15" not in modern
    for command in vision + retrieval + ocr_gtx + dev:
        if command.argv[1:3] == ("-m", "pip"):
            assert command.argv[0].endswith("python.exe")
    ensure, upgrade = bootstrap_commands(_python("venv/bin/python"))
    assert ensure.argv[1:3] == ("-m", "ensurepip")
    assert upgrade.argv[1:3] == ("-m", "pip")


def test_observed_packages_match_the_selected_profile():
    cpu, gtx, rtx = _profile("cpu"), _profile("gtx10"), select_profile("rtx40", paddle_cuda="cu129")
    assert observed_satisfies("dev", cpu, {"dd_agent": True, "fastapi": True, "pytest": True, "psycopg": True})
    assert not observed_satisfies("dev", cpu, {"dd_agent": True, "fastapi": True, "pytest": False, "psycopg": True})
    assert observed_satisfies("pdf", cpu, {"pypdfium2": True, "PIL": True})
    assert observed_satisfies("ocr", cpu, {"paddleocr": "3.7.0", "paddle": "3.3.1", "paddle_cuda": "False"})
    assert not observed_satisfies("ocr", cpu, {"paddleocr": "3.7.0", "paddle": "3.3.1", "paddle_cuda": "11.8"})
    assert observed_satisfies("ocr", gtx, {"paddleocr": "3.7.0", "paddle": "3.3.1", "paddle_cuda": "11.8"})
    assert observed_satisfies("ocr", rtx, {"paddleocr": "3.7.0", "paddle": "3.3.1", "paddle_cuda": "12.9"})
    assert not observed_satisfies("ocr", rtx, {"paddleocr": "3.7.0", "paddle": "3.3.1", "paddle_cuda": "12.6"})
    assert observed_satisfies("vision", gtx, {"torch": "2.14.1+cu126", "torchvision": "0.29.1+cu126", "transformers": "5.20.1"})
    assert not observed_satisfies("vision", gtx, {"torch": "2.15.0+cu126", "torchvision": "0.29.1+cu126", "transformers": "5.20.1"})
    assert observed_satisfies("retrieval", cpu, {"torch": "2.14.1+cpu", "transformers": "4.57.6"})
    assert not observed_satisfies("retrieval", cpu, {"torch": "2.14.1+cu126", "transformers": "4.57.6"})
    assert observed_satisfies("retrieval", select_profile("rtx40"), {"torch": "2.16.0+cu128", "transformers": "4.57.6"})
    assert not observed_satisfies("vision", cpu, {"error": "ImportError"})


def test_reinstall_when_probe_fails_or_fingerprint_changes():
    fingerprint = command_fingerprint(_profile("cpu"), package_commands(_python("py"), "dev", _profile("cpu")), "hash-a")
    assert should_reinstall(False, stamp_document(fingerprint, "cpu"), fingerprint)
    assert not should_reinstall(True, None, fingerprint)
    assert not should_reinstall(True, stamp_document(fingerprint, "cpu"), fingerprint)
    assert should_reinstall(True, stamp_document(fingerprint, "cpu"), "other")


def test_env_merge_never_overwrites_existing_values(tmp_path):
    existing = "# mine\r\nDDA_PORT=9000\r\nDDA_DATABASE_URL=\r\nDDA_EMBEDDING_DIR=D:/keep/me\r\n"
    fills = {
        "DDA_DATABASE_URL": "postgresql://dda:s3cret@127.0.0.1:54329/dda",
        "DDA_PORT": "1",
        "DDA_EMBEDDING_DIR": "C:/new",
        "DDA_OCR_PYTHON": r"C:\Program Files\py.exe",
        "DDA_MCP_API_KEY": "local-mcp-secret",
    }
    merged = merge_env(existing, EXAMPLE, fills)
    text = merged.text
    assert text.startswith("# mine")
    assert "DDA_PORT=9000" in text and "DDA_PORT=1" not in text
    assert "DDA_EMBEDDING_DIR=D:/keep/me" in text and "C:/new" not in text
    assert 'DDA_DATABASE_URL="postgresql://dda:s3cret@127.0.0.1:54329/dda"' in text
    assert "DDA_OCR_PYTHON=" in text and "Program Files" in text
    assert "DDA_PORT" not in merged.written and "DDA_EMBEDDING_DIR" not in merged.written
    assert "DDA_DATABASE_URL" in merged.written and "DDA_MCP_API_KEY" in merged.written
    again = merge_env(text, EXAMPLE, fills)
    assert again.text == text
    assert again.written == ()
    values = env_map(text)
    assert values["DDA_PORT"] == "9000"
    assert values["DDA_EMBEDDING_DIR"] == "D:/keep/me"
    assert values["DDA_DATABASE_URL"].endswith("/dda")
    assert values["DDA_OCR_PYTHON"] == r"C:\Program Files\py.exe"
    described = "\n".join(describe_writes(merged.written))
    assert "local-mcp-secret" not in described and "s3cret" not in described
    assert "已写入（值不显示）" in described


def test_env_merge_from_example_fills_only_blanks_and_is_idempotent():
    fills = {"DDA_DATABASE_URL": "postgresql://dda:s3cret@127.0.0.1:54329/dda", "DDA_PORT": "1"}
    created = merge_env(None, EXAMPLE, fills)
    assert "# 注释保留" in created.text
    assert "DDA_PORT=8765" in created.text
    assert "s3cret" in created.text
    second = merge_env(created.text, EXAMPLE, fills)
    assert second.text == created.text
    quoted = format_assignment("DDA_MCP_API_KEY", 'a"b\\c')
    assert env_map(quoted)["DDA_MCP_API_KEY"] == 'a"b\\c'


def test_model_destinations_honor_existing_env(tmp_path):
    windows = conventional_fills(tmp_path, True)
    posix = conventional_fills(tmp_path, False)
    assert windows["DDA_PDF_PYTHON"].endswith(".venv-docling\\Scripts\\python.exe") or windows["DDA_PDF_PYTHON"].endswith(".venv-docling/Scripts/python.exe")
    assert posix["DDA_VISION_PYTHON"].endswith(".venv-vision/bin/python")
    values = {"DDA_EMBEDDING_DIR": "D:/keep/embed", "DDA_RERANKER_DIR": "D:/keep/rerank"}
    jobs = model_jobs(tmp_path, values, skip_models=False, skip_ocr=True, skip_vision=True, skip_retrieval=False)
    assert [job.repo for job in jobs] == ["Qwen/Qwen3-Embedding-0.6B", "Qwen/Qwen3-Reranker-0.6B"]
    assert jobs[0].dest == Path("D:/keep/embed")
    assert model_jobs(tmp_path, {}, skip_models=True, skip_ocr=False, skip_vision=False, skip_retrieval=False) == ()


def test_remote_lists_and_model_assessment(tmp_path):
    hf_url = listing_url("hf-mirror", "Qwen/Qwen3-Embedding-0.6B")
    assert hf_url.startswith(HF_MIRROR + "/api/models/")
    assert listing_url("huggingface", "Qwen/Qwen3-Embedding-0.6B").startswith("https://huggingface.co/")
    assert listing_url("modelscope", "Qwen/Qwen3-Embedding-0.6B").startswith(MODELSCOPE)
    files = parse_hf_tree([
        {"type": "file", "path": "README.md", "size": 10},
        {"type": "file", "path": ".gitattributes", "size": 4},
        {"type": "file", "path": "config.json", "size": 12},
        {"type": "file", "path": "model.safetensors", "size": 20, "lfs": {"oid": "sha256:abc", "size": 20}},
        {"type": "directory", "path": "1_Pooling"},
    ])
    assert [item.relative for item in files] == ["config.json", "model.safetensors"]
    assert files[1].sha256 == "abc" and files[1].size == 20
    linked = with_urls(files, "hf-mirror", "Qwen/Qwen3-Embedding-0.6B")
    assert linked[1].url == file_url("hf-mirror", "Qwen/Qwen3-Embedding-0.6B", "model.safetensors")
    ms = parse_modelscope_files({"Data": {"Files": [
        {"Type": "tree", "Path": "1_Pooling", "Size": 0},
        {"Type": "blob", "Path": "inference.pdiparams", "Size": 5000, "Sha256": "ff"},
        {"Type": "blob", "Path": "README.md", "Size": 3, "Sha256": "00"},
    ]}})
    assert [item.relative for item in ms] == ["inference.pdiparams"]
    ocr = tmp_path / "ocr"
    assert not assess_model(ocr, "ocr").ok
    ocr.mkdir()
    (ocr / "inference.json").write_text("{}", encoding="utf-8")
    (ocr / "inference.yml").write_text("x: 1\n", encoding="utf-8")
    (ocr / "inference.pdiparams").write_bytes(b"x" * 10)
    missing = assess_model(ocr, "ocr")
    assert not missing.ok and "inference.pdiparams" in missing.missing
    (ocr / "inference.pdiparams").write_bytes(b"<html>nope</html>" + b"y" * MIN_WEIGHT_BYTES)
    assert not assess_model(ocr, "ocr").ok
    (ocr / "inference.pdiparams").write_bytes(b"w" * MIN_WEIGHT_BYTES)
    assert assess_model(ocr, "ocr").ok
    (ocr / "stray.partial").write_bytes(b"1234")
    assert not assess_model(ocr, "ocr").ok
    vision = tmp_path / "vision"
    vision.mkdir()
    (vision / "config.json").write_text('{"model_type":"demo"}', encoding="utf-8")
    (vision / ".cache").mkdir()
    (vision / ".cache" / "skip.safetensors").write_bytes(b"z" * MIN_WEIGHT_BYTES)
    assert not assess_model(vision, "hf").ok
    (vision / "model.safetensors").write_bytes(b"z" * MIN_WEIGHT_BYTES)
    assert assess_model(vision, "hf").ok
    body = b"abc"
    digest = hashlib.sha256(body).hexdigest()
    remote = with_urls((files[0],), "huggingface", "Org/name")
    # config.json from the parsed tree has no sha and size 12, so it does not match body.
    needed = files_needed(vision, (type(remote[0])(remote[0].relative, len(body), digest, remote[0].url),))
    assert needed and needed[0].sha256 == digest
    target = vision / "config.json"
    target.write_bytes(body)
    assert files_needed(vision, needed) == ()
    target.write_bytes(b"zzz")
    assert files_needed(vision, needed)


def test_download_skips_verified_files_and_resumes(tmp_path):
    body = b"hello-model-file"
    digest = hashlib.sha256(body).hexdigest()
    dest = tmp_path / "nested" / "weights.txt"
    calls = {"count": 0}

    class Response:
        def __init__(self, status, payload):
            self.status = status
            self._body = payload

        def read(self, size=-1):
            if size is None or size < 0:
                size = len(self._body)
            chunk, self._body = self._body[:size], self._body[size:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        calls["count"] += 1
        requested = request.get_header("Range")
        if requested:
            start = int(requested.split("=", 1)[1].split("-", 1)[0])
            return Response(206, body[start:])
        return Response(200, body)

    assert download("https://example.test/weights.txt", dest, len(body), digest, opener) == "downloaded"
    assert dest.read_bytes() == body
    assert not Path(str(dest) + ".partial").exists()
    assert download("https://example.test/weights.txt", dest, len(body), digest, opener) == "skipped"
    assert calls["count"] == 1

    other = tmp_path / "resume.txt"
    partial = Path(str(other) + ".partial")
    partial.write_bytes(body[:5])

    def resume(request, timeout=None):
        assert request.get_header("Range") == "bytes=5-"
        return Response(206, body[5:])

    assert download("https://example.test/resume.txt", other, len(body), digest, resume) == "downloaded"
    assert other.read_bytes() == body

    rewrite = tmp_path / "rewrite.txt"
    Path(str(rewrite) + ".partial").write_bytes(b"XXXX")

    def full(request, timeout=None):
        assert request.get_header("Range") == "bytes=4-"
        return Response(200, body)

    assert download("https://example.test/rewrite.txt", rewrite, len(body), digest, full) == "downloaded"
    assert rewrite.read_bytes() == body


def test_database_plan_refuses_reserved_names_and_keeps_existing_url():
    url = validate_database_url("postgresql://dda:s3cret@127.0.0.1:5432/dda")
    assert url.startswith("postgresql://")
    with pytest.raises(SetupError, match="postgres"):
        validate_database_url("postgresql://dda:s3cret@127.0.0.1:5432/postgres")
    with pytest.raises(SetupError, match="template1"):
        validate_database_url("postgres://dda@localhost/template1")
    with pytest.raises(SetupError, match="主机"):
        validate_database_url("postgresql:///dda")
    with pytest.raises(SetupError, match="PostgreSQL"):
        validate_database_url("mysql://dda:s3cret@127.0.0.1/dda")
    with pytest.raises(SetupError):
        validate_port(5432)
    kept = plan_database("ask", url, "postgresql://dda:other@127.0.0.1:5432/otherdb", False)
    assert kept.action == "keep" and kept.url == url
    docker = plan_database("docker", "", "", False)
    assert docker.action == "docker" and docker.url == ""
    skipped = plan_database("skip", url, "", False)
    assert skipped.action == "skip" and skipped.url == url
    with pytest.raises(SetupError, match="连接串"):
        plan_database("existing", "", "", False)
    assert classify_volume(nonempty=False, has_env_file=False) == "fresh"
    assert classify_volume(nonempty=True, has_env_file=True) == "reuse"
    assert classify_volume(nonempty=True, has_env_file=False) == "refuse"
    compose = render_compose()
    assert "127.0.0.1:" in compose and "0.0.0.0" not in compose
    assert "pgvector/pgvector:0.8.7-pg17" in compose
    assert "com.dd-agent.setup" in compose
    secret = "very-secret-password"
    env = render_pg_env(secret, 54329, r"D:\repo\.local\postgres")
    assert secret in env and "D:/repo/.local/postgres" in env
    assert secret not in compose
    built = build_local_database_url(secret, 54329)
    assert built == "postgresql://dda:very-secret-password@127.0.0.1:54329/dda"
    assert "5432" in postgres_help_text() or "业务库" in postgres_help_text()
    assert "不会连接" in postgres_help_text()
    summary = format_summary([], [secret, built])
    assert secret not in summary
    noisy = format_summary(
        [type("Row", (), {"name": "数据库", "state": "ready", "detail": built})()],
        [secret, built],
    )
    assert secret not in noisy and "very-secret-password" not in noisy
    assert redact("token local-mcp-secret end", ["local-mcp-secret"]) == "token *** end"
    assert compose_volume(r"D:\repo\.local\postgres") == "D:/repo/.local/postgres"


def test_dotenv_reads_the_values_we_write(tmp_path):
    dotenv = pytest.importorskip("dotenv")
    fills = {
        "DDA_DATABASE_URL": "postgresql://dda:s3cret@127.0.0.1:54329/dda",
        "DDA_OCR_PYTHON": r"C:\Program Files\py.exe",
        "DDA_MCP_API_KEY": 'a"b\\c',
    }
    merged = merge_env(None, EXAMPLE, fills)
    target = tmp_path / ".env"
    target.write_text(merged.text, encoding="utf-8")
    loaded = dotenv.dotenv_values(target)
    assert loaded["DDA_DATABASE_URL"] == fills["DDA_DATABASE_URL"]
    assert loaded["DDA_OCR_PYTHON"] == fills["DDA_OCR_PYTHON"]
    assert loaded["DDA_MCP_API_KEY"] == fills["DDA_MCP_API_KEY"]
    assert loaded["DDA_PORT"] == "8765"


def test_soffice_version_gate():
    assert parse_soffice_version("LibreOffice 24.2.5.2 420(Build:2)") == (24, 2)
    assert soffice_is_new_enough((24, 2))
    assert not soffice_is_new_enough((7, 6))
    assert not soffice_is_new_enough(None)


def test_dry_run_does_not_touch_env_or_print_secrets(tmp_path, capsys):
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    secret = "postgresql://dda:keep-secret-value@127.0.0.1:5432/dda"
    original = "DDA_DATABASE_URL=" + secret + "\nDDA_PORT=9000\nDDA_MCP_API_KEY=local-mcp-secret\n"
    (tmp_path / ".env").write_text(original, encoding="utf-8")
    code = main([
        "--root", str(tmp_path), "--dry-run", "--device", "cpu", "--non-interactive",
        "--postgres", "skip", "--skip-models", "--skip-libreoffice", "--skip-vision",
    ])
    assert code == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == original
    assert not (tmp_path / ".venv-dev").exists()
    output = capsys.readouterr().out
    assert "keep-secret-value" not in output
    assert "local-mcp-secret" not in output
    assert "dry-run" in output or "未执行" in output
    assert "torch==2.14.1" in output


def test_module_entry_accepts_help():
    import os
    import subprocess
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "scripts")
    completed = subprocess.run(
        [sys.executable, "-m", "local_setup", "--help"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0
    assert "--device" in completed.stdout
    assert "gtx10" in completed.stdout


def test_windows_entrypoint_mentions_python_312_and_installer():
    script = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8-sig")
    assert "3.12" in script and "3.13" in script
    assert "-m" in script and "local_setup" in script
    assert "PYTHONPATH" in script
    assert "uv" in script and "winget" in script
    assert "-Device" in script or "$Device" in script
    assert "gtx10" in script and "rtx40" in script
    assert "keep-secret" not in script and "postgresql://" not in script
    shell = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    assert "3.12" in shell and "apt" in shell and "-m local_setup" in shell
    assert json.dumps(parse_hf_tree([])) == "[]"


def _gbk(text: str) -> bytes:
    encoded = text.encode("gbk")
    with pytest.raises(UnicodeDecodeError):
        encoded.decode("utf-8")
    return encoded


def _reject_text_mode(**kwargs):
    if kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding"):
        raise UnicodeDecodeError("utf-8", b"\xd0", 0, 1, "invalid continuation byte")


def _bytes_completed(argv, stdout: bytes, stderr: bytes = b"", code: int = 0, **kwargs):
    _reject_text_mode(**kwargs)
    return subprocess.CompletedProcess(list(argv), code, stdout, stderr)


def test_execute_passes_root_into_model_download(tmp_path, monkeypatch, capsys):
    # 当前 main 在 execute() 里调用 _download_models 时没有把 root 传进去，
    # 第一个模型（PP-OCRv5_mobile_det）一进入下载就会 NameError。
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    calls = {"fetch": [], "download": []}

    def block_network(*_args, **_kwargs):
        raise AssertionError("测试不应访问网络或下载模型")

    def fake_fetch(url, opener=None):
        calls["fetch"].append(url)
        assert url.startswith("https://hf-mirror.com/api/models/PaddlePaddle/")
        return [
            {
                "type": "file",
                "path": "inference.pdiparams",
                "size": MIN_WEIGHT_BYTES,
                "lfs": {"oid": "sha256:aa", "size": MIN_WEIGHT_BYTES},
            },
            {"type": "file", "path": "inference.json", "size": 2},
            {"type": "file", "path": "inference.yml", "size": 4},
            {"type": "file", "path": "README.md", "size": 10},
        ]

    def fake_download(url, dest, size, sha256, opener=None):
        calls["download"].append((url, Path(dest)))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"w" * (size or 1))
        return "downloaded"

    monkeypatch.setattr("local_setup.fetch.urlopen", block_network)
    monkeypatch.setattr(installer, "fetch_json", fake_fetch)
    monkeypatch.setattr(installer, "download", fake_download)
    monkeypatch.setattr(installer, "_install_components", lambda *_args, **_kwargs: None)
    argv = [
        "--root", str(tmp_path), "--device", "cpu", "--non-interactive",
        "--model-source", "hf-mirror", "--postgres", "skip", "--skip-database",
        "--skip-libreoffice", "--skip-vision", "--skip-retrieval", "--skip-pdf",
        "--no-mcp-key",
    ]
    assert installer.main(argv) == 0
    first_out = capsys.readouterr().out
    assert "PaddlePaddle/PP-OCRv5_mobile_det" in first_out
    assert "keep-secret" not in first_out and "postgresql://" not in first_out
    assert len(calls["fetch"]) == 4
    assert len(calls["download"]) == 12
    assert all(url.startswith("https://hf-mirror.com/") for url, _dest in calls["download"])
    first_dest = calls["download"][0][1]
    assert first_dest.parts[-2:] == ("PP-OCRv5_mobile_det", "inference.pdiparams")
    model_dir = tmp_path / "models" / "ocr" / "official_models" / "PP-OCRv5_mobile_det"
    assert assess_model(model_dir, "ocr").ok
    checksum = tmp_path / ".local" / "setup" / "checksums" / "PaddlePaddle__PP-OCRv5_mobile_det.json"
    assert checksum.is_file()
    assert checksum.is_relative_to(tmp_path / ".local" / "setup" / "checksums")
    saved = json.loads(checksum.read_text(encoding="utf-8"))
    assert saved["inference.pdiparams"]["size"] >= MIN_WEIGHT_BYTES
    assert not list((tmp_path / "models").rglob("*checksum*"))
    assert not (model_dir / ".dda-checksums.json").exists()

    assert installer.main(argv) == 0
    second_out = capsys.readouterr().out
    assert len(calls["fetch"]) == 4
    assert len(calls["download"]) == 12
    assert "已存在且通过校验，跳过" in second_out
    assert "需要下载" not in second_out


def test_decode_output_uses_utf8_then_gbk():
    text = "虚拟环境"
    raw = _gbk(text)
    assert raw[0] == 0xD0
    assert installer._decode_output(text.encode("utf-8")) == text
    assert installer._decode_output(raw) == text
    assert installer._decode_output(b"") == ""
    assert installer._decode_output("已经是文本") == "已经是文本"
    garbage = installer._decode_output(b"\xff\xfe")
    assert isinstance(garbage, str)


def test_run_prints_gbk_failure_tail(tmp_path, monkeypatch, capsys):
    message = "虚拟环境创建失败"
    stdout = "标准输出有说明".encode("gbk")
    stderr = message.encode("gbk")
    assert stderr[0] == 0xD0
    # 标记写在子进程里，确认 PYTHONUTF8 / PYTHONIOENCODING 仍传给 Python 子进程。
    child = (
        "import os, sys\n"
        f"sys.stdout.buffer.write({stdout!r})\n"
        f"sys.stderr.buffer.write({stderr!r})\n"
        "marker = os.environ.get('PYTHONUTF8', '') + '|' + os.environ.get('PYTHONIOENCODING', '')\n"
        "sys.stderr.buffer.write(b'\\n' + marker.encode('ascii'))\n"
        "raise SystemExit(4)\n"
    )
    monkeypatch.setenv("DDA_DATABASE_URL", "postgresql://dda:keep-secret-value@127.0.0.1:5432/dda")
    reporter = installer.Reporter()
    with pytest.raises(SetupError) as caught:
        installer._run((sys.executable, "-c", child), tmp_path, reporter)
    assert "退出码 4" in str(caught.value)
    output = capsys.readouterr().out
    assert message in output
    assert "标准输出有说明" in output
    assert "1|utf-8" in output
    assert "keep-secret-value" not in output
    assert "postgresql://" not in output

    success = "已创建虚拟环境".encode("gbk")
    code = installer._run(
        (sys.executable, "-c", f"import sys; sys.stdout.buffer.write({success!r})"),
        tmp_path, installer.Reporter(),
    )
    assert code == 0

    garbage = "import sys; sys.stderr.buffer.write(b'\\xff\\xfe'); raise SystemExit(2)"
    code = installer._run((sys.executable, "-c", garbage), tmp_path, installer.Reporter(), optional=True)
    assert code == 2
    trailed = capsys.readouterr().out
    assert "UnicodeDecodeError" not in trailed


def test_installer_helpers_decode_gbk_streams(tmp_path, monkeypatch, capsys):
    seen = []
    real_decode = installer._decode_output

    def spy(payload):
        text = real_decode(payload)
        seen.append(text)
        return text

    monkeypatch.setattr(installer, "_decode_output", spy)
    monkeypatch.setattr(installer.shutil, "which", lambda name: name if name == "nvidia-smi" else None)
    gpu = _gbk("NVIDIA GeForce RTX 4070 笔记本, 8.9\n")
    monkeypatch.setattr(
        installer.subprocess, "run",
        lambda argv, **kwargs: _bytes_completed(argv, gpu, **kwargs),
    )
    name, capability, note = installer._probe_nvidia()
    assert name == "NVIDIA GeForce RTX 4070 笔记本"
    assert capability == (8, 9) and note == ""
    assert any("笔记本" in item for item in seen)

    pip_out = _gbk("pip 25.0 来自虚拟环境\n")
    pip_calls = []

    def fake_pip(argv, **kwargs):
        _reject_text_mode(**kwargs)
        pip_calls.append(kwargs.get("env") or {})
        return subprocess.CompletedProcess(list(argv), 0, pip_out, b"")

    monkeypatch.setattr(installer.subprocess, "run", fake_pip)
    assert installer._pip_works(Path("python")) is True
    assert pip_calls[0]["PYTHONUTF8"] == "1" and pip_calls[0]["PYTHONIOENCODING"] == "utf-8"
    assert any("来自虚拟环境" in item for item in seen)

    probe = _gbk("虚拟环境检查\n") + b'DDA_SETUP_PROBE {"dd_agent": true, "fastapi": true, "pytest": true, "psycopg": true}\n'
    probe_calls = []

    def fake_probe(argv, **kwargs):
        _reject_text_mode(**kwargs)
        probe_calls.append(kwargs.get("env") or {})
        return subprocess.CompletedProcess(list(argv), 0, probe, _gbk("探测标准错误"))

    monkeypatch.setattr(installer.subprocess, "run", fake_probe)
    assert installer._probe_ok(Path("python"), "dev", select_profile("cpu"), installer.Reporter()) is True
    assert probe_calls[0]["PYTHONUTF8"] == "1" and probe_calls[0]["PYTHONIOENCODING"] == "utf-8"
    assert any("虚拟环境检查" in item for item in seen)
    assert any("探测标准错误" in item for item in seen)

    version = _gbk("LibreOffice 24.2.5.2 40(Build:2) 中文版\n")
    monkeypatch.setattr(
        installer.subprocess, "run",
        lambda argv, **kwargs: _bytes_completed(argv, version, **kwargs),
    )
    assert installer._soffice_version(Path("soffice")) == (24, 2)
    assert any("中文版" in item for item in seen)

    failure = _gbk("数据库检查失败：扩展未安装")
    app_calls = []

    def fake_app(argv, **kwargs):
        _reject_text_mode(**kwargs)
        app_calls.append(kwargs.get("env") or {})
        return subprocess.CompletedProcess(list(argv), 1, b"", failure)

    monkeypatch.setattr(installer.subprocess, "run", fake_app)
    code, output = installer._app_command(Path("python"), tmp_path, "check")
    assert code == 1
    assert "扩展未安装" in output
    assert "postgresql://" not in output
    assert app_calls[0]["PYTHONUTF8"] == "1" and app_calls[0]["PYTHONIOENCODING"] == "utf-8"

    note = "项目数据库"
    body = json.dumps(
        [{"Config": {"Labels": {"com.dd-agent.setup": "1", "note": note}}}],
        ensure_ascii=False,
    ).encode("gbk")
    monkeypatch.setattr(
        installer.subprocess, "run",
        lambda argv, **kwargs: _bytes_completed(argv, body, _gbk("检查容器"), **kwargs),
    )
    installer._assert_container_owner(installer.Reporter())
    assert "复用已有的" in capsys.readouterr().out
    assert any(note in item for item in seen)

    def fake_compose(argv, **kwargs):
        _reject_text_mode(**kwargs)
        command = list(argv)
        if command[:2] == ["docker", "compose"]:
            return subprocess.CompletedProcess(command, 0, _gbk("已启动"), b"")
        if command[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(command, 0, b"healthy\n", _gbk("容器日志：正常"))
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(command, 1, b"", _gbk("没有这个容器"))
        raise AssertionError(command)

    def fail_sleep(*_args, **_kwargs):
        raise AssertionError("容器应立即健康")

    monkeypatch.setattr(installer.subprocess, "run", fake_compose)
    monkeypatch.setattr(installer.time, "sleep", fail_sleep)
    installer._compose_up(tmp_path, tmp_path / "postgres.env", installer.Reporter())
    assert any("已启动" in item for item in seen)
    assert any("容器日志：正常" in item for item in seen)
    assert any("没有这个容器" in item for item in seen)
