"""RG-05b／K01：指纹与预检缓存的命中、失效、损坏、并发与空输入发布。"""
import json
import threading
from pathlib import Path

import pytest

from dd_agent import cache


def weights(tmp_path, name='model.safetensors', content=b'weights-v1'):
    target = tmp_path / name
    target.write_bytes(content)
    return target


def forget():
    """只清内存加速层；磁盘缓存仍在，用于验证“内存不是恢复依据”。"""
    with cache._guard:
        cache._memory.clear()


def test_reuse_skips_recomputation_and_verify_forces_full_revalidation(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path)
    calls = []

    def compute():
        calls.append(1)
        return model.read_bytes().hex()

    parameters = dict(scale=2)
    first = cache.resolve_fingerprint('kind_a', 'runtime-1', 'alg-v1', parameters,
                                      cache.file_entries([model]), compute, directory=directory)
    assert first == dict(value=model.read_bytes().hex(), cache_hit=False, validation_mode='full',
                         manifest_revision=first['manifest_revision'], key=first['key'])
    second = cache.resolve_fingerprint('kind_a', 'runtime-1', 'alg-v1', parameters,
                                       cache.file_entries([model]), compute, directory=directory)
    assert second['cache_hit'] is True and second['validation_mode'] == 'metadata'
    assert second['value'] == first['value'] and len(calls) == 1

    strict = cache.resolve_fingerprint('kind_a', 'runtime-1', 'alg-v1', parameters,
                                       cache.file_entries([model]), compute, verify=True, directory=directory)
    assert strict['cache_hit'] is False and strict['validation_mode'] == 'full' and len(calls) == 2


def test_file_added_changed_removed_or_renamed_invalidates_cache(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path)
    calls = []

    def compute():
        calls.append(1)
        return str(len(calls))

    def resolve():
        return cache.resolve_fingerprint('kind_b', 'rt', 'alg', {}, cache.file_entries(
            sorted(p for p in tmp_path.glob('*.safetensors'))), compute, directory=directory)

    assert resolve()['cache_hit'] is False
    assert resolve()['cache_hit'] is True
    model.write_bytes(b'weights-v2-longer')                # 内容改变（长度／摘要同时变化）
    assert resolve()['cache_hit'] is False
    assert resolve()['cache_hit'] is True
    weights(tmp_path, 'extra.safetensors', b'more')        # 新增文件
    assert resolve()['cache_hit'] is False
    assert resolve()['cache_hit'] is True
    (tmp_path / 'extra.safetensors').unlink()              # 删除文件回到此前的清单
    # 清单回到早先状态即命中该清单当时完整校验过的结果；这不是脏读，而是同清单同结果。
    assert resolve()['cache_hit'] is True
    missing = cache.file_entries([tmp_path / 'gone.safetensors'])
    assert missing[0]['missing'] is True                   # 缺失也计入清单，不静默漏检


def test_parameter_algorithm_or_runtime_change_uses_a_different_key(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path)
    entries = cache.file_entries([model])

    def resolve(**overrides):
        payload = dict(kind='kind_c', runtime_descriptor='runtime-1', algorithm_tag='alg-v1',
                       parameters=dict(x=1), entries=entries, directory=directory)
        payload.update(overrides)
        return cache.resolve_fingerprint(payload.pop('kind'), payload.pop('runtime_descriptor'),
                                         payload.pop('algorithm_tag'), payload.pop('parameters'),
                                         payload.pop('entries'), lambda: 'v', **payload)

    base = resolve()
    assert resolve()['key'] == base['key']
    assert resolve(parameters=dict(x=2))['key'] != base['key']
    assert resolve(algorithm_tag='alg-v2')['key'] != base['key']
    assert resolve(runtime_descriptor='runtime-2')['key'] != base['key']
    assert resolve(kind='other')['key'] != base['key']


def test_unknown_runtime_descriptor_disables_reuse(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path)
    calls = []

    def compute():
        calls.append(1)
        return 'v'

    entries = cache.file_entries([model])
    # 无法绑定解释器／依赖版本时不做可信复用：每次都完整校验。
    for _ in range(3):
        outcome = cache.resolve_fingerprint('kind_d', None, 'alg', {}, entries, compute, directory=directory)
        assert outcome['cache_hit'] is False and outcome['validation_mode'] == 'full'
    assert len(calls) == 3


def test_corrupted_or_foreign_cache_entries_fall_back_to_recomputation(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path)
    entries = cache.file_entries([model])
    calls = []

    def compute():
        calls.append(1)
        return 'value-' + str(len(calls))

    def resolve():
        return cache.resolve_fingerprint('kind_e', 'rt', 'alg', {}, entries, compute, directory=directory)

    stored = resolve()
    files = list((directory / 'fingerprints' / 'kind_e').glob('*.json'))
    assert len(files) == 1 and json.loads(files[0].read_text())['validation'] == 'full'
    # 内存不是恢复依据：清掉内存后仍应能从磁盘命中；磁盘损坏则退回复算。
    forget()
    assert resolve()['cache_hit'] is True and len(calls) == 1
    for broken in ['{not json', json.dumps({'cache_version': 999, 'key': stored['key'], 'validation': 'full', 'value': 'x'}),
                   json.dumps({'cache_version': cache.CACHE_VERSION, 'key': stored['key'], 'validation': 'metadata', 'value': 'x'}),
                   json.dumps({'cache_version': cache.CACHE_VERSION, 'key': 'other', 'validation': 'full', 'value': 'x'})]:
        files[0].write_text(broken, encoding='utf-8')
        forget()
        outcome = resolve()
        assert outcome['cache_hit'] is False and outcome['validation_mode'] == 'full'
    assert len(calls) == 5


def test_concurrent_misses_compute_only_once(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path)
    entries = cache.file_entries([model])
    started = threading.Event()
    calls = []

    def compute():
        calls.append(1)
        started.set()
        return 'shared-value'

    outcomes = []
    def worker():
        outcomes.append(cache.resolve_fingerprint('kind_f', 'rt', 'alg', {}, entries, compute, directory=directory))
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads: thread.start()
    assert started.wait(5)
    for thread in threads: thread.join(10)
    assert len(calls) == 1
    assert len(outcomes) == 6 and all(o['value'] == 'shared-value' for o in outcomes)


def test_identical_metadata_is_not_content_proof_but_verify_detects_rewrite(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path, content=b'AAAA')
    entries = cache.file_entries([model])          # 快检清单被上层固定：模拟元数据未变
    compute = lambda: model.read_bytes().hex()

    first = cache.resolve_fingerprint('kind_g', 'rt', 'alg', {}, entries, compute, directory=directory)
    model.write_bytes(b'BBBB')                     # 同长度原地改写
    reused = cache.resolve_fingerprint('kind_g', 'rt', 'alg', {}, entries, compute, directory=directory)
    # 已知且必须明示的限制：清单未变时快检无法发现改写，不能声称它等价于内容哈希。
    assert reused['validation_mode'] == 'metadata' and reused['value'] == first['value']
    strict = cache.resolve_fingerprint('kind_g', 'rt', 'alg', {}, cache.file_entries([model]), compute,
                                       verify=True, directory=directory)
    assert strict['validation_mode'] == 'full' and strict['value'] != first['value']


def test_preflight_cache_requires_identity_and_strictly_revalidates(tmp_path):
    directory = tmp_path / 'cache'
    source = tmp_path / 'contract.pdf'
    source.write_bytes(b'%PDF-1.7 v1')
    calls = []

    def compute():
        calls.append(1)
        return {'page_count': 3}

    def resolve(verify=False, payload=None):
        # 只有内容真的变化才重写；否则改动的 mtime 会让清单变化、缓存自然失效。
        if payload is not None and source.read_bytes() != payload:
            source.write_bytes(payload)
        return cache.inspect_document('doc-1', 'sha-1', 'parser-v1', source, compute,
                                      verify=verify, directory=directory)

    first = resolve()
    assert first['cache_hit'] is False
    assert resolve()['cache_hit'] is True and len(calls) == 1
    forget()
    assert resolve()['cache_hit'] is True and len(calls) == 1     # 跨“进程”由磁盘承担
    assert resolve(verify=True)['validation_mode'] == 'full' and len(calls) == 2
    # 缺少材料身份或原件摘要时不缓存（例如 CLI 诊断路径）。
    anonymous = cache.inspect_document(None, None, 'parser-v1', source, compute, directory=directory)
    assert anonymous['cache_hit'] is False and anonymous['key'] == '' and len(calls) == 3
    assert cache.inspect_document('doc-1', None, 'parser-v1', source, compute, directory=directory)['cache_hit'] is False
    # 原件内容变化后清单随之变化，旧结果不再复用。
    assert resolve(payload=b'%PDF-1.7 v2 with more bytes')['cache_hit'] is False


def test_fingerprint_and_preflight_land_in_documented_directories(tmp_path):
    directory = tmp_path / 'cache'
    model = weights(tmp_path)
    cache.resolve_fingerprint('ocr_weights', 'rt', 'alg', {}, cache.file_entries([model]),
                              lambda: 'v', directory=directory)
    source = weights(tmp_path, 'doc.pdf', b'x')
    cache.inspect_document('doc-1', 'sha-1', 'parser-v1', source, lambda: {}, directory=directory)
    assert (directory / 'fingerprints' / 'ocr_weights').is_dir()
    assert (directory / 'preflight' / 'preflight').is_dir()
    assert not list(directory.glob('**/*.tmp-*'))          # 临时文件已原子替换


def test_empty_batch_publishes_zero_chunks_without_embedding(tmp_path, database_url):
    from test_retrieval import setup
    app, project_id, document_id, service, models = setup(tmp_path, database_url)
    app.state.database.save_page(document_id, 'scan-v1',
                                 dict(page_number=3, status='succeeded', method='text', text=' \n\t'))
    calls = []
    original = models.embed
    models.embed = lambda texts, query=False: (calls.append(len(texts)), original(texts, query=query))[1]

    outcome = service.index(project_id, document_id, [3])
    assert outcome['indexed_pages'] == [3] and outcome['failed_pages'] == []
    assert calls == []                                     # 空输入不加载／不调用 Embedding（PF-08）
    assert service.index(project_id, document_id, [3])['skipped_pages'] == [3]
    assert service.search(project_id, '资本')['coverage']['indexed_pages'] == 1
    assert service.search(project_id, '资本')['results'] == []   # 零片段页不等于可检索证据


def test_mixed_batch_only_embeds_pages_with_chunks(tmp_path, database_url):
    from test_retrieval import setup
    app, project_id, document_id, service, models = setup(tmp_path, database_url)
    app.state.database.save_page(document_id, 'scan-v1',
                                 dict(page_number=3, status='succeeded', method='text', text=' \n'))
    calls = []
    original = models.embed
    models.embed = lambda texts, query=False: (calls.append(len(texts)), original(texts, query=query))[1]

    outcome = service.index(project_id, document_id, [1, 3])
    assert outcome['indexed_pages'] == [1, 3] and outcome['failed_pages'] == []
    assert calls == [1]                                    # 只向量化非空页（页码仍按升序返回）
    assert service.search(project_id, '资本')['coverage']['indexed_pages'] == 2
