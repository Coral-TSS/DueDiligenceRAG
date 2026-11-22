"""文件系统边界与 Windows 长路径命名空间的回归。"""
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path

import pytest

from dd_agent.storage import Storage


@pytest.mark.skipif(os.name != 'nt', reason='Windows resolve 命名空间')
def test_resolved_extended_prefix_does_not_reject_same_storage_root(tmp_path, monkeypatch):
    store = Storage(tmp_path, 100)
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == 'blob.bin':
            # CPython 的 non-strict realpath 在目标从不存在变为存在时可能保留前缀。
            return Path('\\\\?\\' + str(result))
        return result
    monkeypatch.setattr(Path, 'resolve', resolve)
    assert store.path('documents/blob.bin') == store.root / 'documents/blob.bin'
    with pytest.raises(ValueError, match='Invalid storage path'):
        store.path('../blob.bin')


def test_storage_rejects_parent_escape(tmp_path):
    with pytest.raises(ValueError, match='Invalid storage path'):
        Storage(tmp_path, 100).path('../escape.bin')


def test_storage_rejects_symlink_escape(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (root / 'linked').symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'Symlink privilege unavailable: {exc.winerror if os.name == "nt" else exc.errno}')
    with pytest.raises(ValueError, match='Invalid storage path'):
        Storage(root, 100).path('linked/escape.bin')


def test_concurrent_blob_publication_preserves_bytes(tmp_path):
    store = Storage(tmp_path, 100)
    body = b'%PDF-1.7 identical bytes'
    def upload(_):
        return store.save('project', 'sample.pdf', io.BytesIO(body))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(upload, range(400)))
    assert len({r['storage_key'] for r in results}) == 1
    assert store.path(results[0]['storage_key']).read_bytes() == body
    assert not list((tmp_path / 'staging').glob('*.part'))


def _png_bytes(width=1, height=1):
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    raw = b''.join(b'\x00' + b'\xff\x00\x00' * width for _ in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def _jpeg_bytes():
    from PIL import Image
    from io import BytesIO
    buffer = BytesIO()
    Image.new('RGB', (4, 3), 'white').save(buffer, format='JPEG')
    return buffer.getvalue()


def test_storage_registers_jpeg_and_png(tmp_path):
    store = Storage(tmp_path, 10_000_000)
    jpeg = _jpeg_bytes()
    png = _png_bytes()
    saved_jpeg = store.save('project', '执照.JPG', io.BytesIO(jpeg))
    saved_png = store.save('project', '扫描件.png', io.BytesIO(png))
    assert saved_jpeg['status'] == 'registered' and saved_png['status'] == 'registered'
    assert '仅登记 PDF' not in saved_jpeg['reason'] and '仅登记 PDF' not in saved_png['reason']
    assert store.path(saved_jpeg['storage_key']).read_bytes() == jpeg
    assert store.path(saved_png['storage_key']).read_bytes() == png


def test_storage_rejects_image_header_mismatch(tmp_path):
    store = Storage(tmp_path, 10_000_000)
    jpeg_as_png = store.save('project', '假图.png', io.BytesIO(_jpeg_bytes()))
    png_as_jpeg = store.save('project', '假图.jpeg', io.BytesIO(_png_bytes()))
    text_as_jpg = store.save('project', '说明.jpg', io.BytesIO(b'hello'))
    assert jpeg_as_png['status'] == 'invalid' and 'PNG' in jpeg_as_png['reason']
    assert png_as_jpeg['status'] == 'invalid' and 'JPEG' in png_as_jpeg['reason']
    assert text_as_jpg['status'] == 'invalid'
    assert '仅登记 PDF' not in jpeg_as_png['reason']


def test_storage_pdf_and_office_classification_unchanged(tmp_path):
    store = Storage(tmp_path, 10_000_000)
    pdf = store.save('project', '合同.pdf', io.BytesIO(b'%PDF-1.7 body'))
    empty = store.save('project', 'empty.pdf', io.BytesIO(b''))
    fake_pdf = store.save('project', 'fake.pdf', io.BytesIO(b'hello'))
    text = store.save('project', '说明.txt', io.BytesIO(b'hello'))
    broken_docx = store.save('project', '章程.docx', io.BytesIO(b'not-a-zip'))
    assert pdf['status'] == 'registered'
    assert empty['status'] == 'invalid' and '空' in empty['reason']
    assert fake_pdf['status'] == 'invalid' and 'PDF' in fake_pdf['reason']
    assert text['status'] == 'unsupported'
    assert '当前阶段仅登记 PDF 供后续解析；此格式暂不支持处理。' not in text['reason']
    assert 'JPG' in text['reason']
    assert broken_docx['status'] == 'invalid'
    assert 'Office' in broken_docx['reason'] or '压缩包' in broken_docx['reason']
