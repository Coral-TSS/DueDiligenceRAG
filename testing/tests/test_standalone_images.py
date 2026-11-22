"""独立图片登记、再导入升级与单页 PNG 准备，不连接数据库，也不跑 OCR。"""
from pathlib import Path

import pytest

from dd_agent.image_artifacts import SINGLE_IMAGE_PIXELS, inspect_artifact, png_size
from dd_agent.standalone_images import (
    StandaloneImageError, REGISTERED_IMAGE_REASON, can_upgrade_unsupported,
    classify_non_office_file, decide_legacy_image_upgrade, inspect_standalone_image,
    materialize_standalone_png, upgrade_legacy_unsupported_images,
)
from test_storage import _jpeg_bytes, _png_bytes


def test_classify_non_office_file_accepts_images_and_keeps_pdf_rules():
    jpeg, png = _jpeg_bytes(), _png_bytes()
    assert classify_non_office_file('a.jpg', len(jpeg), jpeg)[0] == 'registered'
    assert classify_non_office_file('a.jpeg', len(jpeg), jpeg)[0] == 'registered'
    assert classify_non_office_file('a.PNG', len(png), png)[0] == 'registered'
    assert '仅登记 PDF' not in classify_non_office_file('a.jpg', len(jpeg), jpeg)[1]
    assert classify_non_office_file('a.jpg', len(png), png)[0] == 'invalid'
    assert classify_non_office_file('a.pdf', 12, b'%PDF-1.7 xxx')[0] == 'registered'
    assert classify_non_office_file('a.pdf', 5, b'hello')[0] == 'invalid'
    assert classify_non_office_file('a.pdf', 0, b'')[0] == 'invalid'
    status, reason = classify_non_office_file('a.txt', 5, b'hello')
    assert status == 'unsupported'
    assert reason != '当前阶段仅登记 PDF 供后续解析；此格式暂不支持处理。'


def test_can_upgrade_unsupported_only_for_soffice_or_old_pdf_only_reason():
    old_image = '当前阶段仅登记 PDF 供后续解析；此格式暂不支持处理。'
    assert can_upgrade_unsupported('unsupported', old_image, 'registered') is True
    assert can_upgrade_unsupported('unsupported', '此格式暂不支持处理。请安装转换器。', 'registered') is True
    assert can_upgrade_unsupported('unsupported', '未找到 LibreOffice 的 soffice，暂不能转换旧版 Word。', 'registered') is True
    assert can_upgrade_unsupported('unsupported', '文件已加密或设有密码，未解析正文。', 'registered') is False
    assert can_upgrade_unsupported('unsupported', '文件含宏。未执行宏，也未解析正文。', 'registered') is False
    assert can_upgrade_unsupported('invalid', old_image, 'registered') is False
    assert can_upgrade_unsupported('unsupported', old_image, 'unsupported') is False
    assert can_upgrade_unsupported('unsupported', '不是有效的 Office 压缩包，未解析正文。', 'registered') is False


def test_inspect_and_materialize_png_as_one_page(tmp_path):
    source = tmp_path / 'scan.png'
    source.write_bytes(_png_bytes(8, 6))
    profile = inspect_standalone_image(source)
    assert profile == {'page_count': 1, 'pages': [{'page_number': 1, 'characters': 0, 'route': 'ocr'}], 'source_kind': 'image'}
    target = tmp_path / 'page-1-x1-abc.png'
    written = Path(materialize_standalone_png(source, target, max_pixels=SINGLE_IMAGE_PIXELS, max_file_bytes=1_000_000))
    assert written == target and png_size(target.read_bytes()) == (8, 6)
    assert inspect_artifact(target)['state'] == 'unsealed'


def test_materialize_jpeg_to_png_and_reject_corrupt_or_oversize(tmp_path):
    source = tmp_path / 'photo.jpg'
    source.write_bytes(_jpeg_bytes())
    target = tmp_path / 'page-1.png'
    materialize_standalone_png(source, target, max_pixels=SINGLE_IMAGE_PIXELS, max_file_bytes=1_000_000)
    assert target.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'
    width, height = png_size(target.read_bytes())
    assert width >= 1 and height >= 1

    truncated = tmp_path / 'broken.png'
    truncated.write_bytes(_png_bytes(2, 2)[:20])
    with pytest.raises(StandaloneImageError) as failed:
        inspect_standalone_image(truncated)
    assert failed.value.error_code == 'image_corrupt'

    with pytest.raises(StandaloneImageError) as budget:
        materialize_standalone_png(source, tmp_path / 'huge.png', max_pixels=1, max_file_bytes=1_000_000)
    assert budget.value.error_code == 'image_budget_exceeded'

    chopped = tmp_path / 'chopped.jpg'
    chopped.write_bytes(_jpeg_bytes()[:12])
    with pytest.raises(StandaloneImageError) as jpeg_failed:
        materialize_standalone_png(chopped, tmp_path / 'out.png', max_pixels=SINGLE_IMAGE_PIXELS, max_file_bytes=1_000_000)
    assert jpeg_failed.value.error_code == 'image_corrupt'


OLD_REASON = '当前阶段仅登记 PDF 供后续解析；此格式暂不支持处理。'


def test_decide_legacy_image_upgrade_registers_valid_images_only():
    jpeg, png = _jpeg_bytes(), _png_bytes()
    jpeg_result = decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['回单.JPG'], len(jpeg), jpeg, file_available=True)
    png_result = decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['扫描件.png'], len(png), png, file_available=True)
    assert jpeg_result == ('registered', REGISTERED_IMAGE_REASON)
    assert png_result == ('registered', REGISTERED_IMAGE_REASON)
    assert decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['回单.jpg'], len(jpeg), jpeg, file_available=False) is None
    assert decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['空.jpg'], 0, b'', file_available=True) is None
    assert decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['假图.jpg'], 5, b'hello', file_available=True) is None
    assert decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['说明.txt'], 5, b'hello', file_available=True) is None
    assert decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['章程.docx'], 12, b'PK\x03\x04hello', file_available=True) is None
    assert decide_legacy_image_upgrade(
        'unsupported', '文件已加密或设有密码，未解析正文。', ['回单.jpg'], len(jpeg), jpeg, file_available=True) is None
    assert decide_legacy_image_upgrade(
        'unsupported', '文件含宏。未执行宏，也未解析正文。', ['回单.jpg'], len(jpeg), jpeg, file_available=True) is None
    assert decide_legacy_image_upgrade(
        'invalid', OLD_REASON, ['回单.jpg'], len(jpeg), jpeg, file_available=True) is None
    assert decide_legacy_image_upgrade(
        'registered', REGISTERED_IMAGE_REASON, ['回单.jpg'], len(jpeg), jpeg, file_available=True) is None
    # 多个原文件名时，只要有一个符合现行图片登记规则即可升级。
    mixed = decide_legacy_image_upgrade(
        'unsupported', OLD_REASON, ['说明.txt', '回单.jpeg'], len(jpeg), jpeg, file_available=True)
    assert mixed == ('registered', REGISTERED_IMAGE_REASON)


class _FakeDatabase:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.updates = []
        self.enqueue_calls = []

    def list_legacy_unsupported_documents(self):
        return [
            row for row in self.rows
            if row['status'] == 'unsupported' and (
                '当前阶段仅登记 PDF' in (row['reason'] or '')
                or '此格式暂不支持处理' in (row['reason'] or ''))
        ]

    def apply_legacy_image_upgrade(self, document_id, status, reason):
        self.updates.append((document_id, status, reason))
        for row in self.rows:
            if row['id'] == document_id and row['status'] == 'unsupported':
                row['status'] = status
                row['reason'] = reason
                return {'id': document_id, 'status': status, 'reason': reason,
                        'category': row.get('category'), 'subject_id': row.get('subject_id')}
        return None


def test_upgrade_legacy_unsupported_images_is_idempotent_and_does_not_enqueue(tmp_path):
    from dd_agent.storage import Storage

    store = Storage(tmp_path, 10_000_000)
    jpeg, png = _jpeg_bytes(), _png_bytes()
    saved_jpeg = store.save('project', '回单.jpg', __import__('io').BytesIO(jpeg))
    saved_png = store.save('project', '扫描件.png', __import__('io').BytesIO(png))
    missing_key = 'documents/project/missing.bin'
    mismatch = store.save('project', '假图.jpg', __import__('io').BytesIO(b'hello'))
    empty = store.save('project', '空.jpg', __import__('io').BytesIO(b''))
    rows = [
        {'id': 'jpeg-1', 'status': 'unsupported', 'reason': OLD_REASON,
         'storage_key': saved_jpeg['storage_key'], 'filenames': ['回单.jpg'],
         'category': 'incorporation', 'subject_id': 'subject-1'},
        {'id': 'png-1', 'status': 'unsupported', 'reason': OLD_REASON,
         'storage_key': saved_png['storage_key'], 'filenames': ['扫描件.png'],
         'category': 'incorporation', 'subject_id': None},
        {'id': 'missing-1', 'status': 'unsupported', 'reason': OLD_REASON,
         'storage_key': missing_key, 'filenames': ['丢失.jpg']},
        {'id': 'mismatch-1', 'status': 'unsupported', 'reason': OLD_REASON,
         'storage_key': mismatch['storage_key'], 'filenames': ['假图.jpg']},
        {'id': 'empty-1', 'status': 'unsupported', 'reason': OLD_REASON,
         'storage_key': empty['storage_key'], 'filenames': ['空.jpg']},
        {'id': 'password-1', 'status': 'unsupported', 'reason': '文件已加密或设有密码，未解析正文。',
         'storage_key': saved_jpeg['storage_key'], 'filenames': ['回单.jpg']},
    ]
    database = _FakeDatabase(rows)
    first = upgrade_legacy_unsupported_images(database, store)
    assert first == 2
    assert database.updates == [
        ('jpeg-1', 'registered', REGISTERED_IMAGE_REASON),
        ('png-1', 'registered', REGISTERED_IMAGE_REASON),
    ]
    assert database.enqueue_calls == []
    # 未升级行保持原状；已升级行只改了 status／reason。
    by_id = {row['id']: row for row in database.rows}
    assert by_id['jpeg-1']['category'] == 'incorporation' and by_id['jpeg-1']['subject_id'] == 'subject-1'
    assert by_id['missing-1']['status'] == 'unsupported' and by_id['missing-1']['reason'] == OLD_REASON
    assert by_id['mismatch-1']['status'] == 'unsupported'
    assert by_id['empty-1']['status'] == 'unsupported'
    assert by_id['password-1']['status'] == 'unsupported'
    second = upgrade_legacy_unsupported_images(database, store)
    assert second == 0
    assert database.updates == [
        ('jpeg-1', 'registered', REGISTERED_IMAGE_REASON),
        ('png-1', 'registered', REGISTERED_IMAGE_REASON),
    ]


def test_startup_upgrades_legacy_images_before_wake_and_skips_when_not_ready(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dd_agent.app import create_app
    from dd_agent.config import Settings

    calls = []

    def fake_upgrade(database, storage):
        calls.append('upgrade')
        assert storage is not None
        return 1

    monkeypatch.setattr('dd_agent.standalone_images.upgrade_legacy_unsupported_images', fake_upgrade)
    settings = Settings(database_url='', data_dir=tmp_path, max_upload_bytes=1024)
    app = create_app(settings)
    app.state.database.health = lambda: {'status': 'ready', 'message': 'ok'}
    woke = []
    app.state.jobs.wake = lambda: woke.append('wake')
    with TestClient(app, base_url='http://127.0.0.1:8765'):
        pass
    assert calls == ['upgrade']
    assert woke == ['wake']

    calls.clear()
    woke.clear()
    app = create_app(settings)
    app.state.database.health = lambda: {'status': 'needs_configuration', 'message': 'x'}
    app.state.jobs.wake = lambda: woke.append('wake')
    with TestClient(app, base_url='http://127.0.0.1:8765'):
        pass
    assert calls == []
    assert woke == []
