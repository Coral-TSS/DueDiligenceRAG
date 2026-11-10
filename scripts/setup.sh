#!/usr/bin/env bash
# Linux / macOS 入口。只寻找已经装好的 Python 3.12，不调用 apt 或 brew。
# Windows 请用 scripts/setup.ps1。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE="$ROOT/scripts/local_setup/__main__.py"

if [[ ! -f "$PACKAGE" ]]; then
  echo "找不到安装脚本：$PACKAGE" >&2
  exit 1
fi

python_is_312() {
  [[ -n "${1:-}" ]] && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2]==(3, 12) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidate found
  for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_312 "$candidate"; then
      "$candidate" -c 'import sys; print(sys.executable)'
      return 0
    fi
  done
  if command -v uv >/dev/null 2>&1; then
    found="$(uv python find 3.12 2>/dev/null || true)"
    if python_is_312 "$found"; then
      printf '%s\n' "$found"
      return 0
    fi
  fi
  return 1
}

if ! PYTHON_BIN="$(find_python)"; then
  cat >&2 <<'EOF'
未找到 Python 3.12。Linux / macOS 请先自行安装 3.12（不能是 3.13），然后重新运行。
本脚本不会用 apt 或 brew 安装系统包。
Windows 请在仓库根目录运行：
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
EOF
  exit 1
fi

echo "使用 Python 3.12：$PYTHON_BIN"

gpu_args=()
if command -v nvidia-smi >/dev/null 2>&1; then
  line="$(nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader 2>/dev/null | head -n 1 || true)"
  if [[ -n "$line" ]]; then
    name="${line%%,*}"
    cap="${line#*,}"
    name="${name#"${name%%[![:space:]]*}"}"
    name="${name%"${name##*[![:space:]]}"}"
    cap="${cap#"${cap%%[![:space:]]*}"}"
    cap="${cap%"${cap##*[![:space:]]}"}"
    gpu_args+=(--gpu-name "$name" --compute-capability "$cap")
  fi
fi

export PYTHONUTF8=1
export PYTHONPATH="$ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -m local_setup "${gpu_args[@]}" "$@"
