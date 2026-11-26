"""官方术语改写：词表、开关，以及关键词和重排实际用到的问句。"""
import re
from dataclasses import replace

import pytest

from dd_agent.config import Settings
from dd_agent.lexical import build_keyword_query
from dd_agent.query_rewrite import LEXICON_PATH, load_lexicon, resolve_query
from dd_agent.workers.retrieval_worker import INSTRUCTION, RERANK_INSTRUCTION, rerank_pair
from test_lexical import prepared, write_pages


def test_lexicon_is_generic_and_keeps_registered_capital_apart_from_paid_in():
    raw = LEXICON_PATH.read_text(encoding='utf-8')
    assert '有限公司' not in raw
    assert not re.search(r'\d{6,}', raw)
    assert not re.search(r'[0-9A-Z]{18}', raw)
    book = load_lexicon()
    assert book['version'] == 'dd-official-terms-v1'
    by_id = {entry['id']: entry for entry in book['entries']}
    registered = set(by_id['registered_capital']['keyword'])
    paid = set(by_id['paid_in_capital']['keyword'])
    assert registered.isdisjoint(paid)
    assert '注册资本' in registered and '认缴出资额' in registered
    assert '实缴出资额' in paid and '实收资本' in paid
    assert '法定代表人' in by_id['legal_representative']['keyword']
    assert '执行董事' in by_id['executive_director']['keyword']
    assert '公司章程' in by_id['articles']['keyword']
    for entry in book['entries']:
        assert entry['vector'] in entry['keyword']


def test_rewrite_expands_aliases_without_dumping_them_into_the_vector_query():
    capital = resolve_query('注册资金是多少')
    assert capital.enabled is True
    assert capital.original == '注册资金是多少'
    assert capital.rewritten == '注册资金是多少（注册资本）'
    assert '认缴出资额' not in capital.rewritten
    assert '认缴出资额' in capital.keyword_terms
    assert '注册资本' in capital.keyword_terms
    assert len(capital.rewritten) - len(capital.original) <= 24

    paid = resolve_query('实缴资本是多少')
    assert '实缴出资额' in paid.rewritten
    assert '注册资本' not in paid.keyword_terms
    assert '实收资本' in paid.keyword_terms

    already = resolve_query('注册资本是多少')
    assert already.rewritten == '注册资本是多少'
    assert '认缴出资额' in already.keyword_terms
    assert all('实缴' not in term and '实收' not in term for term in already.keyword_terms)

    deadline = resolve_query('认缴期限是什么')
    assert '注册资本' not in deadline.keyword_terms
    assert '认缴期限' in deadline.keyword_terms or '出资时间' in deadline.keyword_terms

    shareholder = resolve_query('法人股东是谁')
    assert '法定代表人' not in shareholder.rewritten
    assert '法定代表人' not in shareholder.keyword_terms

    several = resolve_query('注册资金、法人代表和董事长分别是什么')
    assert several.rewritten.count('（') == 1
    assert '注册资本' in several.rewritten and '法定代表人' in several.rewritten
    assert '执行董事' not in several.rewritten
    assert '执行董事' in several.keyword_terms
    assert 'keyword_terms' not in several.public()


def test_rewrite_can_be_disabled_and_still_validates_the_user_query():
    off = resolve_query('  注册资金是多少  ', enabled=False)
    assert off.enabled is False
    assert off.original == off.rewritten == '注册资金是多少'
    assert off.keyword_terms == ()
    long = '注册资金' + '问' * (500 - len('注册资金'))
    expanded = resolve_query(long)
    assert len(expanded.original) == 500
    assert expanded.rewritten.startswith('注册资金')
    assert len(expanded.rewritten) > 500
    with pytest.raises(ValueError, match='1–500'):
        resolve_query('问' * 501)
    with pytest.raises(ValueError, match='1–500'):
        resolve_query('   ')
    with pytest.raises(ValueError):
        Settings(query_rewrite_enabled='yes')


def test_keyword_plan_keeps_official_terms_that_a_long_phrase_would_drop():
    plain = build_keyword_query('公司章程规定的经理是谁')
    assert not any('经理' in ''.join(group) for group in plain.groups)
    expanded = resolve_query('公司章程规定的经理是谁')
    plan = build_keyword_query(expanded.original, extra_terms=expanded.keyword_terms)
    assert any('经理' in ''.join(group) for group in plan.groups)
    assert '总经理' in plan.needles or any('总经理' in ''.join(group) for group in plan.groups)
    assert len(plan.needles) <= 8 and len(plan.groups) <= 16


def test_environment_switch_defaults_on(monkeypatch):
    monkeypatch.setattr('dd_agent.config.load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.delenv('DDA_QUERY_REWRITE', raising=False)
    assert Settings.from_env().query_rewrite_enabled is True
    monkeypatch.setenv('DDA_QUERY_REWRITE', '0')
    assert Settings.from_env().query_rewrite_enabled is False
    monkeypatch.setenv('DDA_QUERY_REWRITE', 'false')
    assert Settings.from_env().query_rewrite_enabled is False


def test_rerank_instruction_is_chinese_and_embedding_instruction_is_unchanged():
    assert INSTRUCTION == 'Retrieve evidence passages relevant to the due diligence question.'
    assert '尽职调查' in RERANK_INSTRUCTION
    assert '原文证据' in RERANK_INSTRUCTION
    pair = rerank_pair('董事长是谁', '执行董事由甲担任。')
    assert pair == (
        f'<Instruct>: {RERANK_INSTRUCTION}\n'
        '<Query>: 董事长是谁\n'
        '<Document>: 执行董事由甲担任。'
    )
    assert INSTRUCTION not in pair
    assert rerank_pair('', '章程') == f'<Instruct>: {RERANK_INSTRUCTION}\n<Query>: \n<Document>: 章程'


class RecordingModels:
    version = 'synthetic-embedding-v1'

    def __init__(self):
        self.embedded = []
        self.reranked = []

    def profile(self):
        return self.version

    def embed(self, texts, query=False):
        if query:
            self.embedded.extend(texts)
        return [[1., 0.] + [0.] * 1022 for _ in texts]

    def rerank(self, query, texts):
        self.reranked.append(query)
        return [0.9 for _ in texts]


def test_search_uses_expanded_terms_and_the_switch_restores_the_raw_query(tmp_path, database_url):
    app, project_id, document_id, service = prepared(tmp_path, database_url, retrieval_mode='keyword')
    write_pages(app, document_id, [
        (1, '认缴出资额为人民币500万元。'),
        (2, '执行董事由甲担任。'),
    ])
    assert service.index(project_id, document_id, [1, 2])['indexed_pages'] == [1, 2]
    models = RecordingModels()
    service.models = models
    service.settings = replace(service.settings, retrieval_mode='keyword', query_rewrite_enabled=True)
    capital = service.search(project_id, '注册资金是多少')
    assert capital['results'] and '认缴出资额' in capital['results'][0]['text']
    assert capital['query_rewrite']['original'] == '注册资金是多少'
    assert capital['query_rewrite']['rewritten'] == '注册资金是多少（注册资本）'
    assert '认缴出资额' not in capital['query_rewrite']['rewritten']
    assert 'keyword_terms' not in capital['query_rewrite']
    role = service.search(project_id, '董事长是谁')
    assert role['results'] and '执行董事' in role['results'][0]['text']
    assert models.reranked[-1] == '董事长是谁（执行董事）'
    assert models.embedded == []
    service.settings = replace(service.settings, query_rewrite_enabled=False)
    assert service.search(project_id, '注册资金是多少')['results'] == []
    assert service.search(project_id, '董事长是谁')['results'] == []
    missed = service.search(project_id, '董事长是谁')
    assert missed['query_rewrite']['enabled'] is False
    assert missed['query_rewrite']['rewritten'] == '董事长是谁'
    wide = '注册资金' + '问' * (500 - len('注册资金'))
    found = service.search(project_id, wide)
    assert found['query_rewrite']['original'] == wide
    with pytest.raises(ValueError, match='1–500'):
        service.search(project_id, '问' * 501)
