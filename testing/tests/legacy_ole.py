"""测试用的最小 OLE 复合文档。只含目录和指定流，不含可执行宏。"""
import struct


_FREE = 0xFFFFFFFF
_END = 0xFFFFFFFE
_FAT = 0xFFFFFFFD


def _entry(name, kind, start, size, child=_FREE):
    raw = bytearray(128)
    encoded = (name + '\0').encode('utf-16le')
    raw[:len(encoded)] = encoded
    struct.pack_into('<H', raw, 64, len(encoded))
    raw[66] = kind
    raw[67] = 1
    struct.pack_into('<III', raw, 68, _FREE, _FREE, child)
    struct.pack_into('<I', raw, 116, start)
    struct.pack_into('<Q', raw, 120, size)
    return bytes(raw)


def _header(*, mini_fat=_END, mini_count=0):
    header = bytearray(512)
    header[:8] = bytes.fromhex('D0CF11E0A1B11AE1')
    struct.pack_into('<H', header, 0x18, 0x003E)
    struct.pack_into('<H', header, 0x1A, 3)
    struct.pack_into('<H', header, 0x1C, 0xFFFE)
    struct.pack_into('<H', header, 0x1E, 9)
    struct.pack_into('<H', header, 0x20, 6)
    struct.pack_into('<I', header, 0x2C, 1)
    struct.pack_into('<I', header, 0x30, 1)
    struct.pack_into('<I', header, 0x38, 4096)
    struct.pack_into('<I', header, 0x3C, mini_fat)
    struct.pack_into('<I', header, 0x40, mini_count)
    struct.pack_into('<I', header, 0x44, _END)
    struct.pack_into('<I', header, 0x48, 0)
    struct.pack_into('<I', header, 0x4C, 0)
    for index in range(1, 109):
        struct.pack_into('<I', header, 0x4C + index * 4, _FREE)
    return header


def build_ole(entries):
    """每个流至少按 4096 字节放入普通扇区，避免走迷你流。"""
    payloads = []
    for name, data in entries:
        if len(data) < 4096:
            data = data + bytes(4096 - len(data))
        payloads.append((name, data))
    fat = [_FREE] * 128
    fat[0] = _FAT
    fat[1] = _END
    directory = bytearray(512)
    directory[:128] = _entry('Root Entry', 5, _END, 0, child=1 if payloads else _FREE)
    sectors = []
    next_sector = 2
    for index, (name, data) in enumerate(payloads, start=1):
        start = next_sector
        chunks = [data[i:i + 512] for i in range(0, len(data), 512)]
        for nth, chunk in enumerate(chunks):
            sector = start + nth
            fat[sector] = _END if nth + 1 == len(chunks) else sector + 1
            sectors.append(chunk.ljust(512, b'\0'))
        directory[index * 128:(index + 1) * 128] = _entry(name, 2, start, len(data))
        next_sector += len(chunks)
    fat_bytes = b''.join(struct.pack('<I', value) for value in fat)
    return bytes(_header()) + fat_bytes + bytes(directory) + b''.join(sectors)


def word_document(*, encrypted=False):
    data = bytearray(32)
    struct.pack_into('<H', data, 0, 0xA5EC)
    struct.pack_into('<H', data, 2, 0x00C1)
    if encrypted:
        struct.pack_into('<H', data, 0x0A, 0x0100)
    return build_ole([('WordDocument', bytes(data))])


def word_with_macro():
    data = bytearray(32)
    struct.pack_into('<H', data, 0, 0xA5EC)
    return build_ole([('WordDocument', bytes(data)), ('VBA', b'not-run')])


def workbook(*, encrypted=False):
    def record(kind, payload=b''):
        return struct.pack('<HH', kind, len(payload)) + payload
    body = record(0x0809, b'\x00' * 16)
    if encrypted:
        body += record(0x002F, b'\x01\x00\x01\x00\x01\x00')
    body += record(0x000A)
    return build_ole([('Workbook', body)])


def presentation(*, encrypted=False):
    payload = b'EncryptedSummary' if encrypted else b'PPT-ok'
    return build_ole([('PowerPoint Document', payload)])


def mini_word(*, encrypted=False):
    """WordDocument 小于截止大小，必须从迷你流读出加密标志。"""
    fib = bytearray(32)
    struct.pack_into('<H', fib, 0, 0xA5EC)
    struct.pack_into('<H', fib, 2, 0x00C1)
    if encrypted:
        struct.pack_into('<H', fib, 0x0A, 0x0100)
    fat = [_FREE] * 128
    fat[0] = _FAT
    fat[1] = _END
    fat[2] = _END
    fat[3] = _END
    directory = bytearray(512)
    directory[:128] = _entry('Root Entry', 5, 2, 64, child=1)
    directory[128:256] = _entry('WordDocument', 2, 0, 32)
    container = bytearray(512)
    container[:32] = fib
    mini_fat = bytearray(512)
    struct.pack_into('<I', mini_fat, 0, _END)
    for index in range(1, 128):
        struct.pack_into('<I', mini_fat, index * 4, _FREE)
    fat_bytes = b''.join(struct.pack('<I', value) for value in fat)
    return bytes(_header(mini_fat=3, mini_count=1)) + fat_bytes + bytes(directory) + bytes(container) + bytes(mini_fat)
