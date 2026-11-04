"""Execute the production reader with controlled DOM/network responses, without models."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('case', ['content_update', 'other_page_update', 'raw_text', 'failed_page',
    'bounded_history', 'serial_poll', 'late_page', 'late_catalog'])
def test_reader_behavior(case):
    root = Path(__file__).resolve().parents[2]
    node = Path(os.environ.get('DDA_TEST_NODE', root / '.local/harness/runtime/node-v22.19.0-win-x64/node.exe'))
    assert node.is_file(), 'Set DDA_TEST_NODE to Node.js 22+ for frontend behavioral regressions'
    result = subprocess.run([str(node), str(root / 'testing/scripts/frontend/reader-regression.cjs'), case],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
