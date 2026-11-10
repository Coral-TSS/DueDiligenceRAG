"""本机安装入口。Windows 由 scripts/setup.ps1 找到 Python 3.12 后再调用。

只在用户给出的连接串，或本脚本新建的 127.0.0.1 容器上运行 init-db。
不扫描、不修改其他数据库。密钥和连接串写入 .env，不在终端打印。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .fetch import download, fetch_json
from .plan import (
    ENV_DIRS,
    POSTGRES_CONTAINER,
    POSTGRES_IMAGE,
    SECRET_KEYS,
    Command,
    DatabasePlan,
    ModelJob,
    SetupError,
    Status,
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
    format_plan,
    format_summary,
    listing_url,
    merge_env,
    model_jobs,
    observed_satisfies,
    package_commands,
    parse_args,
    parse_capability,
    parse_hf_tree,
    parse_modelscope_files,
    parse_pg_env,
    parse_probe,
    parse_soffice_version,
    pip_fallback_command,
    plan_database,
    postgres_help_text,
    probe_source,
    redact,
    render_compose,
    render_pg_env,
    require_python,
    select_profile,
    selected_components,
    should_reinstall,
    soffice_is_new_enough,
    stamp_document,
    validate_database_url,
    venv_python,
    winget_libreoffice,
    windows_soffice_candidates,
    with_urls,
)


class Reporter:
    def __init__(self):
        self.rows: list[Status] = []
        self.secrets: list[str] = []
        self.summarized = False

    def secret(self, value: str) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    def say(self, text: str) -> None:
        print(redact(text, self.secrets), flush=True)

    def add(self, name: str, state: str, detail: str = "") -> None:
        self.rows.append(Status(name, state, detail))

    def summary(self) -> None:
        if self.summarized:
            return
        self.summarized = True
        self.say("")
        self.say(format_summary(self.rows, self.secrets).rstrip())


def default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    reporter = Reporter()
    try:
        args = parse_args(argv)
        code = execute(args, reporter)
    except SetupError as exc:
        reporter.say("失败：" + str(exc))
        if exc.hint:
            reporter.say("下一步：" + exc.hint)
        reporter.say("可以重新运行同一命令。已经完成的步骤会跳过。")
        reporter.summary()
        return 1
    except KeyboardInterrupt:
        reporter.say("已中断。可以重新运行同一命令继续。")
        reporter.summary()
        return 1
    reporter.summary()
    return code


def execute(args, reporter: Reporter) -> int:
    require_python(tuple(sys.version_info))
    root = (args.root or default_root()).resolve()
    example_path = root / ".env.example"
    if not example_path.is_file():
        raise SetupError("找不到 .env.example。", "请在仓库根目录运行安装脚本。")
    if not (root / "pyproject.toml").is_file():
        raise SetupError("找不到 pyproject.toml。", "请在仓库根目录运行安装脚本。")
    windows = os.name == "nt"
    reporter.say("尽调材料工作区本地安装")
    reporter.say(f"仓库：{root}")
    reporter.say("不会覆盖 .env 里已有的非空配置，也不会连接你没有明确指定的数据库。")
    reporter.say(f"Python：{sys.version.split()[0]}（{sys.executable}）")
    reporter.add("Python 3.12", "ready", sys.version.split()[0])

    gpu_name, capability, gpu_note = _gpu(args)
    profile = select_profile(args.device, gpu_name, capability, args.paddle_cuda)
    reporter.say("显卡：" + profile.reason)
    if gpu_note:
        reporter.say(gpu_note)
    reporter.add("显卡轮子", "ready", profile.id + ("" if not profile.paddle_cuda else f" / Paddle {profile.paddle_cuda}"))

    example = example_path.read_text(encoding="utf-8")
    env_path = root / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else None
    current = env_map(existing or "")
    for key in SECRET_KEYS:
        reporter.secret(current.get(key, ""))
    preview = merge_env(existing, example, conventional_fills(root, windows))
    values = env_map(preview.text)
    components = selected_components(args.skip_pdf, args.skip_ocr, args.skip_vision, args.skip_retrieval)
    commands = {name: package_commands(venv_python(root, name, windows), name, profile) for name in components}
    jobs = model_jobs(
        root, values,
        skip_models=args.skip_models, skip_ocr=args.skip_ocr,
        skip_vision=args.skip_vision, skip_retrieval=args.skip_retrieval,
    )
    database = plan_database(args.postgres, current.get("DDA_DATABASE_URL", ""), args.database_url, args.skip_database)
    if args.dry_run:
        if database.action == "ask":
            database = DatabasePlan("ask", "", "未执行。正式运行时会询问：输入专用库连接串、使用 Docker，或跳过。")
        reporter.say(format_plan(profile, commands, jobs, database, describe_writes(preview.written)).rstrip())
        reporter.add("执行", "skipped", "dry-run，没有安装或写入")
        return 0

    _install_components(root, components, profile, commands, reporter)
    soffice = _libreoffice(args, root, reporter)
    source = _model_source(args, jobs, reporter)
    _download_models(jobs, source, reporter)
    database, database_url = _database(args, root, database, reporter)
    fills = dict(conventional_fills(root, windows))
    if soffice:
        fills["DDA_SOFFICE_PATH"] = soffice
    if database.action in {"write", "docker"} and database_url:
        fills["DDA_DATABASE_URL"] = database_url
        reporter.secret(database_url)
    mcp_note = _mcp_fill(args, current, fills, reporter)
    merged = merge_env(existing, example, fills)
    _write_env(env_path, existing, merged.text, merged.written, reporter)
    if mcp_note:
        reporter.say(mcp_note)
    failed = _finish_database(root, database, windows, reporter)
    _launch_hint(root, windows, reporter)
    return 1 if failed else 0


def _gpu(args) -> tuple[str, tuple[int, int] | None, str]:
    if args.device != "auto":
        return "", None, ""
    name = args.gpu_name.strip()
    capability = parse_capability(args.compute_capability) if args.compute_capability.strip() else None
    note = ""
    if not name and capability is None:
        name, capability, note = _probe_nvidia()
    elif args.compute_capability.strip() and capability is None:
        raise SetupError("计算能力无法解析。")
    return name, capability, note


def _probe_nvidia() -> tuple[str, tuple[int, int] | None, str]:
    if shutil.which("nvidia-smi") is None:
        return "", None, ""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", None, "nvidia-smi 没有返回。按 CPU 安装；若实际有 NVIDIA 显卡，请加上 --device。"
    if completed.returncode != 0 or not completed.stdout.strip():
        return "", None, ""
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    parts = [item.strip() for item in lines[0].split(",")]
    name = parts[0] if parts else ""
    capability = parse_capability(parts[1]) if len(parts) > 1 else None
    note = f"检测到 {len(lines)} 块显卡，使用第一块：{name}。" if len(lines) > 1 else ""
    return name, capability, note


def _install_components(root: Path, components: tuple[str, ...], profile, commands: dict, reporter: Reporter) -> None:
    labels = {
        "dev": "主环境 .venv-dev",
        "pdf": "PDF .venv-docling",
        "ocr": "OCR .venv-ocr",
        "vision": "视觉 .venv-vision",
        "retrieval": "检索 .venv-retrieval",
    }
    skipped = {"pdf": "已按参数跳过", "ocr": "已按参数跳过", "vision": "已按参数跳过", "retrieval": "已按参数跳过"}
    for name in ("pdf", "ocr", "vision", "retrieval"):
        if name not in components:
            reporter.add(labels[name], "skipped", skipped[name])
    for name in components:
        reporter.say(f"环境 {ENV_DIRS[name]}")
        detail = _install_one(root, name, profile, commands[name], reporter)
        reporter.add(labels[name], "ready", detail)


def _install_one(root: Path, component: str, profile, commands: tuple[Command, ...], reporter: Reporter) -> str:
    windows = os.name == "nt"
    py = venv_python(root, component, windows)
    extra = ""
    if component == "dev":
        extra = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    fingerprint = command_fingerprint(profile, commands, extra)
    stamp_path = root / ".local" / "setup" / "stamps" / f"{component}.json"
    stamp = _read_json(stamp_path)
    probe_ok = False
    if py.is_file():
        if not _pip_works(py):
            _bootstrap(py, reporter)
        probe_ok = _probe_ok(py, component, profile, reporter)
    if not should_reinstall(probe_ok, stamp, fingerprint):
        _write_json(stamp_path, stamp_document(fingerprint, profile.id))
        reporter.say("      已安装且与当前方案一致，跳过。")
        return "已安装，跳过"
    if not py.is_file():
        reporter.say(f"      正在创建 {ENV_DIRS[component]}")
        _create_venv(py.parent.parent, py, reporter)
    if _pip_works(py):
        _run((str(py), "-m", "pip", "install", "-U", "pip"), root, reporter, timeout=180)
    else:
        _bootstrap(py, reporter)
    for command in commands:
        _run(command.argv, root, reporter, optional=command.optional)
    if not _probe_ok(py, component, profile, reporter):
        raise SetupError(
            f"{ENV_DIRS[component]} 安装后仍未通过检查。",
            "请看上面的命令输出。确认 Python 是 3.12 后可删掉该目录再运行；不要改用系统 pip。",
        )
    _write_json(stamp_path, stamp_document(fingerprint, profile.id))
    return profile.id


def _create_venv(dest: Path, python: Path, reporter: Reporter) -> None:
    # 优先用标准库 venv。uv 提供的解释器有时没有 ensurepip，这时改用 uv venv --seed，避免去调用系统里的裸 pip。
    code = _run((sys.executable, "-m", "venv", str(dest)), dest.parent, reporter, optional=True, timeout=180)
    if code == 0 and python.is_file():
        return
    uv = shutil.which("uv")
    if uv is None:
        raise SetupError(
            "无法创建带 pip 的虚拟环境。",
            "当前 Python 3.12 不能用 venv/ensurepip。请安装 uv 后重新运行；脚本会改用 uv venv --seed，而不是调用指向 3.13 的 pip。",
        )
    reporter.say("      python -m venv 不可用，改用 uv venv --seed。")
    _run((uv, "venv", "--python", sys.executable, "--seed", "--allow-existing", str(dest)), dest.parent, reporter, timeout=180)
    if not python.is_file():
        raise SetupError("虚拟环境已创建，但没有找到解释器。", f"期望的路径是 {python}。可以删掉该目录后重新运行。")


def _bootstrap(python: Path, reporter: Reporter) -> None:
    if _pip_works(python):
        return
    ensure, upgrade = bootstrap_commands(python)
    reporter.say("      虚拟环境里没有 pip，正在补齐（不用系统里的裸 pip）。")
    if _run(ensure.argv, python.parent, reporter, optional=True, timeout=180) != 0:
        uv = shutil.which("uv")
        if uv is None:
            raise SetupError(
                "ensurepip 不可用，虚拟环境里仍然没有 pip。",
                "这常见于 uv 提供的解释器。请先安装 uv，然后重新运行；脚本会用 uv pip 装上 pip。",
            )
        _run(pip_fallback_command(uv, python).argv, python.parent, reporter, timeout=180)
    _run(upgrade.argv, python.parent, reporter, timeout=180)
    if not _pip_works(python):
        raise SetupError("补齐 pip 后仍然无法运行 python -m pip。", "删掉对应的 .venv-* 目录后重新运行。")


def _pip_works(python: Path) -> bool:
    try:
        completed = subprocess.run(
            [str(python), "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _probe_ok(python: Path, component: str, profile, reporter: Reporter) -> bool:
    try:
        completed = subprocess.run(
            [str(python), "-c", probe_source(component)],
            capture_output=True, text=True, timeout=180, check=False,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    try:
        observed = parse_probe(completed.stdout)
    except SetupError:
        return False
    return observed_satisfies(component, profile, observed)


def _libreoffice(args, root: Path, reporter: Reporter) -> str:
    if args.skip_libreoffice:
        reporter.add("LibreOffice", "skipped", "已按参数跳过。旧版 .doc/.xls/.ppt 暂不可转换")
        return ""
    found = _find_soffice()
    if found is None and os.name == "nt" and shutil.which("winget"):
        reporter.say("可选组件：正在用 winget 安装 LibreOffice 24.2 或更新版本。失败不会中断安装。")
        _run(winget_libreoffice(), root, reporter, optional=True)
        found = _find_soffice()
    if found is None:
        hint = "未安装。需要转换旧版 Office 时，安装 LibreOffice 24.2+ 后重新运行，或设置 DDA_SOFFICE_PATH。"
        if os.name != "nt":
            hint = "未安装。Linux/macOS 请用系统包管理器安装 LibreOffice 24.2+，脚本不会调用 apt 或 brew。"
        reporter.add("LibreOffice", "skipped", hint)
        return ""
    version = _soffice_version(found)
    if version is None:
        reporter.add("LibreOffice", "ready", str(found) + "（未能确认版本，建议 24.2 或更新）")
        return str(found)
    if not soffice_is_new_enough(version):
        reporter.add("LibreOffice", "ready", f"{found} 版本 {version[0]}.{version[1]}，低于 24.2，旧格式转换可能失败")
        return str(found)
    reporter.add("LibreOffice", "ready", f"{version[0]}.{version[1]}")
    return str(found)


def _find_soffice() -> Path | None:
    if os.name == "nt":
        for candidate in windows_soffice_candidates(os.environ.get("LOCALAPPDATA", "")):
            if candidate.is_file():
                return candidate
        return None
    found = shutil.which("soffice")
    return Path(found) if found else None


def _soffice_version(path: Path) -> tuple[int, int] | None:
    try:
        completed = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_soffice_version((completed.stdout or "") + "\n" + (completed.stderr or ""))


def _model_source(args, jobs: tuple[ModelJob, ...], reporter: Reporter) -> str:
    if not jobs:
        reporter.add("模型", "skipped", "已按参数跳过")
        return args.model_source or "huggingface"
    if args.model_source:
        reporter.say(f"模型来源：{args.model_source}")
        return args.model_source
    if args.non_interactive or not sys.stdin.isatty():
        reporter.say("未指定模型来源，使用 Hugging Face。国内网络可加 --model-source hf-mirror 或 modelscope。")
        return "huggingface"
    reporter.say("模型从哪里下载？已存在并通过校验的目录会跳过。")
    return _ask((
        ("huggingface", "Hugging Face"),
        ("hf-mirror", "hf-mirror.com（国内镜像，仓库与 Hugging Face 相同）"),
        ("modelscope", "ModelScope 魔搭（国内）"),
    ))


def _download_models(jobs: tuple[ModelJob, ...], source: str, reporter: Reporter) -> None:
    if not jobs:
        return
    grouped = {"ocr": [], "vision": [], "retrieval": []}
    for job in jobs:
        reporter.say(f"模型 {job.repo}")
        state, detail = _download_one(root, job, source, reporter)
        grouped[job.component].append((state, detail, job.repo))
    names = {"ocr": "OCR 模型", "vision": "视觉模型", "retrieval": "向量和重排序模型"}
    for key, rows in grouped.items():
        if not rows:
            continue
        if any(state == "failed" for state, _, _ in rows):
            reporter.add(names[key], "failed", "有模型未通过校验")
        else:
            reporter.add(names[key], "ready", f"{len(rows)} 个目录已校验")


def _download_one(root: Path, job: ModelJob, source: str, reporter: Reporter) -> tuple[str, str]:
    kind = "ocr" if job.kind == "ocr" else "hf"
    assessment = assess_model(job.dest, kind)
    if assessment.ok:
        reporter.say(f"      已存在且通过校验，跳过 {job.dest}")
        return "ready", "已存在"
    reporter.say(f"      正在读取文件清单：{listing_url(source, job.repo)}")
    payload = fetch_json(listing_url(source, job.repo))
    remote = parse_modelscope_files(payload) if source == "modelscope" else parse_hf_tree(payload)
    remote = with_urls(remote, source, job.repo)
    if not remote:
        raise SetupError(f"{job.repo} 的文件清单是空的。", "可以更换 --model-source 后重新运行。")
    # 校验记录放在 .local，不能放进模型目录，否则会改变视觉和向量指纹。
    store_path = root / ".local" / "setup" / "checksums" / (job.repo.replace("/", "__") + ".json")
    store = _read_json(store_path) or {}
    needed = files_needed(job.dest, remote, store if isinstance(store, dict) else {})
    reporter.say(f"      需要下载 {len(needed)} 个文件，已校验的会跳过。")
    for item in needed:
        reporter.say(f"      下载 {item.relative}")
        download(item.url or file_url(source, job.repo, item.relative), job.dest / item.relative, item.size, item.sha256)
        if isinstance(store, dict):
            store[item.relative] = {"size": (job.dest / item.relative).stat().st_size, "sha256": item.sha256}
    if isinstance(store, dict):
        _write_json(store_path, store)
    _cleanup_partials(job.dest)
    again = assess_model(job.dest, kind)
    if not again.ok:
        raise SetupError(
            f"{job.repo} 下载后仍不完整：{', '.join(again.missing) or again.detail}",
            "可以重新运行。已通过校验的文件会跳过；若清单来源不对，换 --model-source。",
        )
    return "ready", again.detail


def _database(args, root: Path, database: DatabasePlan, reporter: Reporter) -> tuple[DatabasePlan, str]:
    reporter.say("数据库")
    if database.action == "ask":
        database = _ask_database(args, reporter)
    if database.action == "docker":
        url = _ensure_docker(root, args.postgres_port, reporter)
        reporter.secret(url)
        reporter.add("数据库", "ready", f"项目专用容器 {POSTGRES_CONTAINER}，只监听 127.0.0.1，连接串已准备写入 .env")
        return DatabasePlan("docker", url, database.message), url
    if database.action == "write":
        reporter.secret(database.url)
        reporter.add("数据库", "ready", "使用你提供的专用库连接串，只写入 .env 的空缺项")
        return database, database.url
    if database.action == "keep":
        _resume_docker(root, reporter)
        reporter.add("数据库", "ready", "沿用 .env 中的连接串")
        return database, database.url
    reporter.add("数据库", "skipped", database.message)
    return database, ""


def _ask_database(args, reporter: Reporter) -> DatabasePlan:
    if args.non_interactive or not sys.stdin.isatty():
        raise SetupError(
            "还没有数据库连接串。",
            postgres_help_text() + "\n非交互运行请加上 --database-url、--postgres docker 或 --postgres skip。",
        )
    reporter.say(postgres_help_text())
    choice = _ask((
        ("existing", "我已有带 pgvector 的专用空库，现在输入连接串"),
        ("docker", "用 Docker 新建只给本项目用的 pgvector 数据库"),
        ("skip", "先跳过数据库"),
    ))
    if choice == "skip":
        return DatabasePlan("skip", "", "已跳过数据库。")
    if choice == "docker":
        return DatabasePlan("docker", "", "使用项目专用 Docker 数据库。")
    return DatabasePlan("write", _prompt_url(), "使用输入的专用库连接串。")


def _prompt_url() -> str:
    print("请输入专用库连接串。它只会写入 .env，不会再次打印。", flush=True)
    print("示例：postgresql://用户:密码@127.0.0.1:5432/专用库名", flush=True)
    last = ""
    for _ in range(3):
        try:
            entered = input("连接串: ").strip()
        except EOFError as exc:
            raise SetupError("没有读到连接串。", "请重新运行并加上 --database-url。") from exc
        try:
            return validate_database_url(entered)
        except SetupError as exc:
            last = str(exc)
            print(last, flush=True)
            if exc.hint:
                print(exc.hint, flush=True)
    raise SetupError(last or "连接串无效。", "请整理好专用库连接串后重新运行。")


def _ensure_docker(root: Path, port: int, reporter: Reporter) -> str:
    if shutil.which("docker") is None:
        raise SetupError(
            "没有找到 Docker，不能创建项目数据库。",
            "安装并启动 Docker Desktop 后重新运行，或用 --database-url 提供已经装好 pgvector 的专用库。" + "\n" + postgres_help_text(),
        )
    data = root / ".local" / "postgres"
    env_path = root / ".local" / "setup" / "postgres.env"
    nonempty = data.exists() and any(data.iterdir())
    kind = classify_volume(nonempty=nonempty, has_env_file=env_path.is_file())
    if kind == "refuse":
        raise SetupError(
            ".local/postgres 里已有文件，但没有本脚本保存的口令。",
            "脚本不会清空这个目录，也不会去连别的数据库。确认可以放弃其中的文件时，请自行移走该目录后再运行。",
        )
    if kind == "reuse":
        values = parse_pg_env(env_path.read_text(encoding="utf-8"))
        password = values["DDA_SETUP_PG_PASSWORD"]
        try:
            saved_port = int(values["DDA_SETUP_PG_PORT"])
        except ValueError as exc:
            raise SetupError("项目数据库口令文件里的端口无效。", "不会重置 .local/postgres。请核对 .local/setup/postgres.env。") from exc
        reporter.secret(password)
        reporter.say(f"      继续使用已有的项目数据库目录，端口 {saved_port}，不重置口令。")
        _compose_up(root, env_path, reporter)
        return build_local_database_url(password, saved_port)
    password = secrets.token_urlsafe(24)
    reporter.secret(password)
    data.mkdir(parents=True, exist_ok=True)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(render_pg_env(password, port, compose_volume(str(data.resolve()))), encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    reporter.say(f"      正在启动 {POSTGRES_CONTAINER}（镜像 {POSTGRES_IMAGE}，只绑定 127.0.0.1:{port}）。")
    _compose_up(root, env_path, reporter)
    return build_local_database_url(password, port)


def _resume_docker(root: Path, reporter: Reporter) -> None:
    env_path = root / ".local" / "setup" / "postgres.env"
    if not env_path.is_file() or shutil.which("docker") is None:
        return
    reporter.say("      发现本项目的数据库容器记录，尝试启动。不会改连接串，也不会清空数据。")
    try:
        _compose_up(root, env_path, reporter)
    except SetupError as exc:
        reporter.say("      容器没有启动：" + str(exc))
        reporter.say("      若 .env 指向这个容器，请先启动 Docker 后重新运行。已有数据不会被删除。")


def _compose_up(root: Path, env_path: Path, reporter: Reporter) -> None:
    _assert_container_owner(reporter)
    compose = root / ".local" / "setup" / "postgres-compose.yml"
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text(render_compose(), encoding="utf-8")
    _run(
        ("docker", "compose", "-f", str(compose), "--env-file", str(env_path), "up", "-d"),
        root, reporter, timeout=300,
    )
    for _ in range(60):
        completed = subprocess.run(
            ["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", POSTGRES_CONTAINER],
            capture_output=True, text=True, timeout=30, check=False,
        )
        status = (completed.stdout or "").strip()
        if status == "healthy":
            return
        time.sleep(2)
    raise SetupError(
        "数据库容器没有在预期时间内就绪。",
        f"可以查看 docker logs {POSTGRES_CONTAINER}。数据仍在 .local/postgres，重新运行不会清空它。",
    )


def _assert_container_owner(reporter: Reporter) -> None:
    completed = subprocess.run(
        ["docker", "inspect", POSTGRES_CONTAINER],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if completed.returncode != 0:
        return
    try:
        payload = json.loads(completed.stdout)
        labels = payload[0].get("Config", {}).get("Labels") or {}
    except (json.JSONDecodeError, IndexError, AttributeError) as exc:
        raise SetupError(
            f"无法确认已有容器 {POSTGRES_CONTAINER} 的来源。",
            "脚本不会改动它。请先处理同名容器后再运行。",
        ) from exc
    if labels.get("com.dd-agent.setup") != "1":
        raise SetupError(
            f"已有名为 {POSTGRES_CONTAINER} 的容器，但不是本安装脚本创建的。",
            "脚本不会启动或修改它。请先处理这个同名容器，再重新运行。",
        )
    reporter.say(f"      复用已有的 {POSTGRES_CONTAINER}。")


def _mcp_fill(args, current: dict, fills: dict, reporter: Reporter) -> str:
    if args.no_mcp_key:
        reporter.add("MCP 密钥", "skipped", "已按参数跳过，没有改 DDA_MCP_API_KEY")
        return ""
    if current.get("DDA_MCP_API_KEY", "").strip():
        reporter.add("MCP 密钥", "ready", "沿用 .env 中的已有值，未显示")
        return ""
    generated = secrets.token_urlsafe(32)
    reporter.secret(generated)
    fills["DDA_MCP_API_KEY"] = generated
    reporter.add("MCP 密钥", "ready", "已生成并写入 .env，未在终端显示")
    return "DDA_MCP_API_KEY 已写入 .env。终端不显示密钥。"


def _write_env(path: Path, existing: str | None, text: str, written: tuple[str, ...], reporter: Reporter) -> None:
    if existing is not None and existing == text:
        reporter.say(".env 没有缺失项，保持原样。")
        return
    temporary = path.with_name(".env.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)
    if written:
        reporter.say(".env 只补了这些空缺项：")
        for line in describe_writes(written):
            reporter.say("  " + line)
    else:
        reporter.say(".env 已按示例补齐缺失的键，没有改已有的非空值。")


def _finish_database(root: Path, database: DatabasePlan, windows: bool, reporter: Reporter) -> bool:
    if database.action == "skip":
        reporter.add("init-db", "skipped", "未运行")
        reporter.add("check", "skipped", "未运行")
        return False
    python = venv_python(root, "dev", windows)
    failed = False
    for command, label in (("init-db", "init-db"), ("check", "check")):
        reporter.say(f"运行 python -m dd_agent {command}")
        code, output = _app_command(python, root, command)
        shown = output.strip()
        if shown:
            reporter.say(shown)
        if code == 0:
            reporter.add(label, "ready", shown.splitlines()[-1] if shown else "")
            continue
        failed = True
        hint = "请检查连接串、账号权限，以及该库是否已安装 pgvector。"
        if "vector" in shown.lower() or "extension" in shown.lower():
            hint = "这个库还没有 pgvector。请换一个已安装扩展的专用库，或改用 --postgres docker。不要在业务库里试装扩展，除非那就是你准备好的专用库。"
        reporter.add(label, "failed", shown.splitlines()[-1] if shown else f"退出码 {code}")
        reporter.say("下一步：" + hint)
    return failed


def _app_command(python: Path, root: Path, command: str) -> tuple[int, str]:
    completed = subprocess.run(
        [str(python), "-m", "dd_agent", command],
        cwd=root, capture_output=True, text=True, timeout=300, check=False,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    return completed.returncode, output


def _launch_hint(root: Path, windows: bool, reporter: Reporter) -> None:
    python = venv_python(root, "dev", windows)
    reporter.say("")
    reporter.say("启动服务：")
    reporter.say(f"  {python} -m dd_agent")
    reporter.say("网页只监听 http://127.0.0.1:8765")
    reporter.say("测试另需 Node.js 22 或更新版本。安装脚本不安装 Node，也不改业务代码。")


def _ask(choices: tuple[tuple[str, str], ...]) -> str:
    for index, (_, label) in enumerate(choices, 1):
        print(f"  [{index}] {label}", flush=True)
    mapping = {str(index): key for index, (key, _) in enumerate(choices, 1)}
    for _ in range(3):
        try:
            entered = input("请选择序号: ").strip()
        except EOFError as exc:
            raise SetupError("没有读到选择。", "请用命令行参数重新运行，例如 --model-source hf-mirror 或 --postgres docker。") from exc
        if entered in mapping:
            return mapping[entered]
        print("请输入列表中的序号。", flush=True)
    raise SetupError("没有得到有效选择。", "请用对应的命令行参数重新运行。")


def _run(argv: tuple[str, ...], cwd: Path, reporter: Reporter, *, optional: bool = False, timeout: int | None = None) -> int:
    reporter.say("      " + " ".join(argv))
    try:
        completed = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise SetupError("命令超时：" + argv[0], "可以重新运行。已完成的步骤会跳过。") from exc
    except OSError as exc:
        raise SetupError(f"无法执行 {argv[0]}：{exc}", "请确认命令在 PATH 中，然后重新运行。") from exc
    if completed.returncode == 0:
        return 0
    tail = _tail((completed.stdout or "") + "\n" + (completed.stderr or ""))
    if tail:
        reporter.say(tail)
    if optional:
        reporter.say("      这一步没有成功，但可以继续。")
        return completed.returncode
    raise SetupError(
        f"命令失败，退出码 {completed.returncode}：{argv[0]}",
        "可以重新运行同一命令。已经装好的环境和已通过校验的模型会跳过。",
    )


def _tail(text: str, limit: int = 40) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _cleanup_partials(dest: Path) -> None:
    if not dest.exists():
        return
    for path in dest.rglob("*"):
        if path.is_file() and (path.name.endswith(".partial") or path.name.endswith(".incomplete")):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
