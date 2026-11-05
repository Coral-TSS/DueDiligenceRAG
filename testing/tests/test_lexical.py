"""关键词召回、与向量融合、单路开关，以及旧片段补建全文索引。"""
import sys
from dataclasses import replace

import pytest

from dd_agent.config import Settings
from dd_agent.lexical import backfill_lexemes, build_keyword_query, cn_to_int, document_tokens, fuse_candidates
from test_retrieval import setup
from test_search_path import CountingModels


CODE = '91310000MA1FL8XH3B'


def test_exact_legal_tokens_and_question_phrases():
    text = '股东张三认缴。统一社会信用代码91310000MA1FL8XH3B，注册资本为2,000万元，于2024年3月15日依据第十二条签订。'
    tokens = document_tokens(text)
    assert '张三' in tokens
    assert CODE.lower() in tokens
    assert 'amt_2000wan' in tokens
    assert 'date_20240315' in tokens and 'date_202403' in tokens
    assert 'clause_12' in tokens
    assert '注册' in tokens and '册资' in tokens and '资本' in tokens
    assert CODE.lower() in document_tokens('代码：' + '９１３１００００ＭＡ１ＦＬ８ＸＨ３Ｂ')
    assert 'date_20240315' in document_tokens('2024-03-15')
    assert 'date_20240315' in document_tokens('二〇二四年三月十五日')
    assert 'clause_12' in document_tokens('第12条')
    assert 'amt_2000wan' in document_tokens('人民币2000万')
    assert cn_to_int('十二') == 12 and cn_to_int('二十') == 20 and cn_to_int('一百零五') == 105
    question = build_keyword_query('公司的注册资本是多少？')
    assert ('注册', '册资', '资本') in question.groups
    assert ('公司',) not in question.groups
    assert build_keyword_query('张三').groups == (('张三',),)
    day = build_keyword_query('2024-03-15')
    assert 'date_20240315' in day.canonical and 'date_202403' not in day.canonical
    assert 'clause_12' in build_keyword_query('第12条').canonical
    assert 'amt_2000wan' in build_keyword_query('人民币2,000万元').canonical
    assert CODE.lower() in build_keyword_query(CODE).tsquery


def test_fusion_order_respects_weights_and_single_channel_switches():
    vector = [dict(id='a', similarity=0.9), dict(id='b', similarity=0.2), dict(id='c', similarity=0.1)]
    keyword = [dict(id='c', keyword_score=1.0), dict(id='a', keyword_score=0.2)]
    common = dict(fusion='rrf', rrf_k=60, vector_weight=1, keyword_weight=1, limit=3, key=lambda row: row['id'])
    hybrid = fuse_candidates(vector, keyword, mode='hybrid', **common)
    assert [row['id'] for row in hybrid] == ['a', 'c', 'b']
    both = next(row for row in hybrid if row['id'] == 'a')
    assert both['recall']['channels'] == ['vector', 'keyword']
    assert both['recall']['vector_rank'] == 1 and both['recall']['keyword_rank'] == 2
    promoted = fuse_candidates(vector, keyword, mode='hybrid', fusion='rrf', rrf_k=60,
        vector_weight=1, keyword_weight=8, limit=3, key=lambda row: row['id'])
    assert [row['id'] for row in promoted] == ['c', 'a', 'b']
    weighted = fuse_candidates(vector, keyword, mode='hybrid', fusion='weighted', rrf_k=60,
        vector_weight=1, keyword_weight=3, limit=3, key=lambda row: row['id'])
    assert [row['id'] for row in weighted] == ['c', 'a', 'b']
    assert weighted[0]['recall']['fusion'] == 'weighted'
    only_vector = fuse_candidates(vector, keyword, mode='vector', **common)
    assert [row['id'] for row in only_vector] == ['a', 'b', 'c']
    assert all(row['recall']['channels'] == ['vector'] for row in only_vector)
    only_keyword = fuse_candidates(vector, keyword, mode='keyword', **common)
    assert [row['id'] for row in only_keyword] == ['c', 'a']
    assert only_keyword[0]['recall']['keyword_score'] == 1.0
    assert only_keyword[0]['recall']['vector_score'] is None


def test_retrieval_settings_read_environment_and_reject_bad_values(monkeypatch):
    with pytest.raises(ValueError):
        Settings(retrieval_mode='both')
    with pytest.raises(ValueError):
        Settings(retrieval_candidate_limit=13)
    with pytest.raises(ValueError):
        Settings(retrieval_keyword_weight=0)
    monkeypatch.setattr('dd_agent.config.load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setenv('DDA_RETRIEVAL_MODE', 'keyword')
    monkeypatch.setenv('DDA_RETRIEVAL_FUSION', 'weighted')
    monkeypatch.setenv('DDA_RETRIEVAL_KEYWORD_LIMIT', '8')
    monkeypatch.setenv('DDA_RETRIEVAL_KEYWORD_WEIGHT', '2.5')
    settings = Settings.from_env()
    assert settings.retrieval_mode == 'keyword' and settings.retrieval_fusion == 'weighted'
    assert settings.retrieval_keyword_limit == 8 and settings.retrieval_keyword_weight == 2.5
    assert settings.retrieval_vector_limit == 12 and settings.retrieval_rrf_k == 60


def prepared(tmp_path, database_url, **kwargs):
    app, project_id, document_id, service, _ = setup(tmp_path, database_url)
    service.models = CountingModels()
    service.settings = replace(service.settings, **kwargs)
    return app, project_id, document_id, service


def write_pages(app, document_id, pages):
    database = app.state.database
    database.set_reading(document_id, len(pages), 'scan-v1')
    for number, text in pages:
        database.save_page(document_id, 'scan-v1', dict(page_number=number, status='succeeded', method='ocr', text=text))


def test_keyword_hits_codes_names_amounts_dates_and_clauses_without_embedding(tmp_path, database_url):
    app, project_id, document_id, service = prepared(tmp_path, database_url, retrieval_mode='keyword')
    write_pages(app, document_id, [
        (1, f'北京珊瑚科技有限公司股东张三认缴。统一社会信用代码{CODE}，注册资本为2000万元，于2024年3月15日依据第十二条签订。'),
        (2, '公司住所位于吉林省。'),
    ])
    assert service.index(project_id, document_id, [1, 2])['indexed_pages'] == [1, 2]
    service.models.embeds = service.models.ranks = 0
    queries = (CODE, '张三', '北京珊瑚科技有限公司', '第12条', '2024-03-15', '2,000万元')
    for query in queries:
        result = service.search(project_id, query)
        assert result['results'], query
        hit = result['results'][0]
        assert hit['page_number'] == 1
        assert hit['recall']['channels'] == ['keyword']
        assert hit['recall']['keyword_score'] == 1.0
        assert hit['recall']['vector_score'] is None
    assert service.models.embeds == 0 and service.models.ranks == len(queries)
    with app.state.database.connect() as conn:
        assert conn.execute("SELECT 1 FROM pg_indexes WHERE schemaname='dd_agent' AND indexname='evidence_chunks_lexemes'").fetchone()
        assert conn.execute('SELECT bool_and(lexemes IS NOT NULL) AS ready FROM dd_agent.evidence_chunks').fetchone()['ready']


def test_exact_term_enters_hybrid_results_when_vector_recall_misses_it(tmp_path, database_url):
    app, project_id, document_id, service = prepared(tmp_path, database_url)
    pages = [(1, f'注册资本为2000万元，统一社会信用代码{CODE}。')]
    pages += [(number, '公司住所位于吉林省。') for number in range(2, 15)]
    write_pages(app, document_id, pages)
    assert service.index(project_id, document_id, [number for number, _ in pages])['failed_pages'] == []
    service.models.embeds = service.models.ranks = 0
    service.settings = replace(service.settings, retrieval_mode='vector')
    vector_only = service.search(project_id, CODE)
    assert vector_only['results']
    assert all(CODE not in hit['text'] for hit in vector_only['results'])
    assert all(hit['recall']['channels'] == ['vector'] for hit in vector_only['results'])
    vector_embeds = service.models.embeds
    service.settings = replace(service.settings, retrieval_mode='keyword')
    keyword_only = service.search(project_id, CODE)
    assert CODE in keyword_only['results'][0]['text']
    assert keyword_only['results'][0]['recall']['channels'] == ['keyword']
    assert service.models.embeds == vector_embeds
    service.settings = replace(service.settings, retrieval_mode='hybrid')
    hybrid = service.search(project_id, CODE)
    hit = hybrid['results'][0]
    assert CODE in hit['text']
    assert hit['recall']['channels'] == ['keyword']
    assert hit['recall']['fusion'] == 'rrf' and hit['recall']['fusion_score'] > 0
    assert hit['rerank_score'] == 0.9


def test_missing_lexemes_are_backfilled_without_reembedding(tmp_path, database_url, monkeypatch):
    app, project_id, document_id, service = prepared(tmp_path, database_url, retrieval_mode='keyword')
    write_pages(app, document_id, [(1, f'统一社会信用代码{CODE}。'), (2, '股东张三。')])
    service.index(project_id, document_id, [1, 2])
    embedded = service.models.embeds
    database = app.state.database
    with database.connect() as conn:
        conn.execute('UPDATE dd_agent.evidence_chunks SET lexemes=NULL')
    assert service.search(project_id, CODE)['results'] == []
    filled = backfill_lexemes(database, batch=1)
    assert filled == {'evidence_chunks': 2, 'parent_index_children': 0}
    assert CODE in service.search(project_id, CODE)['results'][0]['text']
    assert '张三' in service.search(project_id, '张三')['results'][0]['text']
    with database.connect() as conn:
        conn.execute('UPDATE dd_agent.evidence_chunks SET lexemes=NULL')
    assert database.migrate()['evidence_chunks'] == 2
    assert service.search(project_id, '张三')['results']
    with database.connect() as conn:
        conn.execute('UPDATE dd_agent.evidence_chunks SET lexemes=NULL')
    monkeypatch.setenv('DDA_DATABASE_URL', database_url)
    monkeypatch.setenv('DDA_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['dd_agent', 'reindex-lexical', '--force'])
    from dd_agent.__main__ import main
    assert main() == 0
    assert service.search(project_id, CODE)['results']
    forced = backfill_lexemes(database, force=True, batch=1)
    assert forced['evidence_chunks'] == 2
    assert service.models.embeds == embedded


def test_parent_children_backfill_keyword_channel(tmp_path, database_url):
    from test_parent_search import prepared as parent_prepared
    search, database, project_id, _, _, _ = parent_prepared(tmp_path, database_url)
    search.evidence.settings = replace(search.evidence.settings, retrieval_mode='vector')
    assert search.search(project_id, '资本')['results']
    with database.connect() as conn:
        conn.execute('UPDATE dd_agent.parent_index_children SET lexemes=NULL')
    search.evidence.settings = replace(search.evidence.settings, retrieval_mode='keyword')
    assert search.search(project_id, '资本')['results'] == []
    filled = backfill_lexemes(database, batch=1)
    assert filled['parent_index_children'] >= 2
    result = search.search(project_id, '资本')
    assert result['results']
    assert 'keyword' in result['results'][0]['recall']['channels']
    assert result['results'][0]['recall']['keyword_score'] == 1.0
