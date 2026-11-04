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
