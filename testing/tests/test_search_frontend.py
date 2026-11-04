from pathlib import Path
import subprocess


def test_cancel_aborts_request_and_ignores_late_results():
    root=Path(__file__).resolve().parents[2]
    node=root/'.local/harness/runtime/node-v22.19.0-win-x64/node.exe'
    result=subprocess.run([str(node),str(root/'testing/scripts/frontend/search-regression.cjs')],capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
