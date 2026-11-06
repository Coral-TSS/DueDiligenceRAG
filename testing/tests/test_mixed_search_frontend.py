from pathlib import Path
import subprocess

from node_bin import node_executable


def test_parent_context_is_revision_checked_and_late_reads_are_ignored():
    root=Path(__file__).resolve().parents[2]
    node=node_executable()
    result=subprocess.run([str(node),str(root/'testing/scripts/frontend/mixed-search-regression.cjs')],
        capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
