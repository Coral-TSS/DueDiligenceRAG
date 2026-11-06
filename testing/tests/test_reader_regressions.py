"""Execute the production reader with controlled DOM/network responses, without models."""
from pathlib import Path
import subprocess

import pytest

from node_bin import node_executable


@pytest.mark.parametrize('case', ['content_update', 'other_page_update', 'raw_text', 'failed_page',
    'bounded_history', 'serial_poll', 'late_page', 'late_catalog'])
def test_reader_behavior(case):
    root = Path(__file__).resolve().parents[2]
    node = node_executable()
    result = subprocess.run([str(node), str(root / 'testing/scripts/frontend/reader-regression.cjs'), case],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
