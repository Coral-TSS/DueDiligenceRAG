"""设备、视觉节流和先索引后增强的调度。不加载真实模型，也不要求 GPU。"""
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from dd_agent.config import Settings
from dd_agent.devices import describe_paddle, describe_torch
from dd_agent.performance import capture
from dd_agent.pipeline import ProcessingPipeline


def test_torch_auto_keeps_pascal_on_float32_and_uses_bf16_from_ada():
    pascal = describe_torch('auto', 'auto', cuda_available=True, capability=(6, 1), bf16_supported=False)
    assert pascal['device'] == 'cuda' and pascal['dtype'] == 'float32' and pascal['attention'] == 'sdpa'
    ada = describe_torch('auto', 'auto', cuda_available=True, capability=(8, 9), bf16_supported=True)
    assert ada['dtype'] == 'bfloat16'
    ada_fp16 = describe_torch('auto', 'auto', cuda_available=True, capability=(8, 9), bf16_supported=False)
    assert ada_fp16['dtype'] == 'float16'
    cpu = describe_torch('auto', 'auto', cuda_available=False, capability=None, bf16_supported=False)
    assert cpu['device'] == 'cpu' and cpu['dtype'] == 'float32'
    # 显式 bf16 在不支持的卡上回退，避免加载直接失败；显式 fp16 在 GPU 上保留。
    assert describe_torch('cuda', 'bfloat16', cuda_available=True, capability=(6, 1), bf16_supported=False)['dtype'] == 'float32'
    assert describe_torch('cuda', 'float16', cuda_available=True, capability=(6, 1), bf16_supported=False)['dtype'] == 'float16'
    assert describe_torch('cpu', 'float16', cuda_available=True, capability=(8, 9), bf16_supported=True)['device'] == 'cpu'
    assert describe_torch('cuda', 'auto', cuda_available=False, capability=None, bf16_supported=False)['notes'] == ['cuda_unavailable']


def test_paddle_gpu_omits_cpu_kwargs_and_mkldnn_stays_off_unless_asked():
    from dd_agent.devices import paddle_runtime_kwargs
    assert describe_paddle('auto', compiled_with_cuda=False, device_count=0, enable_mkldnn=False, threads=4)['device'] == 'cpu'
    gpu = describe_paddle('auto', compiled_with_cuda=True, device_count=1, enable_mkldnn=True, threads=6)
    assert gpu['device'] == 'gpu:0' and gpu['mkldnn'] is False and 'mkldnn_cpu_only' in gpu['notes']
    cpu = describe_paddle('cpu', compiled_with_cuda=True, device_count=1, enable_mkldnn=True, threads=6)
    assert cpu['device'] == 'cpu' and cpu['mkldnn'] is True and cpu['threads'] == 6
    missing = describe_paddle('gpu:0', compiled_with_cuda=False, device_count=0, enable_mkldnn=False, threads=4)
    assert missing['device'] == 'cpu' and 'gpu_unavailable' in missing['notes']
    gpu_kwargs = paddle_runtime_kwargs(gpu)
    assert gpu_kwargs == {'device': 'gpu:0'}
    assert 'cpu_threads' not in gpu_kwargs and 'enable_mkldnn' not in gpu_kwargs
    assert paddle_runtime_kwargs(cpu) == {'device': 'cpu', 'cpu_threads': 6, 'enable_mkldnn': True}


@pytest.mark.parametrize('kwargs', [
    {'ocr_device': 'cuda'},
    {'vision_device': 'gpu:0'},
    {'vision_dtype': 'fp16'},
    {'vision_max_new_tokens': 8},
    {'vision_timeout_budget': 51},
    {'vision_low_score': 1.5},
    {'ocr_cpu_threads': 32},
])
def test_invalid_device_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_environment_loads_performance_settings(monkeypatch):
    monkeypatch.setattr('dd_agent.config.load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setenv('DDA_OCR_DEVICE', 'gpu:0')
    monkeypatch.setenv('DDA_VISION_DEVICE', 'cuda')
    monkeypatch.setenv('DDA_EMBEDDING_DTYPE', 'float32')
    monkeypatch.setenv('DDA_RERANKER_DTYPE', 'bfloat16')
    monkeypatch.setenv('DDA_VISION_MAX_NEW_TOKENS', '96')
    monkeypatch.setenv('DDA_VISION_REGIONS_PER_PAGE', '2')
    monkeypatch.setenv('DDA_VISION_LOW_SCORE', '0.6')
    monkeypatch.setenv('DDA_VISION_TIMEOUT_BUDGET', '1')
    monkeypatch.setenv('DDA_OCR_ENABLE_MKLDNN', 'yes')
    monkeypatch.setenv('DDA_OCR_CPU_THREADS', '6')
    settings = Settings.from_env()
    assert settings.ocr_device == 'gpu:0' and settings.vision_device == 'cuda'
    assert settings.embedding_dtype == 'float32' and settings.reranker_dtype == 'bfloat16'
    assert settings.vision_max_new_tokens == 96 and settings.vision_regions_per_page == 2
    assert settings.vision_low_score == 0.6 and settings.vision_timeout_budget == 1
    assert settings.ocr_enable_mkldnn is True and settings.ocr_cpu_threads == 6


def test_defaults_keep_fast_vision_and_fewer_regions():
    settings = Settings()
    assert settings.vision_regions_per_page == 1
    assert settings.vision_max_new_tokens == 128
    assert settings.ocr_enable_mkldnn is False
    assert settings.ocr_device == 'auto' and settings.vision_dtype == 'auto'
    assert not hasattr(settings, 'ingest_schedule')
    assert not hasattr(settings, 'vision_use_cache')


def test_device_and_cache_do_not_change_historical_version_stamps():
    from dd_agent.reading import PIPELINE_VERSION
    from dd_agent.automatic import SELECTION_VERSION
    assert PIPELINE_VERSION == 'pdfium-text-v2-ppocr5-cpu-v1'
    # 选择规则变了才会换增强版本；设备、注意力和 KV cache 没有单独开关。
    assert 'selective' in SELECTION_VERSION


def test_keyword_page_does_not_enhance_every_line_or_the_whole_page(tmp_path):
    import struct
    from dd_agent.automatic import line_priority, regions_for, route_page
    assert route_page({'method': 'ocr', 'text': '注册资本2000万元', 'lines': []})['needs_vision']
    clear = {'method': 'ocr', 'text': '法定代表人：张三', 'lines': [{'text': '法定代表人：张三', 'score': 0.99}]}
    routed = route_page(clear)
    assert routed['important'] and not routed['needs_vision']
    low = {'method': 'ocr', 'text': '法定代表人：张三', 'lines': [{'text': '法定代表人：张三', 'score': 0.4}]}
    assert route_page(low)['needs_vision']
    assert line_priority({'text': '自2013年11月05日至203年11月25日', 'score': 0.99}, True, 0.85) > 0
    assert line_priority({'text': '普通说明', 'score': 0.99}, True, 0.85) == 0
    image = tmp_path / 'page.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 8 + struct.pack('>II', 1000, 2000))
    lines = [dict(text='企业名称：甲公司', score=0.99, box=[[10, 10], [900, 10], [900, 40], [10, 40]]),
             dict(text='本节无数字', score=0.99, box=[[10, 80], [900, 80], [900, 110], [10, 110]])]
    page = dict(method='ocr', text='\n'.join(item['text'] for item in lines), lines=lines)
    route = route_page(page)
    assert route['important'] and not route['needs_vision']
    assert regions_for(page, route, image, limit=1) == []
    dated = [dict(text='名称预先核准', score=0.99, box=[[10, 10], [400, 10], [400, 40], [10, 40]]),
             dict(text='自2013年11月05日至203年11月25日', score=0.99, box=[[10, 80], [800, 80], [800, 120], [10, 120]])]
    dated_page = dict(method='ocr', text='\n'.join(item['text'] for item in dated), lines=dated)
    dated_route = route_page(dated_page)
    regions = regions_for(dated_page, dated_route, image, limit=1)
    assert len(regions) == 1 and not regions[0]['over_budget']
    assert regions[0]['bounds'] != [0.0, 0.0, 1.0, 1.0]


def test_page_stage_events_keep_page_and_region_but_not_text(tmp_path):
    from dd_agent.performance import span
    with capture(tmp_path, 'trial'):
        with span('page.ocr', page=2):
            pass
        with span('page.vision.region', page=2, region=0):
            pass
        capture_trace = capture.__wrapped__ if hasattr(capture, '__wrapped__') else None
    del capture_trace
    from dd_agent.performance import active
    assert not active()
    import json
    value = json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))
    assert value['events'][0]['stage'] == 'page.ocr' and value['events'][0]['page'] == 2
    assert value['events'][1]['region'] == 0 and 'text' not in value['events'][1]
    # 直接塞进事件的正文仍被丢掉。
    from dd_agent.performance import Trace
    trace = Trace(tmp_path / 'more', 'trial', 8, False)
    trace.event(dict(stage='page.embedding', seconds=1.25, status='ok', page=4, region=1, text='注册资本密文'))
    trace.finish('ok')
    stored = json.loads(next((tmp_path / 'more').glob('*.json')).read_text(encoding='utf-8'))
    assert stored['events'][0]['page'] == 4 and '注册资本密文' not in json.dumps(stored, ensure_ascii=False)


def test_process_pages_order_indexes_before_vision_then_refreshes():
    order = []

    class Reader:
        def read(self, *args, **kwargs):
            order.append('reading')
            return {}

    class Automatic:
        def process(self, *args, **kwargs):
            order.append('vision')
            return {'failed_pages': []}

    class Evidence:
        def index(self, *args, **kwargs):
            order.append('embedding')
            return {'failed_pages': []}

    pipeline = ProcessingPipeline(Settings(), reader=Reader(), automatic=Automatic(),
                                  evidence=Evidence(), database=SimpleNamespace())
    pipeline.run('project', 'document', [1, 2])
    assert order == ['reading', 'embedding', 'vision', 'embedding']


def _scheduler():
    from dd_agent.stage_scheduler import StageScheduler
    scheduler = StageScheduler.__new__(StageScheduler)
    scheduler.settings = Settings()
    scheduler.lock = RLock()
    scheduler.reader_done = Event()
    scheduler.states = {name: {} for name in ('reading', 'vision', 'embedding')}
    scheduler.paused = []
    scheduler.parent_counts = {name: 0 for name in ('pending', 'running', 'succeeded', 'failed', 'cancelled')}
    scheduler._vision_pass = False
    return scheduler


def test_vision_waits_until_ocr_and_embedding_settle():
    scheduler = _scheduler()
    scheduler.states['vision'][1] = 'pending'
    scheduler.states['embedding'][1] = 'pending'
    assert scheduler._deferred_phase() == 'embed'
    scheduler.reader_done.set()
    scheduler.states['reading'][1] = 'succeeded'
    assert scheduler._deferred_phase() == 'embed'
    scheduler.states['embedding'][1] = 'succeeded'
    assert scheduler._deferred_phase() == 'vision'
    # 视觉写回会重新挂上向量任务，这一轮仍先把视觉做完，避免来回加载。
    scheduler.states['embedding'][1] = 'pending'
    assert scheduler._deferred_phase() == 'vision'
    scheduler.states['vision'][1] = 'succeeded'
    assert scheduler._deferred_phase() == 'embed'
    assert not scheduler._deferred_settled()
    scheduler.states['embedding'][1] = 'succeeded'
    assert scheduler._deferred_settled()
