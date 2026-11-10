"""数据目录只读盘点、dry-run，以及日志按大小轮转。

不删除原件、恢复产物或业务缓存。真正删除属于暂缓的 l3／l4，这里没有回收执行入口。
日志单文件默认 8MB，另限保留份数和备份总量。错误摘要与普通输出分成两个文件。
"""
import json
import os
from pathlib import Path
import re
from threading import Thread


LOG_LIMIT = 8 * 1024 * 1024
LOG_COPIES = 3
LOG_TOTAL = LOG_LIMIT * LOG_COPIES

REASON_ORIGINAL = '原件，默认保护'
REASON_RECOVERY = '恢复产物，默认保护'
REASON_EVIDENCE = '有效证据，默认保护'
REASON_MANIFEST = '准备清单，默认保护'
REASON_TAIL = '尾段，默认保护'
REASON_RECEIPT = '收据，默认保护'
REASON_CHECKPOINT = '向量检查点，默认保护'
REASON_LEASE = '活动租约未释放'
REASON_UNREGISTERED = '未登记，无法证明无引用；修改时间不能作为删除依据'
REASON_NOT_REBUILDABLE = '不可重建'
REASON_ESCAPE = '路径越出登记根，拒绝回收'
REASON_RECLAIMABLE = '可重建、无引用、无活动租约；本次只列出，不删除'
REASON_SYMLINK = '符号链接不跟随，无法证明目标无引用'
REASON_MISSING = '登记项的文件已不在目录中；本次不删除其他文件'

_PROTECTED_PREFIXES = (
    ('documents/', 'original', REASON_ORIGINAL),
    ('recovery/', 'recovery', REASON_RECOVERY),
    ('checkpoints/', 'vector_checkpoint', REASON_CHECKPOINT),
    ('evidence/', 'evidence', REASON_EVIDENCE),
    ('manifests/', 'manifest', REASON_MANIFEST),
    ('tails/', 'tail', REASON_TAIL),
    ('receipts/', 'receipt', REASON_RECEIPT),
)
_ROLE_REASONS = {role: reason for _prefix, role, reason in _PROTECTED_PREFIXES}

_DSN = re.compile(r'(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s\'"]+')
_SECRET = re.compile(
    r'(?i)\b(?:api[_-]?key|secret|access[_-]?key|token|password|passwd|pwd)\b\s*[=:]\s*[^\s,;]+')
_BEARER = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}')
_SK = re.compile(r'\bsk-[A-Za-z0-9_\-]{8,}\b')


def redact(text):
    """去掉连接串、密钥和令牌。不尝试改写业务正文的含义，错误日志根本不接收正文。"""
    value = _DSN.sub('[已脱敏]', text)
    value = _SECRET.sub('[已脱敏]', value)
    value = _BEARER.sub('[已脱敏]', value)
    return _SK.sub('[已脱敏]', value)


def _redact_bytes(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return data
    return redact(text).encode('utf-8')


def _backup_paths(path, copies):
    found = []
    for index in range(1, copies + 1):
        candidate = Path(str(path) + f'.{index}')
        if candidate.is_file():
            found.append(candidate)
    return found


def _trim_backups(path, copies, total_limit):
    found = _backup_paths(path, copies)
    while found and (len(found) > copies or sum(item.stat().st_size for item in found) > total_limit):
        found[-1].unlink()
        found.pop()


def rotate_log(path, limit=LOG_LIMIT, copies=LOG_COPIES, total_limit=LOG_TOTAL):
    """超过单文件上限时保留有限份备份，并限制备份总字节。失败时保留原文件。"""
    path = Path(path)
    try:
        if copies < 1 or total_limit < 1:
            return False
        if not path.is_file() or path.stat().st_size < limit:
            return False
        data = path.read_bytes()
        redacted = _redact_bytes(data)
        oldest = Path(str(path) + f'.{int(copies)}')
        if oldest.exists():
            oldest.unlink()
        for index in range(int(copies) - 1, 0, -1):
            source = Path(str(path) + f'.{index}')
            target = Path(str(path) + f'.{index + 1}')
            if source.exists():
                os.replace(source, target)
        backup = Path(str(path) + '.1')
        if redacted == data:
            os.replace(path, backup)
        else:
            temporary = Path(str(backup) + '.partial')
            temporary.write_bytes(redacted)
            os.replace(temporary, backup)
            path.unlink()
        _trim_backups(path, int(copies), total_limit)
        return True
    except OSError:
        return False


def append_output(path, text, *, limit=LOG_LIMIT, copies=LOG_COPIES, total_limit=LOG_TOTAL):
    """普通输出。写入前脱敏，超限则轮转。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(path, limit, copies, total_limit)
    line = redact(str(text))
    if line and not line.endswith('\n'):
        line += '\n'
    with path.open('a', encoding='utf-8') as stream:
        stream.write(line)


def append_error(path, *, category, location, request_id, limit=LOG_LIMIT, copies=LOG_COPIES, total_limit=LOG_TOTAL):
    """错误日志只记类别、脱敏位置和请求号。不接收正文参数。"""
    if not isinstance(category, str) or not category or len(category) > 80 or any(ch.isspace() for ch in category):
        raise ValueError('错误类别无效。')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(path, limit, copies, total_limit)
    record = {
        'category': category,
        'location': redact(str(location))[:200],
        'request_id': redact(str(request_id))[:80],
    }
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def split_log_paths(directory, stem):
    root = Path(directory)
    return {'output': root / f'{stem}.log', 'error': root / f'{stem}.error.log'}


class LogPump:
    """把子进程 stderr 写入普通日志。错误摘要由调用方另写，不混进这个文件。"""

    def __init__(self, stream, path):
        self.stream = stream
        self.path = Path(path)
        self.thread = Thread(target=self._run, name='log-pump', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while True:
                line = self.stream.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode('utf-8', errors='replace')
                try:
                    append_output(self.path, line)
                except OSError:
                    continue
        except Exception:
            return

    def close(self):
        try:
            self.stream.close()
        except OSError:
            pass
        if self.thread.ident is not None:
            self.thread.join(timeout=5)


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


def _safe_relative(value):
    raw = str(value or '').replace('\\', '/').strip()
    if not raw or raw.startswith('/') or re.match(r'^[A-Za-z]:', raw):
        return None
    parts = Path(raw).parts
    if any(part in {'..', ''} for part in parts):
        return None
    return Path(*parts).as_posix()


def _prefix_role(relative):
    name = relative if relative.endswith('/') else relative + '/'
    for prefix, role, reason in _PROTECTED_PREFIXES:
        if name == prefix or name.startswith(prefix) or relative == prefix.rstrip('/'):
            return role, reason
    return None, None


def _walk(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept = []
        for name in list(dirnames):
            path = current / name
            if path.is_symlink():
                found.append(path)
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            found.append(current / name)
    return found


def _referenced_reason(referrers):
    shown = '、'.join(str(item) for item in referrers[:8])
    return f'仍被引用：{shown}'


def _classify(relative, record, path):
    del path
    safe = _safe_relative(relative)
    if safe is None:
        return 'retain', REASON_ESCAPE, 'unknown', False
    role, reason = _prefix_role(safe)
    if record and record.get('protected'):
        role = record.get('role') or role or 'protected'
        return 'retain', record.get('reason') or _ROLE_REASONS.get(role, REASON_RECOVERY), role, False
    if role:
        return 'retain', reason, role, False
    referrers = list(record.get('referrers') or []) if record else []
    lease_active = bool(record.get('lease_active')) if record else False
    rebuildable = bool(record.get('rebuildable')) if record else False
    if record is None:
        return 'retain', REASON_UNREGISTERED, 'unknown', False
    if lease_active:
        return 'retain', REASON_LEASE, record.get('role') or 'cache', False
    if referrers:
        return 'retain', _referenced_reason(referrers), record.get('role') or 'cache', False
    if not rebuildable:
        return 'retain', REASON_NOT_REBUILDABLE, record.get('role') or 'cache', False
    return 'reclaimable', REASON_RECLAIMABLE, record.get('role') or 'cache', True


def _entry(root, path, relative, record):
    if path is not None and path.is_symlink():
        try:
            resolved = path.resolve()
            escapes = not resolved.is_relative_to(root.resolve())
        except OSError:
            escapes = True
        reason = REASON_ESCAPE if escapes else REASON_SYMLINK
        return {
            'relative_path': relative,
            'bytes': 0,
            'version': '',
            'referrers': [],
            'lease_active': False,
            'last_used': '',
            'rebuildable': False,
            'decision': 'retain',
            'reason': reason,
            'role': 'symlink',
        }
    decision, reason, role, rebuildable = _classify(relative, record, path)
    nbytes = 0
    if path is not None and path.is_file() and not path.is_symlink():
        try:
            nbytes = path.stat().st_size
        except OSError:
            nbytes = 0
    stored_bytes = record.get('bytes') if record and record.get('bytes') is not None else nbytes
    return {
        'relative_path': relative,
        'bytes': stored_bytes if path is None else nbytes,
        'version': (record or {}).get('version') or '',
        'referrers': list((record or {}).get('referrers') or []),
        'lease_active': bool((record or {}).get('lease_active')),
        'last_used': (record or {}).get('last_used') or '',
        'rebuildable': bool((record or {}).get('rebuildable')) if record else rebuildable,
        'decision': decision,
        'reason': reason,
        'role': (record or {}).get('role') or role,
    }


def cache_inventory(data_dir, registry=None):
    """逐项列出缓存占用和保留／可回收原因。只读。"""
    root = Path(data_dir)
    by_path = {}
    escaped = []
    for item in list(registry or []):
        relative = _safe_relative(item.get('relative_path') or item.get('path'))
        if relative is None:
            escaped.append(_entry(root, None, str(item.get('relative_path') or item.get('path') or ''), item))
            escaped[-1]['decision'] = 'retain'
            escaped[-1]['reason'] = REASON_ESCAPE
            continue
        by_path[relative] = item
    items = []
    if root.is_dir():
        for path in _walk(root):
            relative = path.relative_to(root).as_posix()
            items.append(_entry(root, path, relative, by_path.get(relative)))
    seen = {item['relative_path'] for item in items}
    for relative, record in by_path.items():
        if relative not in seen:
            missing = _entry(root, None, relative, record)
            missing['bytes'] = 0
            if missing['decision'] == 'reclaimable':
                missing['decision'] = 'retain'
                missing['reason'] = REASON_MISSING
            items.append(missing)
    items.extend(escaped)
    items.sort(key=lambda item: item['relative_path'])
    return {
        'root': str(root),
        'items': items,
        'total_bytes': sum(item['bytes'] for item in items),
        'deleted': [],
    }


def dry_run(data_dir, registry=None):
    """与清单相同，并明确本次删除列表为空。不调用删除。"""
    report = cache_inventory(data_dir, registry)
    report['mode'] = 'dry-run'
    report['deleted'] = []
    return report
