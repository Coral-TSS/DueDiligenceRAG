"""Run tests with all generated files and diagnostics below testing/."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description='统一运行并记录测试')
    parser.add_argument('--models', action='store_true', help='同时运行真实本地视觉及检索模型测试')
    args, pytest_args = parser.parse_known_args()
    base = Path(__file__).resolve().parent
    root = base.parent
    run_id = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    output = base / 'runs' / run_id
    output.mkdir(parents=True)
    environment = {**os.environ, 'DDA_TEST_RUN_DIR': str(output), 'PYTHONUTF8':'1',
                   'PYTHONIOENCODING':'utf-8', 'PYTHONUNBUFFERED':'1', 'PYTHONPYCACHEPREFIX':str(base/'cache/bytecode')}
    if args.models:
        environment.update(DDA_TEST_LOCAL_MODELS='1', DDA_TEST_VISION_MODEL='1')
    command = [sys.executable, '-m', 'pytest', '--basetemp='+str(output/'tmp'),
               '--junitxml='+str(output/'results.xml'), *pytest_args]
    with (output/'console.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=root, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding='utf-8')
        try:
            for line in process.stdout:
                print(line, end='', flush=True); log.write(line); log.flush()
            code = process.wait()
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate(); process.wait()
    result = {'run_id':run_id, 'exit_code':code, 'models_requested':args.models}
    if (output/'results.xml').exists():
        suites = ET.parse(output/'results.xml').getroot().iter('testsuite')
        totals = dict(tests=0, failures=0, errors=0, skipped=0)
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.get(key, '0'))
        result.update(totals)
    (output/'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (base/'records/latest-run.md').write_text(
        f'# 最近测试运行\n\n运行：{run_id}\n\n退出码：{code}\n\n'
        f'[运行结果](../runs/{run_id}/summary.json) · [完整日志](../runs/{run_id}/console.log)\n', encoding='utf-8')
    print(f'测试产物：{output}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
