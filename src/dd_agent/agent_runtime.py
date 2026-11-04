"""Thin process boundary for the pinned Harness; no custom agent loop."""
# 当前唯一能力是 harness-check：验证项目内 SDK／CLI 的版本与启动退出。
# 不含尽调循环、业务 RAG MCP 注册、Word 工具或报告生成；ready 不能解释为报告可用。
# 用户桌面安装 D:\Tools\Deepseek Harness 与这里的项目隔离环境不是同一个部署。
from pathlib import Path

from .config import Settings
from .reading import ReadingError, worker_records


class HarnessError(Exception):
    pass


def check_harness(settings=None):
    settings=settings or Settings.from_env()
    patch=Path(__file__).parent/'agent_profiles/dd-report.patch.yml'
    manifest=dict(home=str(settings.harness_home.resolve()),workspace=str(settings.harness_workspace.resolve()),
                  patch=str(patch.resolve()),dsh_bin=str(settings.harness_dsh_bin.resolve()),
                  node=str(settings.harness_node.resolve()))
    try:
        # 复用通用工作进程边界，让 SDK 的依赖与主应用／OCR 环境分开。
        records=list(worker_records(settings.harness_python,'harness_worker',manifest,
            settings.data_dir/'harness-diagnostics',timeout_seconds=60))
    except (ReadingError,OSError):
        raise HarnessError('Harness 运行环境不可用；请按 README 安装 .venv-harness，并检查 DDA_HARNESS_PYTHON。') from None
    if len(records)!=1 or records[0].get('status')!='ready':
        code=records[0].get('error_code','invalid_result') if records else 'no_result'
        raise HarnessError(f'Harness 基线检查未通过（{code}）；请核对固定版本、profile 和本地诊断日志。')
    return records[0]
