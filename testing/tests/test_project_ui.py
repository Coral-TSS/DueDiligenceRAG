"""当前项目由地址决定。断点是：无项目时仍去读最新项目，或迟到响应画进新项目。"""
from pathlib import Path
import subprocess

from node_bin import node_executable


def _run(script):
    root = Path(__file__).resolve().parents[2]
    node = node_executable()
    return subprocess.run(
        [str(node), str(root / 'testing/scripts/frontend' / script)],
        capture_output=True, text=True, encoding='utf-8', timeout=15)


def test_workspace_keeps_the_url_project_and_drops_late_materials():
    result = _run('project-current-regression.cjs')
    assert result.returncode == 0, result.stdout + result.stderr


def test_search_does_not_default_to_the_newest_project_and_drops_late_hits():
    result = _run('search-project-regression.cjs')
    assert result.returncode == 0, result.stdout + result.stderr


def test_coverage_panel_stays_on_the_current_project():
    result = _run('coverage-gaps-regression.cjs')
    assert result.returncode == 0, result.stdout + result.stderr
