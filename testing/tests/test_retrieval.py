"""Catch stale evidence, cross-project leakage, broken recovery and source offsets."""
import importlib
from uuid import UUID

import pytest

from dd_agent.config import Settings
from test_workspace import application, client, project, upload


def retrieval():
    try:
        return importlib.import_module('dd_agent.retrieval')
    except ModuleNotFoundError:
        pytest.fail('Local evidence indexing and retrieval are not implemented')


class TinyModels:
    """Only replace costly model inference. SQL, ranking orchestration and files stay real."""
    version = 'synthetic-embedding-v1'
    fail = False

    def profile(self):
        return self.version

    def embed(self, texts, query=False):
        if self.fail:
            raise retrieval().RetrievalError('合成模型失败')
        return [[1., 0.] + [0.] * 1022 if '资本' in t else [0., 1.] + [0.] * 1022 for t in texts]

    def rerank(self, query, texts):
        return [0.9 if '资本' in t else 0.1 for t in texts]


def setup(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    with client(app) as c:
        p = project(c)
        d = upload(c, p).json()['document']['id']
    db = app.state.database
    db.set_reading(d, 3, 'scan-v1')
    for n, text in [(1, '注册资本为2000万元。'), (2, '公司住所位于吉林省。')]:
        db.save_page(d, 'scan-v1', dict(page_number=n, status='succeeded', method='ocr', text=text))
    models = TinyModels()
    service = retrieval().EvidenceService(Settings(database_url=database_url, data_dir=tmp_path), models=models)
    return app, p, d, service, models


def test_chunks_preserve_page_source_offsets_without_losing_long_text():
    split = retrieval().split_text
    text = '公司基本情况\n\n' + '注册资本与股东信息。' * 160 + '\n下一段。'
    chunks = split(text)
    assert len(chunks) > 2
    covered = set()
    for chunk in chunks:
        assert 0 < len(chunk['text']) <= 700
        assert chunk['text'] == text[chunk['start_offset']:chunk['end_offset']]
        covered.update(range(chunk['start_offset'], chunk['end_offset']))
    assert all(i in covered for i, char in enumerate(text) if not char.isspace())
    assert split(' \n\t') == []


def test_index_resume_search_sources_and_project_isolation(tmp_path, database_url):
    app, p, d, service, _ = setup(tmp_path, database_url)
    first = service.index(p, d, [1, 2])
    assert first['indexed_pages'] == [1, 2]
    assert service.index(p, d, [1, 2])['skipped_pages'] == [1, 2]
    result = service.search(p, '注册资本')
    assert result['coverage']['indexed_pages'] == 2
    assert result['coverage']['total_pages'] == 3
    assert result['coverage']['complete'] is False
    hit = result['results'][0]
    assert hit['text'] == '注册资本为2000万元。'
    assert hit['page_number'] == 1 and hit['method'] == 'ocr'
    assert hit['verification'] == 'unverified'
    assert hit['filenames'] == ['合同.pdf']
    assert hit['document_id'] == UUID(d)
    with client(app) as c:
        other = project(c, '另一个项目')
    assert service.search(other, '注册资本')['results'] == []
    with pytest.raises(retrieval().RetrievalError, match='材料'):
        service.index(other, d, [1])


def test_page_edits_and_reading_or_model_versions_never_reuse_stale_chunks(tmp_path, database_url):
    app, p, d, service, models = setup(tmp_path, database_url)
    service.index(p, d, [1])
    app.state.database.save_page(d, 'scan-v1', dict(page_number=1, status='succeeded', method='ocr', text='注册资本变更为5000万元。'))
    assert service.search(p, '资本')['results'] == []
    service.index(p, d, [1])
    assert service.search(p, '资本')['results'][0]['text'] == '注册资本变更为5000万元。'
    models.version = 'synthetic-embedding-v2'
    assert service.search(p, '资本')['results'] == []
    service.index(p, d, [1])
    app.state.database.set_reading(d, 3, 'scan-v2')
    assert service.search(p, '资本')['results'] == []


def test_failed_embedding_preserves_other_pages_and_can_resume(tmp_path, database_url):
    _, p, d, service, models = setup(tmp_path, database_url)
    service.index(p, d, [1])
    models.fail = True
    outcome = service.index(p, d, [1, 2])
    assert outcome['skipped_pages'] == [1]
    assert outcome['failed_pages'] == [2]
    models.fail = False
    assert service.search(p, '资本')['coverage']['indexed_pages'] == 1
    assert service.index(p, d, [2])['indexed_pages'] == [2]


def test_blank_unread_and_invalid_vectors_are_not_fabricated_evidence(tmp_path, database_url):
    app, p, d, service, models = setup(tmp_path, database_url)
    app.state.database.save_page(d, 'scan-v1', dict(page_number=2, status='succeeded', method='text', text=' \n'))
    assert service.index(p, d, [2])['indexed_pages'] == [2]
    assert service.search(p, '资本')['results'] == []
    assert service.index(p, d, [3])['failed_pages'] == [3]
    models.embed = lambda texts, query=False: [[float('nan')] * 1024 for _ in texts]
    assert service.index(p, d, [1])['failed_pages'] == [1]
    with pytest.raises(ValueError):
        service.search(p, ' ')


def test_snapshot_changed_during_embedding_is_not_published(tmp_path, database_url):
    app, p, d, service, models = setup(tmp_path, database_url)
    original = models.embed
    def changed(texts, query=False):
        app.state.database.save_page(d, 'scan-v1', dict(page_number=1, status='succeeded', method='ocr', text='新修订'))
        return original(texts, query=query)
    models.embed = changed
    assert service.index(p, d, [1])['failed_pages'] == [1]
    assert service.search(p, '资本')['results'] == []


def test_search_api_validation_source_link_and_empty_state(tmp_path, database_url):
    app, p, d, service, _ = setup(tmp_path, database_url)
    service.index(p, d, [1])
    app.state.evidence = service
    with client(app) as c:
        route = f'/api/projects/{p}/search'
        response = c.post(route, json={'query': '资本'})
        assert response.status_code == 200
        hit = response.json()['results'][0]
        assert f'document={d}' in hit['source_url'] and 'page=1' in hit['source_url']
        assert c.post(route, json={'query': ' '}).status_code == 422
        assert c.post(route, json={'query': 'a' * 501}).status_code == 422
        assert c.get('/search').status_code == 200
        other = project(c, '空项目')
        assert c.post(f'/api/projects/{other}/search', json={'query': '资本'}).json()['status'] == 'not_indexed'


def test_page_changed_during_reranking_is_not_returned_as_current_evidence(tmp_path, database_url):
    app, p, d, service, models = setup(tmp_path, database_url)
    service.index(p, d, [1])
    def update_during_rerank(query, texts):
        app.state.database.save_page(d, 'scan-v1', dict(page_number=1, status='succeeded', method='ocr', text='修订后的内容'))
        return [0.9] * len(texts)
    models.rerank = update_during_rerank
    assert service.search(p, '资本')['results'] == []


def test_shared_model_slot_returns_busy_instead_of_loading_twice(tmp_path):
    m = retrieval()
    with m.model_slot(tmp_path):
        with pytest.raises(m.RetrievalError, match='其他任务'):
            with m.model_slot(tmp_path):
                pytest.fail('A second model operation acquired the shared slot')


def test_worker_failure_after_first_output_is_not_accepted_as_success(tmp_path, monkeypatch):
    m = retrieval()
    def failed_worker(*args):
        yield {'version': 'uncommitted-worker-output'}
        raise m.ReadingError('worker exited unsuccessfully after emitting output')
    monkeypatch.setattr(m, 'worker_records', failed_worker)
    models = m.LocalModels(Settings(data_dir=tmp_path))
    # verify=True 绕过进程内指纹缓存。--models 里前面的真模型用例会把同一权重路径的摘要留在内存里，
    # 普通 profile() 会直接命中，不再进入 worker。这里要测的就是 worker 先吐一行再失败。
    with pytest.raises(m.RetrievalError):
        models.profile(verify=True)


def test_real_offline_qwen_pgvector_roundtrip(tmp_path, database_url):
    import os
    import json
    from dd_agent.performance import capture
    from dd_agent.model_runtime import close_runtime
    if os.getenv('DDA_TEST_LOCAL_MODELS') != '1':
        pytest.skip('Set DDA_TEST_LOCAL_MODELS=1 for local model integration')
    _, p, d, service, _ = setup(tmp_path, database_url)
    service.models = retrieval().LocalModels(service.settings)
    try:
        outcome = service.index(p, d, [1, 2])
        assert outcome['failed_pages'] == [], outcome
        measurements=[]
        for label in ('cold','cached'):
            folder=tmp_path/label
            with capture(folder,'test',sample_memory=True):
                result = service.search(p, '公司的注册资本是多少？')
            assert result['results'][0]['text'] == '注册资本为2000万元。'
            assert result['coverage']['indexed_pages'] == 2
            measurements.append(json.loads(next(folder.glob('*.json')).read_text(encoding='utf-8')))
        counters=measurements[1]['counters']
        assert counters['search.embedding_cache.hit']==1
        assert counters['search.reranker_cache.hit']==1
        assert counters.get('runtime.requests',0)==0
        assert counters['sql.calls']==7
    finally:close_runtime()
