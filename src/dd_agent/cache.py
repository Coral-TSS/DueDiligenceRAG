"""模型指纹与原件预检的有界缓存（RG-05b／K01）。

契约要点：
- 键 = 种类 + 运行时描述符 + 算法标记 + 全部参数 + 文件清单（路径／大小／修改时间／首尾抽样摘要）。
- 快检（validation_mode='metadata'）只用于**曾经完整校验过**的受控版本：
  缓存条目只能由完整复验（'full'）写入，命中时不再读取权重内容。
- 元数据与首尾抽样都相同却原地改写内容的情况无法可靠识别；
  因此提供严格复验入口 verify=True，它必须完整重算，不被缓存短路。
  **不得**声称元数据／抽样检查等价于内容哈希。
- 缓存只保存结果与清单，不复制原文正文；损坏／缺失／版本不符一律退回复算；
  磁盘写入失败不影响业务结果。内存只是有界加速，不作为恢复依据。
"""
import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .performance import count


CACHE_VERSION = 1
SAMPLE_BYTES = 65536          # 每个文件只读首尾各 64 KiB 做抽样，不读整个权重。
MEMORY_LIMIT = 64             # 内存加速上限；超出丢弃最久未用。

_guard = Lock()
_key_locks = {}
_memory = OrderedDict()


def cache_directory(settings):
    """缓存根目录默认跟随数据目录，可用 DDA_CACHE_DIR 单独指定。"""
    configured = getattr(settings, 'cache_dir', None)
    return Path(configured) if configured else Path(settings.data_dir) / 'cache'


def _sample(path, size, limit=SAMPLE_BYTES):
    # 首尾抽样：检测内容与元数据变化，成本远低于全量读取。
    digest = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            digest.update(stream.read(limit))
            if size > limit:
                stream.seek(max(0, size - limit))
                digest.update(stream.read(limit))
    except OSError:
        return 'unreadable'
    return digest.hexdigest()


def file_entries(paths):
    """文件清单：路径／大小／修改时间／抽样摘要；缺失文件也计入，避免静默漏检。"""
    entries = []
    for path in paths:
        target = Path(path)
        try:
            stat = target.stat()
        except OSError:
            entries.append({'path': str(target), 'missing': True})
            continue
        entries.append({'path': str(target), 'size': stat.st_size,
                        'mtime_ns': stat.st_mtime_ns, 'sample': _sample(target, stat.st_size)})
    return entries


def _key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _read(path, key):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('cache_version') != CACHE_VERSION:
        return None
    if data.get('key') != key or data.get('validation') != 'full' or 'value' not in data:
        return None
    return data


def _write(path, payload):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp-' + uuid4().hex)
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        # 缓存写失败不能影响业务结果。
        count('cache.write_failed')


def _memory_get(key):
    with _guard:
        value = _memory.get(key)
        if value is not None:
            _memory.move_to_end(key)
        return value


def _memory_put(key, value):
    with _guard:
        _memory[key] = value
        _memory.move_to_end(key)
        while len(_memory) > MEMORY_LIMIT:
            _memory.popitem(last=False)


def _key_lock(key):
    # 单键锁：并发未命中时只有第一个线程重算，其余获锁后重查共享结果。
    with _guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = Lock()
        return lock


def _resolve(directory, subdir, kind, identity, compute, verify, runtime_descriptor=None):
    if directory is None:
        raise ValueError('缓存目录未指定，无法复用指纹／预检结果。')
    if runtime_descriptor is None:
        # 无法绑定解释器／依赖版本时不作可信复用，直接完整校验。
        verify = True
    key = _key({'kind': kind, **identity})
    manifest_revision = _key({'entries': identity.get('entries')})
    path = Path(directory) / subdir / kind / (key[:32] + '.json')

    def hit(entry):
        count('cache.hit.' + kind)
        return dict(value=entry['value'], cache_hit=True, validation_mode='metadata',
                    manifest_revision=entry.get('manifest_revision', manifest_revision), key=key)

    if not verify:
        entry = _memory_get(key)
        if entry is None:
            entry = _read(path, key)
        if entry is not None:
            return hit(entry)
    count('cache.miss.' + kind)
    with _key_lock(key):
        if not verify:
            entry = _read(path, key)      # 获锁后重查，避免并发重复计算
            if entry is not None:
                _memory_put(key, entry)
                return hit(entry)
        value = compute()
        payload = dict(cache_version=CACHE_VERSION, kind=kind, key=key, validation='full',
                       manifest_revision=manifest_revision, value=value)
        _write(path, payload)
        _memory_put(key, payload)
        return dict(value=value, cache_hit=False, validation_mode='full',
                    manifest_revision=manifest_revision, key=key)


def resolve_fingerprint(kind, runtime_descriptor, algorithm_tag, parameters, entries, compute,
                        verify=False, directory=None):
    """按种类／算法／参数／文件清单复用指纹；verify=True 强制完整复验。"""
    identity = dict(runtime=str(runtime_descriptor), algorithm=str(algorithm_tag),
                    parameters=parameters, entries=entries)
    return _resolve(directory, 'fingerprints', kind, identity, compute, verify, runtime_descriptor)


def inspect_document(identity, sha256, parser_version, source, compute, verify=False, directory=None):
    """预检缓存：键基于材料身份、原件摘要与解析版本；缓存不复制正文。"""
    if not identity or not sha256:
        # 缺少项目内材料身份与内容摘要时不做可信复用（CLI 诊断路径）。
        return dict(value=compute(), cache_hit=False, validation_mode='full',
                    manifest_revision='', key='')
    payload = dict(identity=str(identity), sha256=str(sha256), parser=str(parser_version),
                   entries=file_entries([source]))
    return _resolve(directory, 'preflight', 'preflight', payload, compute, verify, parser_version)


def _site_packages(interpreter):
    # Windows venv 把解释器放在 Scripts／根目录，POSIX 用 lib/pythonX.Y；两种都要识别。
    base = interpreter.parent.parent
    for candidate in (base / 'Lib' / 'site-packages', interpreter.parent / 'Lib' / 'site-packages'):
        if candidate.is_dir():
            return candidate
    for candidate in sorted(base.glob('lib/python*/site-packages')):
        if candidate.is_dir():
            return candidate
    return None


def venv_runtime_descriptor(python, packages=('torch', 'transformers', 'tokenizers')):
    """把缓存键绑定到 worker 环境的依赖版本；无法确定时返回 None（禁用复用）。

    描述符＝依赖 dist-info 名称 + 虚拟环境 pyvenv.cfg 摘要（含基础解释器版本）。
    """
    interpreter = Path(python)
    if not interpreter.is_file():
        return None
    site = _site_packages(interpreter.resolve())
    if site is None:
        return None
    wanted = {name.lower() for name in packages}
    try:
        names = sorted(p.name for p in site.glob('*.dist-info') if p.name.split('-')[0].lower() in wanted)
    except OSError:
        return None
    if not names:
        return None
    config = interpreter.resolve().parent.parent / 'pyvenv.cfg'
    try:
        marker = hashlib.sha256(config.read_bytes()).hexdigest()[:16] if config.is_file() else 'nocfg'
    except OSError:
        marker = 'nocfg'
    return '|'.join(names) + '#' + marker
