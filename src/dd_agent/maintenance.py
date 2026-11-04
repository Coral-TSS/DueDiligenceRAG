"""数据目录只读盘点，以及日志按大小保留一份备份。不删除原件或恢复成果。"""
import os
from pathlib import Path


LOG_LIMIT = 8 * 1024 * 1024


def rotate_log(path, limit=LOG_LIMIT):
    """超过上限时把当前文件换成唯一的 .1 备份。失败时保持原文件，调用方仍可继续追加。"""
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size < limit:
            return False
        backup = Path(str(path) + '.1')
        if backup.exists():
            backup.unlink()
        os.replace(path, backup)
        return True
    except OSError:
        return False


def _bytes(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        children = list(path.iterdir())
    except OSError:
        return 0
    for child in children:
        total += _bytes(child)
    return total


def inventory(data_dir):
    """按数据目录的第一层列出占用。只读，不删除。"""
    root = Path(data_dir)
    entries = []
    if root.is_dir():
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError:
            children = []
        for child in children:
            entries.append({'name': child.name, 'bytes': _bytes(child),
                            'kind': 'dir' if child.is_dir() and not child.is_symlink() else 'file'})
    return {'root': str(root), 'entries': entries, 'total_bytes': sum(item['bytes'] for item in entries)}
