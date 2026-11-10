"""RG-05l 的 l1／l2：只读清单、dry-run 和日志轮转。不删除缓存。"""
from pathlib import Path

import pytest

from dd_agent.maintenance import (
    LOG_LIMIT, REASON_CHECKPOINT, REASON_ESCAPE, REASON_LEASE, REASON_ORIGINAL,
    REASON_RECLAIMABLE, REASON_RECOVERY, REASON_UNREGISTERED,
    append_error, append_output, cache_inventory, dry_run, inventory, redact, rotate_log,
)


def _snapshot(root):
    found = {}
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def test_inventory_and_dry_run_explain_every_item_and_delete_nothing(tmp_path):
    (tmp_path / 'documents').mkdir()
    (tmp_path / 'documents' / 'original.bin').write_bytes(b'orig')
    (tmp_path / 'recovery').mkdir()
    (tmp_path / 'recovery' / 'state.json').write_bytes(b'{}')
    (tmp_path / 'checkpoints').mkdir()
    (tmp_path / 'checkpoints' / 'vectors.json').write_bytes(b'vec')
    (tmp_path / 'cache').mkdir()
    (tmp_path / 'cache' / 'free.bin').write_bytes(b'free')
    (tmp_path / 'cache' / 'used.bin').write_bytes(b'used')
    (tmp_path / 'cache' / 'leased.bin').write_bytes(b'lease')
    (tmp_path / 'notes.txt').write_bytes(b'note')
    outside = tmp_path.parent / f'dda-outside-{tmp_path.name}.txt'
    outside.write_text('keep-me', encoding='utf-8')
    link = tmp_path / 'cache' / 'escape'
    link.symlink_to(outside)
    before = _snapshot(tmp_path)
    registry = [
        {'relative_path': 'cache/free.bin', 'bytes': 4, 'version': 'image-transform-v1',
         'referrers': [], 'lease_active': False, 'rebuildable': True, 'role': 'image'},
        {'relative_path': 'cache/used.bin', 'referrers': ['page:1'], 'lease_active': False,
         'rebuildable': True, 'role': 'image'},
        {'relative_path': 'cache/leased.bin', 'referrers': [], 'lease_active': True,
         'rebuildable': True, 'role': 'image'},
        {'relative_path': '../outside.bin', 'rebuildable': True, 'role': 'image'},
    ]
    report = dry_run(tmp_path, registry)
    reasons = {item['relative_path']: item['reason'] for item in report['items']}
    assert reasons['documents/original.bin'] == REASON_ORIGINAL
    assert reasons['recovery/state.json'] == REASON_RECOVERY
    assert reasons['checkpoints/vectors.json'] == REASON_CHECKPOINT
    assert reasons['cache/free.bin'] == REASON_RECLAIMABLE
    assert reasons['cache/used.bin'].startswith('仍被引用')
    assert reasons['cache/leased.bin'] == REASON_LEASE
    assert reasons['notes.txt'] == REASON_UNREGISTERED
    assert reasons['cache/escape'] == REASON_ESCAPE
    assert any(item['reason'] == REASON_ESCAPE and item['relative_path'] == '../outside.bin' for item in report['items'])
    assert report['deleted'] == [] and report['mode'] == 'dry-run'
    assert _snapshot(tmp_path) == before
    assert outside.read_text(encoding='utf-8') == 'keep-me'
    plain = inventory(tmp_path)
    assert plain['total_bytes'] == 26
    assert {item['name'] for item in plain['entries']} >= {'documents', 'recovery', 'cache', 'notes.txt'}
    outside.unlink()


def test_registered_original_stays_protected_even_if_marked_rebuildable(tmp_path):
    (tmp_path / 'documents').mkdir()
    (tmp_path / 'documents' / 'a.bin').write_bytes(b'abc')
    report = cache_inventory(tmp_path, [{
        'relative_path': 'documents/a.bin', 'rebuildable': True, 'referrers': [], 'lease_active': False,
    }])
    item = report['items'][0]
    assert item['decision'] == 'retain' and item['reason'] == REASON_ORIGINAL
    assert (tmp_path / 'documents' / 'a.bin').read_bytes() == b'abc'


def test_log_rotation_caps_copies_and_total_and_redacts_secrets(tmp_path):
    assert LOG_LIMIT == 8 * 1024 * 1024
    path = tmp_path / 'worker.log'
    path.write_bytes(b'api_key=supersecretvalue postgres://user:password@localhost/dda\n' * 3)
    assert rotate_log(path, limit=40, copies=2, total_limit=10_000) is True
    backup = (tmp_path / 'worker.log.1').read_text(encoding='utf-8')
    assert 'supersecretvalue' not in backup and 'postgres://' not in backup and 'password@' not in backup
    assert '已脱敏' in backup
    path.write_bytes(b'y' * 50)
    assert rotate_log(path, limit=40, copies=2, total_limit=80) is True
    assert (tmp_path / 'worker.log.1').read_bytes() == b'y' * 50
    # 两份备份合计超过总量时丢掉较旧的一份，当前业务文件不被动。
    path.write_bytes(b'z' * 50)
    assert rotate_log(path, limit=40, copies=2, total_limit=80) is True
    assert (tmp_path / 'worker.log.1').read_bytes() == b'z' * 50
    assert not (tmp_path / 'worker.log.2').exists()


def test_error_log_is_separate_and_does_not_store_body_or_dsn(tmp_path):
    output = tmp_path / 'worker.log'
    error = tmp_path / 'worker.error.log'
    append_output(output, '处理完成 api_key=abcd1234 bearer sk-live-secret-token\n', limit=1000)
    text = output.read_text(encoding='utf-8')
    assert 'abcd1234' not in text and 'sk-live' not in text and '处理完成' in text
    append_error(error, category='worker_failed', location='ocr_worker', request_id='req-1')
    recorded = error.read_text(encoding='utf-8')
    assert 'worker_failed' in recorded and 'req-1' in recorded
    assert '注册资本' not in recorded and 'postgres://' not in recorded
    with pytest.raises(TypeError):
        append_error(error, category='worker_failed', location='ocr_worker', request_id='req-1',
                     body='注册资本2000万元')
    assert output.resolve() != error.resolve()
    assert 'worker_failed' not in output.read_text(encoding='utf-8')
    assert redact('postgresql://reader:s3cret@127.0.0.1:5432/dda') == '[已脱敏]'
