"""Original OCR is immutable to reviews; stale/manual decisions must not leak into search."""
import importlib
from pathlib import Path

import pytest

from dd_agent.config import Settings
from test_retrieval import setup, TinyModels
from test_workspace import client, project


def review_module():
    try:
        return importlib.import_module('dd_agent.review')
    except ModuleNotFoundError:
        pytest.fail('Evidence review and candidate history are not implemented')


def service(app, settings):
    return review_module().ReviewService(settings)








class VisionStub:
    fail = False
    def recognize(self, **kwargs):
        if self.fail:
            raise review_module().ReviewError('合成视觉模型不可用')
        return {'text': '注册资本为5000万元。', 'model_version': 'synthetic-v1',
                'image_sha256': 'a' * 64, 'elapsed_seconds': 0.2, 'truncated': False,
                'crop_box': [0, 0, 10, 10], 'image_size': [10, 10]}


def test_vision_candidates_remain_unadopted_and_failures_are_recorded(tmp_path, database_url):
    app, p, d, evidence, _ = setup(tmp_path, database_url)
    vision = VisionStub()
    reviewer = review_module().ReviewService(evidence.settings, vision=vision)
    initial = reviewer.state(p, d, 1)
    candidate = reviewer.recognize(p, d, 1, [0, 0, 1, 1], initial['raw_revision'])
    assert candidate['status'] == 'succeeded'
    assert candidate['result']['text'] == '注册资本为5000万元。'
    assert reviewer.state(p, d, 1)['adoption'] is None
    assert app.state.database.pages(d)[0]['text'] == '注册资本为2000万元。'
    vision.fail = True
    failed = reviewer.recognize(p, d, 1, [0, 0, .5, .5], initial['raw_revision'])
    assert failed['status'] == 'failed'
    assert failed['error_code'] == 'vision_failed'
    assert len(reviewer.state(p, d, 1)['candidates']) == 2
    assert app.state.database.pages(d)[0]['status'] == 'succeeded'
    for box in ([0, 0, 0, 1], [-.1, 0, 1, 1], [0, 0, 2, 1], [0, 0, float('nan'), 1]):
        with pytest.raises(ValueError):
            reviewer.recognize(p, d, 1, box, initial['raw_revision'])


def test_candidate_returning_after_source_change_is_marked_stale(tmp_path, database_url):
    app, p, d, evidence, _ = setup(tmp_path, database_url)
    class ChangingVision(VisionStub):
        def recognize(self, **kwargs):
            app.state.database.save_page(d, 'scan-v1', dict(page_number=1, status='succeeded', method='ocr', text='新 OCR'))
            return super().recognize(**kwargs)
    reviewer = review_module().ReviewService(evidence.settings, vision=ChangingVision())
    initial = reviewer.state(p, d, 1)
    candidate = reviewer.recognize(p, d, 1, [0, 0, 1, 1], initial['raw_revision'])
    assert candidate['status'] == 'stale'
    assert reviewer.state(p, d, 1)['adoption'] is None




def test_quality_metrics_report_unknown_and_wrong_adoptions_separately():
    q = importlib.import_module('dd_agent.quality') if __import__('importlib').util.find_spec('dd_agent.quality') else None
    assert q is not None, 'Quality evaluation is not implemented'
    result = q.evaluate_fields([
        {'id':'a', 'category':'handwriting', 'reference':'2014', 'prediction':'2014', 'adopted':True},
        {'id':'b', 'category':'handwriting', 'reference':'2013', 'prediction':'203', 'adopted':True},
        {'id':'c', 'category':'printed', 'reference':'2000万元', 'prediction':'', 'adopted':False},
        {'id':'d', 'category':'stamp', 'reference':None, 'prediction':'猜测内容', 'adopted':False},
    ])
    assert result['readable_fields'] == 3
    assert result['exact_accuracy'] == pytest.approx(1/3)
    assert result['unknown_reference_fields'] == 1
    assert result['wrong_adoptions'] == 1
    assert result['prediction_coverage'] == pytest.approx(2/3)
    assert result['by_category']['handwriting']['correct'] == 1
    assert q.evaluate_fields([])['exact_accuracy'] is None
    unknown_adoption = q.evaluate_fields([{'id':'unknown','category':'stamp','reference':None,'prediction':'猜测','adopted':True}])
    assert unknown_adoption['unverifiable_adoptions'] == 1


def test_higher_resolution_produces_larger_preview_and_a_new_reading_version(tmp_path, database_url):
    assert hasattr(Settings, 'pdf_render_scale'), 'PDF render detail is not configurable'
    import struct
    from dd_agent.reading import Reader
    from test_reading import make_pdf
    from test_workspace import application, upload
    pdf = tmp_path/'source.pdf'
    body = make_pdf(pdf, ['The registered capital is 2000 and establishment date is 2014-01-26.'])
    app = application(tmp_path/'data', database_url, migrate=True, limit=10000)
    with client(app) as c:
        p = project(c); d = upload(c, p, body=body).json()['document']['id']
    default = Reader(Settings(database_url=database_url, data_dir=tmp_path/'data')).read(p, d, [1])
    detail_reader = Reader(Settings(database_url=database_url, data_dir=tmp_path/'data', pdf_render_scale=3))
    detail = detail_reader.read(p, d, [1])
    assert default['pipeline_version'] != detail['pipeline_version']
    assert detail['skipped_pages'] == []
    # 文字页阅读时不渲染，浏览时才按该版本的比例生成预览。
    key = detail_reader.ensure_preview(p, d, 1)
    image = (tmp_path/'data'/key).read_bytes()
    assert struct.unpack('>II', image[16:24]) == (1785,2526)


def test_crop_geometry_maps_normalized_bounds_to_original_pixels(tmp_path):
    import subprocess
    script = Path(__file__).resolve().parents[2]/'src/dd_agent/workers/vision_worker.py'
    code = '''import importlib.util
from PIL import Image
spec=importlib.util.spec_from_file_location('vision_worker',%r)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
im=Image.new('RGB',(100,80),'white');im.putpixel((20,20),(255,0,0))
crop,box=m.crop_region(im,[.2,.25,.7,.75])
assert box==[20,20,70,60]
assert crop.size==(50,40)
assert crop.getpixel((0,0))==(255,0,0)
''' % str(script)
    result = subprocess.run([str(Settings().vision_python), '-c', code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_real_offline_vision_region_with_resource_metadata(tmp_path):
    import os, subprocess
    if os.getenv('DDA_TEST_VISION_MODEL') != '1':
        pytest.skip('Set DDA_TEST_VISION_MODEL=1 for local visual model integration')
    target = tmp_path/'image.png'
    code = "from PIL import Image,ImageDraw,ImageFont; im=Image.new('RGB',(1000,160),'white'); ImageDraw.Draw(im).text((30,40),'Registered capital: 2000',font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',48),fill='black'); im.save(%r)" % str(target)
    subprocess.run([str(Settings().vision_python), '-c', code], check=True, timeout=30)
    settings = Settings(data_dir=tmp_path)
    result = review_module().LocalVision(settings).recognize(image_path=target, bounds=[0,0,1,1], directory=tmp_path)
    assert '2000' in result['text'], result
    assert len(result['model_version']) == 64
    assert result['crop_box'] == [0,0,1000,160]
    assert result['truncated'] is False
    assert result['elapsed_seconds'] > 0 and result['peak_memory_mb'] > 0
