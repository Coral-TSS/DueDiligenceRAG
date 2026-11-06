"""Node.js for frontend regression scripts. The application does not ship or invoke Node."""
import os
import shutil
from pathlib import Path


def node_executable():
    chosen = os.environ.get('DDA_TEST_NODE', '').strip()
    if chosen:
        path = Path(chosen)
        if not path.is_file():
            raise AssertionError(f'DDA_TEST_NODE 不是可执行文件：{path}')
        return path
    found = shutil.which('node')
    if not found:
        raise AssertionError('前端回归需要 Node.js 22+。设置 DDA_TEST_NODE，或将 node 加入 PATH。')
    return Path(found)
