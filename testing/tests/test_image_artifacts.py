"""RG-05k：产物键、坐标、预算、租约和损坏重建。不启动 Windows OCR 工作进程。"""
import json
import struct
import threading
import zlib
from pathlib import Path

import pytest

from dd_agent.image_artifacts import (
    TRANSFORM_VERSION, ArtifactBoundsError, ArtifactKindError, ArtifactLimitError,
    ArtifactMeter, DecodeScope, GenerationLease, PageTransform, admit_image, artifact_key,
    assert_recognition_input, atomic_save, generate_once, inspect_artifact, plan_pages,
    publish_png, restart_action, seal_rendered_png,
)


def png_bytes(width, height, rgb=(10, 20, 30)):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b''.join(b'\x00' + bytes(rgb) * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))


def test_artifact_key_keeps_render_crop_and_splits_thumbnails():
    shared = dict(source_sha256='ab' * 32, page=3, engine='pdfium', dpi=144, color='rgb',
                  orientation=90, crop=(1, 2, 8, 9), transform_version=TRANSFORM_VERSION)
    recognition = artifact_key(kind='recognition', **shared)
    thumbnail = artifact_key(kind='thumbnail', **shared)
    assert recognition != thumbnail
    for part in ('pdfium', '144', 'rgb', '90', '1,2,8,9', TRANSFORM_VERSION, 'recognition'):
        assert part in recognition
    with pytest.raises(ArtifactKindError):
        artifact_key(kind='icon', **shared)


def test_rotation_scale_and_tilt_round_trip_and_crop_rejects_out_of_bounds():
    turned = PageTransform(100, 80, 90, 2)
    assert turned.artifact_size == (160, 200)
    assert turned.to_artifact(10, 20) == (40, 180)
    assert turned.to_source(40, 180) == (10, 20)
    scaled = PageTransform(595, 842, 0, 3)
    assert scaled.to_artifact(10, 20) == (30, 60)
    assert scaled.to_source(30, 60) == (10, 20)
    tilt = PageTransform(120, 90, 15, 1.5)
    back = tilt.to_source(*tilt.to_artifact(12.5, 40))
    assert back == pytest.approx((12.5, 40))
    assert tilt.matrix()['transform_version'] == TRANSFORM_VERSION
    inside = PageTransform(100, 80, 0, 2, crop=(10, 10, 40, 30))
    assert inside.to_artifact(10, 10) == (0, 0)
    assert inside.artifact_size == (60, 40)
    with pytest.raises(ArtifactBoundsError):
        PageTransform(100, 80, 0, 2, crop=(90, 70, 110, 90))
    with pytest.raises(ArtifactBoundsError):
        PageTransform(100, 80, 90, 2, crop=(-1, 0, 10, 10))


def test_pixel_and_disk_limits_reject_without_writing_a_smaller_image(tmp_path):
    target = tmp_path / 'page.png'
    original = png_bytes(4, 4)
    with pytest.raises(ArtifactLimitError, match='未降低画质'):
        publish_png(target, original, width=4000, height=4000, single_limit=1000, inflight=0,
                    inflight_limit=10_000_000, disk_used=0, disk_limit=10_000)
    assert not target.exists()
    with pytest.raises(ArtifactLimitError, match='在途像素'):
        admit_image(100, 100, single_limit=10_000_000, inflight=500, inflight_limit=600,
                    disk_used=0, incoming_bytes=1, disk_limit=100)
    with pytest.raises(ArtifactLimitError, match='磁盘字节'):
        publish_png(target, original, width=4, height=4, single_limit=1000, inflight=0,
                    inflight_limit=1000, disk_used=50, disk_limit=60)
    assert not target.exists()
    publish_png(target, original, width=4, height=4, single_limit=1000, inflight=0,
                inflight_limit=1000, disk_used=0, disk_limit=10_000)
    assert target.read_bytes() == original


def test_atomic_save_hides_partial_files(tmp_path):
    target = tmp_path / 'page.png'
    payload = png_bytes(2, 2)
    seen = {}

    def produce():
        assert not target.exists()
        atomic_save(target, payload)
        seen['partials'] = list(tmp_path.glob('*.partial'))
        return target

    assert produce() == target
    assert seen['partials'] == []
    assert target.read_bytes() == payload


def test_corrupt_or_missing_rebuilds_and_ocr_failure_reuses_the_image(tmp_path):
    path = tmp_path / 'readings' / 'doc' / 'rendered' / 'page-1-x2-abc.png'
    payload = png_bytes(2, 2)
    atomic_save(path, payload)
    assert inspect_artifact(path)['state'] == 'unsealed'
    descriptor = seal_rendered_png(path, root=tmp_path, relative_path=path.relative_to(tmp_path).as_posix(),
                                   source_sha256='abc' * 12, page=1, kind='recognition', scale=2)
    assert inspect_artifact(path)['state'] == 'ok'
    assert descriptor['kind'] == 'recognition'
    action = restart_action(image_intact=True, ocr_valid=False, image_required=True)
    assert action == {'images': 'reuse', 'redo_ocr': True, 'redo_text': False, 'scope': 'page'}
    path.write_bytes(payload[:-8])
    assert inspect_artifact(path)['state'] == 'corrupt'
    missing = restart_action(image_intact=False, ocr_valid=False, image_required=True)
    assert missing['images'] == 'rebuild' and missing['redo_ocr'] is True and missing['scope'] == 'page'
    path.unlink()
    assert inspect_artifact(path)['state'] == 'missing'
    preview = restart_action(image_intact=False, ocr_valid=True, image_required=False, preview_missing=True)
    assert preview['images'] == 'rebuild_preview' and preview['redo_ocr'] is False
    native = restart_action(image_intact=False, ocr_valid=True, image_required=False, params_changed=True,
                            text_depends_on_image=False)
    assert native['redo_text'] is False and native['scope'] == 'page'
    scanned = restart_action(image_intact=False, ocr_valid=True, image_required=True, params_changed=True,
                             text_depends_on_image=True)
    assert scanned['images'] == 'invalidate' and scanned['redo_ocr'] is True and scanned['scope'] == 'page'


def test_thumbnail_is_not_a_recognition_input():
    recognition = {'kind': 'recognition', 'relative_path': 'readings/doc/rendered/page.png', 'key': 'recognition'}
    thumbnail = {'kind': 'thumbnail', 'relative_path': 'image-artifacts/thumbnail/page.png', 'key': 'thumbnail'}
    assert_recognition_input(recognition)
    with pytest.raises(ArtifactKindError):
        assert_recognition_input(thumbnail)
    reuse, render = plan_pages([1], {1: 'ocr'}, lambda number: True, False, kinds={1: 'thumbnail'})
    assert reuse == [] and render == [1]


def test_single_key_lease_renders_once_under_contention(tmp_path):
    target = tmp_path / 'page.png'
    produced = []
    barrier = threading.Barrier(2)

    def intact():
        return target if target.is_file() else None

    def produce():
        produced.append(threading.get_ident())
        threading.Event().wait(0.15)
        atomic_save(target, png_bytes(2, 2))
        return target

    results = []

    def worker(owner):
        barrier.wait()
        results.append(generate_once(tmp_path, 'same-key', owner, produce, intact))

    threads = [threading.Thread(target=worker, args=(f'owner-{index}',)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(produced) == 1
    assert sum(bool(item['rendered']) for item in results) == 1
    rendered = next(item for item in results if item['rendered'])
    assert rendered['owner']['owner'] in {'owner-0', 'owner-1'}
    assert rendered['owner']['pid'] > 0


def test_lease_owner_is_visible_while_held_and_cleared_on_exit(tmp_path):
    key = artifact_key(source_sha256='cd' * 32, page=1, kind='recognition', engine='pdfium', dpi=144,
                       color='rgb', orientation=0, crop=None, transform_version=TRANSFORM_VERSION)
    lease = GenerationLease(tmp_path, key, 'owner-a')
    with lease:
        holder = lease.holder()
        assert holder['owner'] == 'owner-a' and holder['pid'] > 0 and holder['key'] == key
    assert lease._file is None
    with GenerationLease(tmp_path, key, 'owner-b') as second:
        assert second.holder()['owner'] == 'owner-b'


def test_decode_scope_reuses_and_releases(tmp_path):
    opened = []

    class Image:
        def __init__(self, path):
            self.path = path
            self.pixels = 10
            self.closed = False
            opened.append(path)

        def close(self):
            self.closed = True

    meter = ArtifactMeter()
    with DecodeScope(limit=2, opener=Image, meter=meter) as scope:
        first = scope.get(tmp_path / 'a.png')
        again = scope.get(tmp_path / 'a.png')
        scope.get(tmp_path / 'b.png')
        assert first is again
        assert meter.decodes == 2
        assert meter.peak_pixels == 20
        with pytest.raises(ArtifactLimitError):
            scope.get(tmp_path / 'c.png')
    assert first.closed and meter.decodes == 2
    with DecodeScope(limit=1, opener=Image, meter=meter) as scope:
        scope.get(tmp_path / 'a.png')
    assert meter.decodes == 3


def test_reader_does_not_rerender_after_ocr_failure_and_rebuilds_corrupt_files(tmp_path, database_url, monkeypatch):
    from dd_agent.config import Settings
    from dd_agent.reading import Reader
    from test_reading import make_pdf
    from test_workspace import application, client, project, upload

    body = make_pdf(tmp_path / 'scan.pdf', [''])
    renders = []
    ocr_calls = []

    def fake_worker(python, worker, manifest, directory, timeout_seconds=1200):
        del python, directory, timeout_seconds
        if manifest.get('mode') == 'inspect':
            yield {'page_count': 1, 'pages': [{'page_number': 1, 'characters': 0, 'route': 'ocr'}]}
            return
        if worker != 'pdf_worker':
            ocr_calls.append(manifest.get('pages'))
            page = manifest['pages'][0]
            yield {'page_number': page['page_number'], 'error_code': 'ocr_page_failed', 'image_path': page.get('image_path', '')}
            return
        for number in manifest['pages']:
            if number not in set(manifest.get('render_pages') or []):
                yield {'page_number': number, 'route': 'ocr', 'text': ''}
                continue
            renders.append(number)
            target = Path(manifest['output_dir']) / manifest['image_pattern'].format(number=number)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_save(target, png_bytes(2, 2))
            yield {'page_number': number, 'route': 'ocr', 'text': '', 'image_path': str(target)}

    monkeypatch.setattr('dd_agent.reading.worker_records', fake_worker)
    monkeypatch.setattr('dd_agent.reading.ocr_records', lambda settings, manifest, directory: fake_worker(
        '', 'ocr_worker', manifest, directory))
    app = application(tmp_path / 'data', database_url, migrate=True, limit=10000)
    with client(app) as http:
        project_id = project(http)
        document_id = upload(http, project_id, body=body).json()['document']['id']
    settings = Settings(database_url=database_url, data_dir=tmp_path / 'data', model_runtime_enabled=False,
                        ocr_models_dir=tmp_path / 'models')
    reader = Reader(settings, app.state.database)
    first = reader.read(project_id, document_id, [1])
    assert first['failed_pages'] == 1
    assert renders == [1]
    second = Reader(settings, app.state.database).read(project_id, document_id, [1])
    assert second['failed_pages'] == 1
    assert renders == [1]
    assert len(ocr_calls) == 2
    rendered = next((tmp_path / 'data').rglob('page-1-*.png'))
    rendered.write_bytes(png_bytes(2, 2)[:-5])
    Reader(settings, app.state.database).read(project_id, document_id, [1])
    assert renders == [1, 1]
    sidecar = json.loads(Path(str(rendered) + '.dda.json').read_text(encoding='utf-8'))
    assert sidecar['kind'] == 'recognition'
    assert_recognition_input(sidecar)
