"""视觉阈值、批预算、渲染复用、权重清单和双轻模型驻留。不加载真实模型。"""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dd_agent.automatic import line_priority, regions_for, route_page, selection_report, AutomaticProcessor
from dd_agent.batching import pack_ranges
from dd_agent.cache import verified_content_digest
from dd_agent.config import Settings
from dd_agent.maintenance import inventory, rotate_log
from dd_agent.model_runtime import residence_action
from dd_agent.reading import render_plan
from dd_agent.retrieval import parse_stored_vector, reuse_vectors
from dd_agent.workers.vision_worker import group_items


def test_pack_ranges_keeps_one_long_item_and_respects_budget():
    assert pack_ranges([100, 100, 50, 3000, 10], 200, 4) == [(0, 2), (2, 3), (3, 4), (4, 5)]
    assert pack_ranges([10, 10, 10], 1000, 2) == [(0, 2), (2, 3)]
    with pytest.raises(ValueError):
        pack_ranges([1], 0, 1)


def test_default_line_rules_stay_and_thresholds_can_exclude_value_or_keyword_lines():
    dated = {'text': '自2013年11月05日至203年11月25日', 'score': 0.99}
    amount = {'text': '注册资本2000万元', 'score': 0.99}
    low = {'text': '法定代表人：张三', 'score': 0.4}
    assert line_priority(dated, True, 0.85) == 3
    assert line_priority(amount, True, 0.85) == 2
    assert line_priority(low, False, 0.85) == 1
    assert line_priority(dated, True, 0.85, value_lines=False) == 0
    # 金额行同时符合“关键词+数字”和“金额”。只收紧关键词上限时，它仍按金额行入选。
    assert line_priority(amount, True, 0.85, keyword_max_score=0.5) == 3
    assert line_priority(amount, True, 0.85, keyword_max_score=0.5, value_lines=False) == 0
    assert line_priority(dated, True, 0.85, value_max_score=0.5) == 0
    assert line_priority({'text': '注册资本2000万元'}, True, 0.85, keyword_max_score=0.5) == 2
    page = {'method': 'ocr', 'text': '名称预先核准\n' + dated['text'],
            'lines': [{'text': '名称预先核准', 'score': 0.99}, dated]}
    assert route_page(page)['needs_vision']
    assert not route_page(page, value_lines=False)['needs_vision']


def test_regions_record_why_they_were_selected(tmp_path):
    import struct
    image = tmp_path / 'page.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 8 + struct.pack('>II', 1000, 2000))
    lines = []
    for index in range(16):
        if index == 1:
            text, score = '自2013年11月05日至203年11月25日', 0.4
        elif index == 12:
            text, score = '注册资本2000万元', 0.99
        else:
            text, score = f'普通文字{index}', 0.99
        lines.append(dict(text=text, score=score, box=[[10, 10 + index * 100], [900, 10 + index * 100], [900, 50 + index * 100], [10, 50 + index * 100]]))
    page = dict(method='ocr', text='\n'.join(item['text'] for item in lines), lines=lines)
    route = route_page(page)
    tokens = dict(low=64, keyword_digit=128, value=96, poor=128)
    regions = regions_for(page, route, image, limit=1, tokens=tokens)
    assert regions[0]['select_reason'] == 'low' and regions[0]['max_new_tokens'] == 64
    assert regions[0]['over_budget'] is False and regions[1]['over_budget'] is True
    report = selection_report(True, regions)
    assert report['triggered'] and report['selected'] == 1 and report['not_selected'] == 1
    assert report['reasons'] == {'low': 1}


def test_token_limits_change_the_machine_version_but_batch_size_does_not():
    class Vision:
        def profile(self):
            return 'fixture'
    base = AutomaticProcessor(Settings(), vision=Vision()).version()
    shorter = AutomaticProcessor(replace(Settings(), vision_tokens_low=80), vision=Vision()).version()
    batched = AutomaticProcessor(replace(Settings(), vision_batch=4), vision=Vision()).version()
    assert base != shorter and base == batched


def test_group_items_batches_by_token_budget_and_keeps_order():
    items = [{'max_new_tokens': 64}, {'max_new_tokens': 128}, {'max_new_tokens': 64}, {'max_new_tokens': 64}]
    assert group_items(items, 128, 2) == [(64, [0, 2]), (64, [3]), (128, [1])]
    with pytest.raises(ValueError):
        group_items([{'max_new_tokens': 200}], 128, 2)


def test_weight_manifest_reuses_digest_until_size_changes(tmp_path):
    weight = tmp_path / 'model.safetensors'
    weight.write_bytes(b'abc' * 1000)
    prefix = b'qwen-last-token-normalized-fp32-v1|marker'
    labeled = [(b'model.safetensors', weight)]
    store = tmp_path / 'cache'
    first = verified_content_digest(prefix, labeled, store, 'embedding')
    manual = hashlib.sha256(prefix + b'model.safetensors' + weight.read_bytes()).hexdigest()
    assert first == manual
    assert verified_content_digest(prefix, labeled, store, 'embedding') == first
    assert len(list((store / 'weight-manifests').rglob('*.json'))) == 1
    weight.write_bytes(b'abcd' * 1000)
    changed = verified_content_digest(prefix, labeled, store, 'embedding')
    assert changed != first
    assert verified_content_digest(prefix, labeled, None, 'embedding') == changed


def test_unchanged_chunks_reuse_vectors_and_changed_text_is_embedded():
    stored = [('甲公司', [0.1, 0.2]), ('乙公司', [0.3, 0.4])]
    chunks = [dict(text='甲公司'), dict(text='丙公司'), dict(text='甲公司')]
    vectors, missing, slots = reuse_vectors(chunks, stored)
    assert missing == ['丙公司'] and slots == [1]
    assert vectors[0] == vectors[2] == [0.1, 0.2] and vectors[1] is None
    assert parse_stored_vector('[0.5, 1]') == [0.5, 1.0]
    assert parse_stored_vector([1, 2]) == [1.0, 2.0]


def test_render_plan_reuses_ocr_images_and_still_renders_text_pages(tmp_path):
    ready = tmp_path / 'page-1.png'
    ready.write_bytes(b'png')
    exists = lambda number: number == 1 and ready.is_file()
    reuse, render = render_plan([1, 2, 3], {1: 'ocr', 2: 'text', 3: 'ocr'}, exists, False)
    assert reuse == [1] and render == [2, 3]
    reuse, render = render_plan([2], {2: 'text'}, lambda number: True, True)
    assert reuse == [2] and render == []


def test_inventory_lists_sizes_without_deleting(tmp_path):
    (tmp_path / 'readings').mkdir()
    (tmp_path / 'readings' / 'a.bin').write_bytes(b'12345')
    (tmp_path / 'note.txt').write_bytes(b'ab')
    report = inventory(tmp_path)
    assert report['total_bytes'] == 7
    assert {item['name'] for item in report['entries']} == {'readings', 'note.txt'}
    assert (tmp_path / 'readings' / 'a.bin').is_file()


def test_log_rotation_keeps_one_backup(tmp_path):
    path = tmp_path / 'model-runtime.log'
    path.write_bytes(b'a' * 20)
    assert rotate_log(path, limit=100) is False and path.read_bytes() == b'a' * 20
    path.write_bytes(b'b' * 120)
    assert rotate_log(path, limit=100) is True
    assert not path.exists() and (tmp_path / 'model-runtime.log.1').read_bytes() == b'b' * 120
    path.write_bytes(b'c' * 120)
    assert rotate_log(path, limit=100) is True
    assert (tmp_path / 'model-runtime.log.1').read_bytes() == b'c' * 120


def test_light_models_stay_together_and_heavy_models_take_the_only_slot():
    assert residence_action('embedding', resident_kind=None, partner_kind=None, resident_same=False, partner_same=False)['load'] == 'resident'
    second = residence_action('reranker', resident_kind='embedding', partner_kind=None, resident_same=False, partner_same=False)
    assert second == {'reuse': None, 'discard': [], 'load': 'partner'}
    again = residence_action('embedding', resident_kind='embedding', partner_kind='reranker', resident_same=True, partner_same=False)
    assert again['reuse'] == 'resident' and again['discard'] == []
    vision = residence_action('vision', resident_kind='embedding', partner_kind='reranker', resident_same=False, partner_same=False)
    assert vision['discard'] == ['partner', 'resident'] and vision['load'] == 'resident'
    replaced = residence_action('embedding', resident_kind='embedding', partner_kind='reranker', resident_same=False, partner_same=False)
    assert replaced['discard'] == ['resident'] and replaced['load'] == 'resident'


def test_ocr_page_batch_falls_back_when_predict_rejects_a_list(tmp_path):
    from dd_agent.workers.ocr_worker import recognize_pages
    image = tmp_path / 'a.png'
    image.write_bytes(b'png')
    calls = []

    class Fake:
        def predict(self, value):
            calls.append(value)
            if isinstance(value, list):
                raise TypeError('list')
            return [SimpleNamespace(json={'res': {'rec_texts': ['甲'], 'rec_scores': [0.9], 'rec_polys': [[[0, 0], [1, 0], [1, 1], [0, 1]]]}},
                                  get=lambda *_args, **_kwargs: {})]
    pages = [{'page_number': 1, 'image_path': str(image)}, {'page_number': 2, 'image_path': str(image)}]
    results = list(recognize_pages(Fake(), {'pages': pages, 'page_batch': 2}))
    assert [item['page_number'] for item in results] == [1, 2]
    assert any(isinstance(item, list) for item in calls)
    assert all(item.get('payload', {}).get('rec_texts') == ['甲'] for item in results)


def test_ocr_page_batch_keeps_predict_order(tmp_path):
    from dd_agent.workers.ocr_worker import recognize_pages
    image = tmp_path / 'a.png'
    image.write_bytes(b'png')

    class Fake:
        def predict(self, value):
            paths = value if isinstance(value, list) else [value]
            return [SimpleNamespace(json={'res': {'rec_texts': [path[-5:-4]], 'rec_scores': [0.9],
                                         'rec_polys': [[[0, 0], [1, 0], [1, 1], [0, 1]]]}},
                                    get=lambda *_args, **_kwargs: {}) for path in paths]
    first, second = tmp_path / '1.png', tmp_path / '2.png'
    first.write_bytes(b'1'); second.write_bytes(b'2')
    results = list(recognize_pages(Fake(), {'pages': [
        {'page_number': 1, 'image_path': str(first)}, {'page_number': 2, 'image_path': str(second)}], 'page_batch': 4}))
    assert [item['payload']['rec_texts'][0] for item in results] == ['1', '2']
    json.dumps(results)
