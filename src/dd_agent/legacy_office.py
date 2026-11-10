"""旧版 .doc／.xls／.ppt：本机 LibreOffice 转为现代格式后，交给已有解析。

转换只在临时目录进行。原件只读，不覆盖。每次使用独立的用户配置目录，并禁止执行宏。
找不到 soffice、含宏、有密码、损坏、转换失败或超时，都给出明确原因，不重试。
"""
import hashlib
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import threading
import re

_KIND = {'.doc': '旧版 Word', '.xls': '旧版 Excel', '.ppt': '旧版 PowerPoint'}
_TARGET = {'.doc': '.docx', '.xls': '.xlsx', '.ppt': '.pptx'}
_STREAM = {'.doc': 'worddocument', '.xls': 'workbook', '.ppt': 'powerpoint document'}
_MACRO_NAMES = {
    'vba', '_vba_project_cur', '_vba_project', 'vbaproject',
    '__srp_0', '__srp_1', '__srp_2', '__srp_3',
}
_PASSWORD_NAMES = {'encryptedpackage', 'encryptioninfo', 'encryptedsummary'}
_MISSING_MARK = '未找到 LibreOffice 的 soffice'
_OLE_MAGIC = bytes.fromhex('D0CF11E0A1B11AE1')
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD
_SPECIAL = 0xFFFFFFFA
_VERSION_RE = re.compile(r'LibreOffice\s+[0-9]+(?:\.[0-9]+){1,4}')
_GATES = {}
_GATE_LOCK = threading.Lock()
_VERSIONS = {}
_VERSION_LOCK = threading.Lock()
_MACRO_XCU = '''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="MacroSecurity" oor:op="fuse"><value>3</value></prop>
 </item>
</oor:items>
'''


class _OleError(Exception):
    pass


def _u16(blob, offset):
    return int.from_bytes(blob[offset:offset + 2], 'little')


def _u32(blob, offset):
    return int.from_bytes(blob[offset:offset + 4], 'little')


def _fail(ext, status, reason, error_code, **extra):
    result = {
        'ok': False, 'status': status, 'reason': reason, 'error_code': error_code,
        'conversion': {
            'source_extension': ext, 'target_extension': _TARGET[ext], 'tool': 'soffice',
            'converter': extra.get('converter', ''), 'outcome': error_code,
        },
    }
    return result


def _kind(ext):
    return _KIND[ext]


def missing_soffice_reason(ext, state):
    kind = _kind(ext)
    if state == 'configured_missing':
        return (
            f'{_MISSING_MARK}：DDA_SOFFICE_PATH 不是可用的程序。'
            f'{kind} 未转换，也未解析正文。'
        )
    return (
        f'{_MISSING_MARK}。请安装 LibreOffice，或设置 DDA_SOFFICE_PATH。'
        f'未配置时会查找 Windows 默认安装路径。{kind} 未解析正文。'
    )


def _pending_reason(ext):
    return f'已登记。{_kind(ext)} 将用本机 LibreOffice 转为 {_TARGET[ext][1:]} 后再解析，尚未转换。'


def _success_reason(ext, converter):
    return (
        f'已用 {converter} 将 {_kind(ext)}（{ext}）转为 {_TARGET[ext][1:]} 后解析。'
        '证据仍指向原件，转换中间文件已删除。'
    )


def _windows_candidates():
    roots = [
        Path(r'C:\Program Files\LibreOffice\program'),
        Path(r'C:\Program Files (x86)\LibreOffice\program'),
    ]
    local = os.environ.get('LOCALAPPDATA', '').strip()
    if local:
        roots.append(Path(local) / 'Programs' / 'LibreOffice' / 'program')
    return [root / name for root in roots for name in ('soffice.com', 'soffice.exe')]


def _as_binary(path):
    path = Path(path)
    if path.is_file():
        return path
    if path.is_dir():
        for name in ('soffice.com', 'soffice.exe', 'soffice'):
            candidate = path / name
            if candidate.is_file():
                return candidate
    return None


def _first_existing(paths):
    for path in paths:
        if path.is_file():
            return path
    return None


def soffice_status(settings=None):
    """返回 (程序路径或 None, 'ok'／'missing'／'configured_missing')。不启动进程。

    显式配置了路径时，不会再猜 Windows 默认位置。未配置时只在 Windows 上查找默认安装路径。
    """
    if settings is not None and settings.soffice_path is not None:
        found = _as_binary(settings.soffice_path)
        return found, 'ok' if found is not None else 'configured_missing'
    if settings is None:
        raw = os.getenv('DDA_SOFFICE_PATH', '').strip()
        if raw:
            found = _as_binary(Path(raw).expanduser())
            return found, 'ok' if found is not None else 'configured_missing'
    if os.name == 'nt':
        found = _first_existing(_windows_candidates())
        return found, 'ok' if found is not None else 'missing'
    return None, 'missing'


def registration_decision(path, ext, status_provider=None):
    """登记阶段不启动 soffice。结构有问题，或当前找不到程序时，直接给出原因。"""
    if Path(path).stat().st_size == 0:
        return None
    problem = inspect_legacy(path, ext)
    if problem is not None:
        return problem['status'], problem['reason']
    binary, state = (status_provider or (lambda: soffice_status(None)))()
    if binary is None:
        return 'unsupported', missing_soffice_reason(ext, state)
    return 'registered', _pending_reason(ext)


def _sector_size(blob):
    if len(blob) < 512 or blob[:8] != _OLE_MAGIC:
        raise _OleError('signature')
    if _u16(blob, 0x1C) != 0xFFFE:
        raise _OleError('byte-order')
    major = _u16(blob, 0x1A)
    shift = _u16(blob, 0x1E)
    if major not in (3, 4) or shift not in (9, 12):
        raise _OleError('sector')
    size = 1 << shift
    if len(blob) < size * 2:
        raise _OleError('short')
    return size, major


def _read_sector(blob, sector, sector_size):
    if sector < 0 or sector >= _SPECIAL:
        raise _OleError('sector')
    start = (sector + 1) * sector_size
    end = start + sector_size
    if end > len(blob):
        raise _OleError('truncated')
    return blob[start:end]


def _chain(fat, start, limit):
    sector = start
    seen = set()
    while sector < _SPECIAL:
        if sector in seen or len(seen) > limit or sector < 0 or sector >= len(fat):
            raise _OleError('chain')
        seen.add(sector)
        yield sector
        sector = fat[sector]


def _load_fat(blob, sector_size):
    count = _u32(blob, 0x2C)
    per = sector_size // 4
    if count <= 0 or count > 4096:
        raise _OleError('fat')
    difat = []
    for index in range(109):
        value = _u32(blob, 0x4C + index * 4)
        if value >= _SPECIAL:
            break
        difat.append(value)
        if len(difat) == count:
            break
    next_sector = _u32(blob, 0x44)
    hops = 0
    difat_count = _u32(blob, 0x48)
    while len(difat) < count and next_sector < _SPECIAL:
        hops += 1
        if hops > difat_count + 1 or hops > 4096:
            raise _OleError('difat')
        data = _read_sector(blob, next_sector, sector_size)
        for index in range(per - 1):
            value = _u32(data, index * 4)
            if value >= _SPECIAL:
                break
            difat.append(value)
            if len(difat) == count:
                break
        next_sector = _u32(data, (per - 1) * 4)
    if len(difat) < count:
        raise _OleError('fat')
    fat = []
    for sector in difat[:count]:
        data = _read_sector(blob, sector, sector_size)
        fat.extend(_u32(data, index) for index in range(0, sector_size, 4))
    return fat


def _entry_name(entry):
    kind = entry[66]
    if kind not in (1, 2, 5):
        return None
    length = _u16(entry, 64)
    if length < 2 or length > 64 or length % 2:
        return None
    try:
        return entry[:length - 2].decode('utf-16le')
    except UnicodeDecodeError:
        return None


def _directory(blob, sector_size, fat, major):
    entries = []
    for sector in _chain(fat, _u32(blob, 0x30), 4096):
        data = _read_sector(blob, sector, sector_size)
        for offset in range(0, sector_size, 128):
            raw = data[offset:offset + 128]
            name = _entry_name(raw)
            if name is None:
                continue
            size = int.from_bytes(raw[120:128], 'little')
            if major == 3:
                size &= 0xFFFFFFFF
            if size > len(blob):
                raise _OleError('size')
            entries.append({
                'name': name, 'kind': raw[66], 'start': _u32(raw, 116), 'size': size,
            })
            if len(entries) > 100_000:
                raise _OleError('directory')
    return entries


def _read_regular(blob, sector_size, fat, start, size):
    if size == 0:
        return b''
    parts = []
    remaining = size
    for sector in _chain(fat, start, size // sector_size + 2):
        chunk = _read_sector(blob, sector, sector_size)[:remaining]
        parts.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    data = b''.join(parts)
    if len(data) < size:
        raise _OleError('stream')
    return data[:size]


def _mini_fat(blob, sector_size, fat):
    start = _u32(blob, 0x3C)
    count = _u32(blob, 0x40)
    if count <= 0 or count > 4096 or start >= _SPECIAL:
        raise _OleError('minifat')
    per = sector_size // 4
    data = []
    # The mini FAT itself is a regular-sector chain of `count` sectors, not a FAT walk.
    sector = start
    seen = set()
    while len(seen) < count:
        if sector in seen or sector >= _SPECIAL or sector < 0:
            raise _OleError('minifat')
        seen.add(sector)
        data.append(_read_sector(blob, sector, sector_size))
        if len(seen) == count:
            break
        if sector >= len(fat):
            raise _OleError('minifat')
        sector = fat[sector]
    blob_fat = b''.join(data)
    return [_u32(blob_fat, index) for index in range(0, len(blob_fat), 4)]


def _read_mini(blob, sector_size, fat, entries, start, size):
    shift = _u16(blob, 0x20)
    if shift not in (6, 7):
        raise _OleError('mini')
    mini_size = 1 << shift
    root = next((item for item in entries if item['kind'] == 5), None)
    if root is None:
        raise _OleError('root')
    container = _read_regular(blob, sector_size, fat, root['start'], root['size'])
    mini = _mini_fat(blob, sector_size, fat)
    parts = []
    remaining = size
    for sector in _chain(mini, start, size // mini_size + 2):
        offset = sector * mini_size
        if offset + mini_size > len(container):
            raise _OleError('mini')
        chunk = container[offset:offset + mini_size][:remaining]
        parts.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    data = b''.join(parts)
    if len(data) < size:
        raise _OleError('mini')
    return data[:size]


def _streams(path):
    blob = Path(path).read_bytes()
    sector_size, major = _sector_size(blob)
    fat = _load_fat(blob, sector_size)
    entries = _directory(blob, sector_size, fat, major)
    cutoff = _u32(blob, 0x38)
    if cutoff <= 0 or cutoff > len(blob):
        cutoff = 4096

    def read_named(name):
        wanted = name.casefold()
        entry = next((item for item in entries if item['name'].casefold() == wanted), None)
        if entry is None:
            return None
        if entry['kind'] == 5 or entry['size'] >= cutoff:
            return _read_regular(blob, sector_size, fat, entry['start'], entry['size'])
        return _read_mini(blob, sector_size, fat, entries, entry['start'], entry['size'])

    return entries, read_named


def _word_encrypted(data):
    if data is None or len(data) < 12:
        raise _OleError('word')
    flags = _u16(data, 0x0A)
    return bool(flags & 0x0100 or flags & 0x8000)


def _excel_encrypted(data):
    if data is None or len(data) < 4:
        raise _OleError('workbook')
    offset = 0
    limit = min(len(data), 65536)
    while offset + 4 <= limit:
        kind = _u16(data, offset)
        length = _u16(data, offset + 2)
        if length > 8224 or offset + 4 + length > len(data):
            return False
        if kind == 0x002F:
            return True
        if kind == 0x000A:
            return False
        offset += 4 + length
    return False


def _ppt_encrypted(data):
    if data is None:
        raise _OleError('ppt')
    return b'EncryptedSummary' in data[:65536]


def inspect_legacy(path, ext):
    """只读复合文档目录和必要流。不启动转换器，不执行宏。"""
    kind = _kind(ext)
    try:
        entries, read_named = _streams(path)
    except _OleError:
        return _fail(ext, 'invalid', f'不是有效的{kind}文件，未转换，也未解析正文。', 'legacy_corrupt')
    except OSError:
        return _fail(ext, 'invalid', f'无法读取{kind}原件，未转换，也未解析正文。', 'legacy_unreadable')
    names = {item['name'].casefold() for item in entries}
    if names & _MACRO_NAMES:
        return _fail(ext, 'unsupported', f'{kind} 含宏。未执行宏，也未转换或解析正文。', 'legacy_macro')
    if names & _PASSWORD_NAMES:
        return _fail(ext, 'unsupported', f'{kind} 已加密或设有密码，未转换，也未解析正文。', 'legacy_password')
    expected = _STREAM[ext]
    if ext == '.xls':
        present = 'workbook' in names or 'book' in names
    else:
        present = expected in names
    if not present:
        return _fail(ext, 'invalid', f'不是有效的{kind}文件，未转换，也未解析正文。', 'legacy_corrupt')
    try:
        if ext == '.doc':
            encrypted = _word_encrypted(read_named('WordDocument'))
        elif ext == '.xls':
            encrypted = _excel_encrypted(read_named('Workbook') if 'workbook' in names else read_named('Book'))
        else:
            encrypted = _ppt_encrypted(read_named('PowerPoint Document'))
    except _OleError:
        return _fail(ext, 'invalid', f'不是有效的{kind}文件，未转换，也未解析正文。', 'legacy_corrupt')
    if encrypted:
        return _fail(ext, 'unsupported', f'{kind} 已加密或设有密码，未转换，也未解析正文。', 'legacy_password')
    return None


def _gate(limit):
    with _GATE_LOCK:
        gate = _GATES.get(limit)
        if gate is None:
            gate = threading.BoundedSemaphore(limit)
            _GATES[limit] = gate
        return gate


def _soffice_env():
    allow = {
        'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE',
        'LOCALAPPDATA', 'APPDATA', 'COMSPEC', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE',
        'USER', 'USERNAME', 'LOGNAME', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
        'COMMONPROGRAMFILES', 'SYSTEMDRIVE',
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allow}
    env['SAL_NO_MOUSEGRABS'] = '1'
    if os.name != 'nt':
        env['SAL_USE_VCLPLUGIN'] = 'svp'
    return env


def _kill_process_tree(process):
    if process.poll() is not None and os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif os.name == 'nt':
        subprocess.run(
            ['taskkill', '/F', '/T', '/PID', str(process.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _popen(binary, args, cwd):
    kwargs = {
        'args': [str(binary), *args],
        'cwd': str(cwd),
        'env': _soffice_env(),
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
    }
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(**kwargs)


def _run(binary, args, cwd, timeout):
    process = _popen(binary, args, cwd)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        return 'timeout', b''
    return code, b''


def _run_captured(binary, args, cwd, timeout):
    kwargs = {
        'args': [str(binary), *args],
        'cwd': str(cwd),
        'env': _soffice_env(),
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.DEVNULL,
    }
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    process = subprocess.Popen(**kwargs)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return 'timeout', b''
    return process.returncode, output or b''


def _converter_label(binary, timeout, cwd):
    key = str(binary)
    with _VERSION_LOCK:
        cached = _VERSIONS.get(key)
    if cached:
        return cached, None
    code, output = _run_captured(binary, ['--version'], cwd, timeout)
    if code == 'timeout':
        return '', 'timeout'
    text = output.decode('utf-8', errors='replace')[:200]
    match = _VERSION_RE.search(text)
    label = match.group(0) if match else 'LibreOffice（版本未知）'
    if code == 0 or match:
        with _VERSION_LOCK:
            _VERSIONS[key] = label
    return label, None


def _convert_once(settings, source, ext, binary):
    kind = _kind(ext)
    target = _TARGET[ext]
    before = hashlib.sha256(source.read_bytes()).digest()
    mode = source.stat().st_mode
    try:
        with tempfile.TemporaryDirectory(prefix='dda-legacy-') as temporary:
            root = Path(temporary)
            profile = root / 'profile'
            (profile / 'user').mkdir(parents=True)
            (profile / 'user' / 'registrymodifications.xcu').write_text(_MACRO_XCU, encoding='utf-8')
            incoming = root / 'in'
            outgoing = root / 'out'
            incoming.mkdir()
            outgoing.mkdir()
            copy = incoming / f'source{ext}'
            copy.write_bytes(source.read_bytes())
            os.chmod(copy, stat.S_IREAD)
            if copy.resolve() == source.resolve():
                return _fail(ext, 'invalid', f'{kind} 转换被拒绝，未改动原件，也未解析正文。', 'legacy_failed')
            label, version_problem = _converter_label(binary, settings.soffice_timeout_seconds, root)
            if version_problem == 'timeout':
                return _fail(ext, 'unsupported', f'{kind} 转换超时，已停止 soffice 及其子进程。未解析正文，也不会自动重试。', 'legacy_timeout', converter=label)
            command = [
                '--headless', '--invisible', '--nologo', '--norestore', '--nofirststartwizard',
                '--nolockcheck', '--nodefault', f'-env:UserInstallation={profile.as_uri()}',
                '--convert-to', target[1:], '--outdir', str(outgoing), str(copy),
            ]
            code, _ = _run(binary, command, root, settings.soffice_timeout_seconds)
            if hashlib.sha256(source.read_bytes()).digest() != before or source.stat().st_mode != mode:
                return _fail(ext, 'invalid', f'{kind} 原件在转换时被改动，已中止，未解析正文。', 'legacy_original_changed', converter=label)
            if code == 'timeout':
                return _fail(ext, 'unsupported', f'{kind} 转换超时，已停止 soffice 及其子进程。未解析正文，也不会自动重试。', 'legacy_timeout', converter=label)
            produced = [item for item in outgoing.iterdir() if item.is_file() and item.suffix.lower() == target]
            if code != 0 or len(produced) != 1:
                return _fail(ext, 'unsupported', f'{kind} 转换失败，未解析正文，也不会自动重试。', 'legacy_failed', converter=label)
            from .office import classify_saved_file
            status, reason = classify_saved_file(f'converted{target}', produced[0])
            if status != 'registered':
                return _fail(
                    ext, 'unsupported' if status == 'unsupported' else 'invalid',
                    f'{kind} 的转换结果无法作为正文：{reason}', 'legacy_output_rejected', converter=label,
                )
            blob = produced[0].read_bytes()
    except Exception:
        if hashlib.sha256(source.read_bytes()).digest() != before:
            return _fail(ext, 'invalid', f'{kind} 原件在转换时被改动，已中止，未解析正文。', 'legacy_original_changed')
        return _fail(ext, 'unsupported', f'{kind} 转换失败，未解析正文，也不会自动重试。', 'legacy_failed')
    if hashlib.sha256(source.read_bytes()).digest() != before or source.stat().st_mode != mode:
        return _fail(ext, 'invalid', f'{kind} 原件在转换时被改动，已中止，未解析正文。', 'legacy_original_changed', converter=label)
    return {
        'ok': True, 'blob': blob, 'target_extension': target, 'reason': _success_reason(ext, label),
        'conversion': {
            'source_extension': ext, 'target_extension': target, 'tool': 'soffice',
            'converter': label, 'outcome': 'converted',
        },
    }


def convert_for_parse(settings, source, ext):
    """转成内存中的 docx／xlsx／pptx。不把中间文件留在数据目录，也不重试。"""
    problem = inspect_legacy(source, ext)
    if problem is not None:
        return problem
    binary, state = soffice_status(settings)
    if binary is None:
        return _fail(ext, 'unsupported', missing_soffice_reason(ext, state), 'soffice_missing')
    gate = _gate(settings.soffice_concurrency)
    gate.acquire()
    try:
        return _convert_once(settings, Path(source), ext, binary)
    finally:
        gate.release()
