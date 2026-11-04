"""Run the isolated RG-05a baseline; --models also exercises OCR/VLM/Qwen offline."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--models', action='store_true', help='使用本机真实 OCR、视觉、Embedding 和重排模型')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    environment = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}
    if args.models: environment['DDA_TEST_PERFORMANCE_MODELS'] = '1'
    return subprocess.call([sys.executable, str(root/'testing/run.py'),
                            'testing/tests/test_performance_baseline.py', '-s', '-q'], cwd=root, env=environment)


if __name__ == '__main__':
    raise SystemExit(main())
