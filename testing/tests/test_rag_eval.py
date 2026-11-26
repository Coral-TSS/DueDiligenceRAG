"""RAG 评测包的离线检查。不连接数据库，不加载模型，不读客户文件或密钥。"""
import asyncio
import importlib.util
import inspect
import json
import os
import sys
import types
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest


ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / 'testing' / 'quality' / 'rag_eval'


def _load_pipeline():
    spec = importlib.util.spec_from_file_location('rag_eval_pipeline', EVAL_DIR / 'pipeline.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pipeline = _load_pipeline()
DEEPSEEK_EXTRA_BODY = pipeline.DEEPSEEK_EXTRA_BODY
DEEPSEEK_MAX_TOKENS = pipeline.DEEPSEEK_MAX_TOKENS
GENERATION_SYSTEM = pipeline.GENERATION_SYSTEM
NO_CONTEXT_ANSWER = pipeline.NO_CONTEXT_ANSWER
QueryApp = pipeline.QueryApp
RagPipeline = pipeline.RagPipeline
RecordReplay = pipeline.RecordReplay
RetrievalPipeline = pipeline.RetrievalPipeline
bind_completion_options = pipeline.bind_completion_options
completion_kwargs = pipeline.completion_kwargs
completion_text = pipeline.completion_text
contexts_from_search = pipeline.contexts_from_search
describe_judge_llm = pipeline.describe_judge_llm
filter_payload = pipeline.filter_payload
judge_llm_config = pipeline.judge_llm_config
load_dataset = pipeline.load_dataset
load_score_records = pipeline.load_score_records
metric_skip_message = pipeline.metric_skip_message
prepare_run = pipeline.prepare_run
select_metric_names = pipeline.select_metric_names


_SCRIPTS = {}


def _load_script(name):
    if name not in _SCRIPTS:
        spec = importlib.util.spec_from_file_location(name, EVAL_DIR / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _SCRIPTS[name] = module
    return _SCRIPTS[name]


def _score_record(**overrides):
    record = {
        'id': 'q1',
        'user_input': '示例主体的法定代表人是谁？',
        'retrieved_contexts': ['办公材料片段'],
        'response': '材料未记载。',
        'reference': '甲',
    }
    record.update(overrides)
    return record


PROJECT = '11111111-1111-1111-1111-111111111111'
OTHER = '44444444-4444-4444-4444-444444444444'
DOCUMENT = '22222222-2222-2222-2222-222222222222'
SUBJECT = '33333333-3333-3333-3333-333333333333'


def _write(tmp_path, body, name='gold.json'):
    path = tmp_path / name
    path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
    return path


def _body(items=None, project_id=PROJECT):
    return {
        'customer_data': False,
        'project_id': project_id,
        'items': items if items is not None else [{
            'id': 'q1',
            'question': '示例主体的法定代表人是谁？',
            'reference': '甲',
        }],
    }


class FakeSearch:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {
            'status': 'candidates',
            'coverage': {'office_chunks': 2, 'indexed_pages': 0},
            'results': [{
                'index_kind': 'source_unit',
                'text': '办公材料片段',
                'locator_label': '第 1 段',
                'unit_id': 'unit-1',
            }],
        }

    def search(self, project_id, query, *, document_id=None, scope=None, cancel=None,
               vector_limit=None, keyword_limit=None, candidate_limit=None, result_limit=None):
        self.calls.append({
            'project_id': project_id,
            'query': query,
            'document_id': document_id,
            'scope': scope,
            'cancel': cancel,
            'vector_limit': vector_limit,
            'keyword_limit': keyword_limit,
            'candidate_limit': candidate_limit,
            'result_limit': result_limit,
        })
        return self.payload


class _Completions:
    def __init__(self, sink):
        self.sink = sink

    def create(self, **kwargs):
        self.sink.append(kwargs)
        message = SimpleNamespace(content='材料未记载。')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeChat:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=_Completions(self.calls))


def test_example_is_synthetic_and_placeholder_is_rejected():
    example = json.loads((EVAL_DIR / 'dataset.example.json').read_text(encoding='utf-8'))
    assert example['customer_data'] is False
    assert example['project_id'] == '00000000-0000-0000-0000-000000000000'
    text = json.dumps(example, ensure_ascii=False)
    assert '法定代表人' in text and '注册资本' in text
    with pytest.raises(ValueError, match='全零'):
        load_dataset(EVAL_DIR / 'dataset.example.json')


def test_dataset_accepts_external_file_and_normalizes_filters(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    path = _write(outside, _body([{
        'question': '示例主体的注册资本是多少？',
        'reference': '  ',
        'document_ids': [],
        'categories': [],
        'subject_ids': [],
        'date_point': '',
    }]))
    dataset = load_dataset(path)
    assert dataset['project_id'] == PROJECT
    assert dataset['customer_data'] is False
    item = dataset['items'][0]
    assert item['id'] == 'item-1'
    assert item['reference'] == ''
    assert item['document_ids'] is None
    assert item['categories'] is None
    assert 'question' not in filter_payload(item)
    assert 'reference' not in filter_payload(item)


def test_dataset_rejects_malformed_rows(tmp_path):
    cases = [
        (_body(project_id='not-a-uuid'), 'UUID'),
        (_body(project_id='00000000-0000-0000-0000-000000000000'), '全零'),
        ({'project_id': PROJECT, 'items': []}, 'items'),
        (_body([{'question': ''}]), 'question'),
        (_body([{'question': '问' * 501}]), 'question'),
        (_body([{'question': '示例问题', 'reference': 1}]), 'reference'),
        (_body([{'question': '示例问题', 'document_ids': 'abc'}]), '数组'),
        (_body([{
            'question': '示例问题',
            'document_id': DOCUMENT,
            'document_ids': [DOCUMENT],
        }]), '同时'),
        (_body([{'question': '示例问题', 'categories': ['not-a-category']}]), '类别'),
        (_body([{'question': '示例问题', 'date_point': '2024/01/31'}]), 'date_point'),
        (_body([{'question': '示例问题', 'period_start': '2024-01-01'}]), '期间'),
        (_body([{'question': '示例问题', 'page_from': 2}]), '页范围'),
    ]
    for body, message in cases:
        with pytest.raises(ValueError, match=message):
            load_dataset(_write(tmp_path, body))


def test_project_override_also_rejects_the_placeholder(tmp_path):
    path = _write(tmp_path, _body())
    dataset, project_id = prepare_run(path, OTHER)
    assert dataset['project_id'] == PROJECT
    assert project_id == OTHER
    with pytest.raises(ValueError, match='全零'):
        prepare_run(path, '00000000-0000-0000-0000-000000000000')


def test_context_extraction_covers_page_parent_and_office_hits():
    found = {'results': [
        {'index_kind': 'page', 'text': ' 页级片段 '},
        {
            'index_kind': 'parent_child',
            'matched_children': [
                {'text': '子块甲'},
                {'text': '   '},
                {'text': '子块甲'},
            ],
            'context': {'text': '更长的父块窗口'},
        },
        {
            'index_kind': 'source_unit',
            'text': '办公材料片段',
            'locator_label': '第 2 段',
            'unit_id': 'unit-9',
        },
        {'index_kind': 'parent_child', 'children': [{'text': '旧 children 字段'}]},
        {'index_kind': 'parent_child', 'chunks': [{'text': '旧 chunks 字段'}]},
        {
            'index_kind': 'parent_child',
            'matched_children': [{'text': ''}],
            'context': {'text': '只有父块窗口'},
        },
        {'text': '   '},
        '忽略',
    ]}
    assert contexts_from_search(found) == [
        '页级片段',
        '子块甲',
        '办公材料片段',
        '旧 children 字段',
        '旧 chunks 字段',
        '只有父块窗口',
    ]
    assert contexts_from_search({'results': []}) == []
    assert contexts_from_search(None) == []


def test_metric_selection_skips_reference_metrics_unless_every_item_has_one():
    complete = select_metric_names([
        {'id': 'a', 'reference': '甲'},
        {'id': 'b', 'reference': '乙'},
    ])
    assert complete['selected'] == ['Faithfulness', 'LLMContextRecall', 'FactualCorrectness']
    assert complete['skipped'] == []
    assert metric_skip_message(complete) == ''

    partial = select_metric_names([
        {'id': 'a', 'reference': '甲'},
        {'id': 'b', 'reference': '  '},
    ])
    assert partial['selected'] == ['Faithfulness']
    assert partial['skipped'] == ['LLMContextRecall', 'FactualCorrectness']
    assert partial['missing_reference_ids'] == ['b']
    message = metric_skip_message(partial)
    assert 'LLMContextRecall' in message and 'ContextRecall' in message
    assert 'FactualCorrectness' in message and 'Faithfulness' in message
    assert 'b' in message


def test_search_scope_is_shared_by_ragas_and_trulens_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_MODEL', 'deepseek-chat')
    path = _write(tmp_path, _body([
        {
            'id': 'scoped',
            'question': '示例主体在该期间的出资是多少？',
            'reference': '',
            'document_id': DOCUMENT,
            'categories': ['finance'],
            'subject_ids': [SUBJECT],
            'period_start': '2024-01-01',
            'period_end': '2024-12-31',
            'page_from': 2,
            'page_to': 4,
        },
        {
            'id': 'open',
            'question': '示例主体的法定代表人是谁？',
            'date_point': '2024-06-30',
        },
    ]))
    dataset = load_dataset(path)
    fake = FakeSearch()
    pipeline = RagPipeline(service=fake, chat=FakeChat())
    app = QueryApp(pipeline, dataset['project_id'])

    record, found = pipeline.run_item(PROJECT, dataset['items'][0], generate=False)
    answer = app.query(dataset['items'][0]['question'], filters=filter_payload(dataset['items'][0]))
    app.query(dataset['items'][1]['question'], filters=filter_payload(dataset['items'][1]))

    assert record['response'] == ''
    assert record['retrieved_contexts'] == ['办公材料片段']
    assert found['coverage']['office_chunks'] == 2
    assert answer == '材料未记载。'
    assert 'eval-key' not in answer
    assert len(fake.calls) == 3
    scoped = fake.calls[0]['scope']
    assert fake.calls[0]['document_id'] is None
    assert fake.calls[0]['query'] == '示例主体在该期间的出资是多少？'
    assert scoped.document_ids == (UUID(DOCUMENT),)
    assert scoped.categories == ('finance',)
    assert scoped.subject_ids == (UUID(SUBJECT),)
    assert scoped.period_start == date(2024, 1, 1)
    assert scoped.period_end == date(2024, 12, 31)
    assert scoped.page_from == 2 and scoped.page_to == 4
    assert scoped.date_point is None
    assert fake.calls[1]['scope'].document_ids == scoped.document_ids
    assert fake.calls[1]['document_id'] is None
    opened = fake.calls[2]['scope']
    assert opened.document_ids is None
    assert opened.date_point == date(2024, 6, 30)
    assert opened.categories is None


def test_unfiltered_item_passes_an_empty_scope():
    fake = FakeSearch()
    pipeline = RetrievalPipeline(service=fake)
    contexts, _found = pipeline.retrieve(PROJECT, '示例问题', {'question': '示例问题'})
    assert contexts == ['办公材料片段']
    assert fake.calls[0]['scope'] is None
    assert fake.calls[0]['document_id'] is None


def _clear_retrieval_env(monkeypatch):
    for name in (
        'EVAL_RETRIEVAL_VECTOR_LIMIT',
        'EVAL_RETRIEVAL_KEYWORD_LIMIT',
        'EVAL_RETRIEVAL_CANDIDATE_LIMIT',
        'EVAL_RETRIEVAL_RESULT_LIMIT',
        'DDA_RETRIEVAL_VECTOR_LIMIT',
        'DDA_RETRIEVAL_KEYWORD_LIMIT',
        'DDA_RETRIEVAL_CANDIDATE_LIMIT',
    ):
        monkeypatch.delenv(name, raising=False)


def test_retrieve_uses_the_wider_eval_window_and_ignores_product_settings(monkeypatch):
    _clear_retrieval_env(monkeypatch)
    monkeypatch.setenv('DDA_RETRIEVAL_VECTOR_LIMIT', '50')
    monkeypatch.setenv('DDA_RETRIEVAL_CANDIDATE_LIMIT', '12')
    fake = FakeSearch()
    retrieval = RetrievalPipeline(service=fake)
    retrieval.retrieve(PROJECT, '示例问题', {'question': '示例问题'})
    call = fake.calls[0]
    assert call['vector_limit'] == pipeline.EVAL_VECTOR_LIMIT == 20
    assert call['keyword_limit'] == pipeline.EVAL_KEYWORD_LIMIT == 20
    assert call['candidate_limit'] == pipeline.EVAL_CANDIDATE_LIMIT == 40
    assert call['result_limit'] == pipeline.EVAL_RESULT_LIMIT == 15
    assert retrieval.limits == {
        'vector_limit': 20,
        'keyword_limit': 20,
        'candidate_limit': 40,
        'result_limit': 15,
    }
    monkeypatch.setenv('EVAL_RETRIEVAL_VECTOR_LIMIT', '24')
    monkeypatch.setenv('EVAL_RETRIEVAL_KEYWORD_LIMIT', '24')
    monkeypatch.setenv('EVAL_RETRIEVAL_CANDIDATE_LIMIT', '48')
    monkeypatch.setenv('EVAL_RETRIEVAL_RESULT_LIMIT', ' 16 ')
    overridden = RagPipeline(service=fake)
    overridden.retrieve(PROJECT, '示例问题', {'question': '示例问题'})
    assert fake.calls[1]['vector_limit'] == 24
    assert fake.calls[1]['keyword_limit'] == 24
    assert fake.calls[1]['candidate_limit'] == 48
    assert fake.calls[1]['result_limit'] == 16
    records, traces = pipeline.collect_records(
        overridden,
        PROJECT,
        [{'id': 'q1', 'question': '示例主体的法定代表人是谁？', 'reference': ''}],
        generate=False,
    )
    assert records[0]['retrieved_contexts'] == ['办公材料片段']
    assert 'generation_contexts' not in records[0]
    assert traces[0]['retrieval_limits'] == overridden.limits
    assert traces[0]['retrieved_context_count'] == 1
    assert traces[0]['generation_context_count'] == 0
    assert traces[0]['generation_context_limit'] == pipeline.GENERATION_CONTEXT_LIMIT == 5
    assert 'query_rewrite' not in traces[0]
    for raw in ('0', '51', '20.5', 'true', '+20'):
        monkeypatch.setenv('EVAL_RETRIEVAL_CANDIDATE_LIMIT', raw)
        with pytest.raises(ValueError, match='1–50'):
            RetrievalPipeline(service=fake)


def test_missing_contexts_do_not_call_the_judge(monkeypatch):
    monkeypatch.delenv('EVAL_LLM_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    fake = FakeSearch({'status': 'no_matches', 'coverage': {'office_chunks': 0}, 'results': []})
    chat = FakeChat()
    pipeline = RagPipeline(service=fake, chat=chat)
    record, _found = pipeline.run_item(PROJECT, {
        'id': 'empty',
        'question': '示例问题',
        'reference': '',
    }, generate=True)
    assert record['response'] == NO_CONTEXT_ANSWER
    assert chat.calls == []
    assert '中文' in GENERATION_SYSTEM and '材料未记载' in GENERATION_SYSTEM
    assert '只回答问题所问的那一个字段' in GENERATION_SYSTEM
    assert '相邻' in GENERATION_SYSTEM and '必须直接答出' in GENERATION_SYSTEM
    assert '片段以外' in GENERATION_SYSTEM


def test_generation_uses_only_retrieved_context_and_does_not_log_the_key(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_MODEL', 'deepseek-chat')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com')
    fake = FakeSearch()
    chat = FakeChat()
    pipeline = RagPipeline(service=fake, chat=chat)
    record, _found = pipeline.run_item(PROJECT, {
        'id': 'gen',
        'question': '示例问题',
        'reference': '甲',
    }, generate=True)
    assert record['response'] == '材料未记载。'
    sent = chat.calls[0]
    assert sent['model'] == 'deepseek-chat'
    assert sent['temperature'] == 0
    assert sent['max_tokens'] == DEEPSEEK_MAX_TOKENS
    assert sent['extra_body'] == DEEPSEEK_EXTRA_BODY
    assert sent['extra_body']['thinking'] == {'type': 'disabled'}
    assert sent['extra_body']['reasoning_effort'] == 'none'
    assert 'tools' not in sent
    assert 'deepseek_web_search' not in json.dumps(sent, ensure_ascii=False)
    assert sent['messages'][0]['content'] == GENERATION_SYSTEM
    assert '办公材料片段' in sent['messages'][1]['content']
    assert '只回答下面这个问题所问的字段' in sent['messages'][1]['content']
    assert record['generation_contexts'] == ['办公材料片段']
    assert 'eval-key' not in json.dumps(sent, ensure_ascii=False)
    info = describe_judge_llm()
    assert info == {
        'model': 'deepseek-chat',
        'base_url': 'https://api.deepseek.com',
        'api_key_set': True,
        'thinking_disabled': True,
    }
    assert 'eval-key' not in json.dumps(info)


def test_generation_keeps_the_retrieval_window_and_sends_only_the_top_snippets(monkeypatch, tmp_path):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_MODEL', 'gpt-4o-mini')
    contexts = [f'片段{index}记载了不同字段。' for index in range(8)]
    fake = FakeSearch({
        'status': 'candidates',
        'coverage': {'office_chunks': 0},
        'results': [{'text': text} for text in contexts],
        'query_rewrite': {
            'original': '示例主体的法定代表人是谁？',
            'rewritten': '示例主体的法定代表人是谁？（法定代表人）',
            'enabled': True,
            'keyword_terms': ['不应进入痕迹'],
        },
    })
    chat = FakeChat()
    rag = RagPipeline(service=fake, chat=chat)
    item = {'id': 'cut', 'question': '示例主体的法定代表人是谁？', 'reference': '甲'}
    record, _found = rag.run_item(PROJECT, item, generate=True)
    assert record['retrieved_contexts'] == contexts
    assert record['generation_contexts'] == contexts[:pipeline.GENERATION_CONTEXT_LIMIT]
    sent = chat.calls[0]['messages'][1]['content']
    assert '片段0记载了不同字段。' in sent and '片段4记载了不同字段。' in sent
    assert '片段5记载了不同字段。' not in sent
    assert '不应进入痕迹' not in sent
    _records, traces = pipeline.collect_records(rag, PROJECT, [item], generate=True)
    trace = traces[0]
    assert trace['retrieved_context_count'] == 8
    assert trace['generation_context_count'] == 5
    assert trace['context_count'] == 8
    assert trace['query_rewrite'] == {
        'original': '示例主体的法定代表人是谁？',
        'rewritten': '示例主体的法定代表人是谁？（法定代表人）',
        'enabled': True,
    }
    assert 'keyword_terms' not in trace['query_rewrite']
    assert pipeline.faithfulness_contexts(record) == contexts[:5]
    assert pipeline.faithfulness_contexts({'retrieved_contexts': contexts}) == contexts
    script = _load_script('run_ragas')
    assert script._faithfulness_kwargs(record)['retrieved_contexts'] == contexts[:5]
    assert script._context_recall_kwargs(record)['retrieved_contexts'] == contexts
    saved_path = tmp_path / 'records.json'
    saved_path.write_text(json.dumps([record], ensure_ascii=False), encoding='utf-8')
    saved = load_score_records(saved_path)[0]
    assert saved['generation_contexts'] == contexts[:5]
    assert saved['retrieved_contexts'] == contexts
    broken = tmp_path / 'broken.json'
    broken.write_text(json.dumps([_score_record(generation_contexts='不是数组')], ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='generation_contexts'):
        load_score_records(broken)
    readme = (EVAL_DIR / 'README.md').read_text(encoding='utf-8')
    assert 'generation_contexts' in readme
    assert 'retrieved_context_count' in readme
    assert '前 5' in readme
    assert 'Agent' in readme
    with pytest.raises(ValueError, match='1–50'):
        pipeline.contexts_for_generation(contexts, limit=0)


def test_judge_env_prefers_eval_variables_and_hides_query_secrets(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-key')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com?token=hidden')
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    config = judge_llm_config()
    assert config['api_key'] == 'eval-key'
    assert config['model'] == 'deepseek-flash'
    assert completion_kwargs(config)['extra_body'] == DEEPSEEK_EXTRA_BODY
    assert completion_kwargs(config)['max_tokens'] == DEEPSEEK_MAX_TOKENS
    info = describe_judge_llm(config)
    assert info['base_url'] == 'https://api.deepseek.com'
    assert info['thinking_disabled'] is True
    assert 'hidden' not in json.dumps(info)
    assert 'eval-key' not in json.dumps(info)

    monkeypatch.delenv('EVAL_LLM_API_KEY', raising=False)
    monkeypatch.delenv('EVAL_LLM_BASE_URL', raising=False)
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-key')
    fallback = judge_llm_config()
    assert fallback['api_key'] == 'openai-key'
    assert fallback['model'] == 'gpt-4o-mini'
    assert completion_kwargs(fallback) == {}
    assert describe_judge_llm(fallback)['thinking_disabled'] is False
    with pytest.raises(RuntimeError, match='EVAL_LLM_API_KEY') as caught:
        monkeypatch.delenv('OPENAI_API_KEY', raising=False)
        pipeline.require_api_key()
    assert 'openai-key' not in str(caught.value)
    assert 'eval-key' not in str(caught.value)


def test_current_search_api_still_exposes_scope_and_office_chunks():
    from dd_agent.material_metadata import parse_filters
    from dd_agent.retrieval import EvidenceService

    search = inspect.signature(EvidenceService.search)
    assert search.parameters['document_id'].kind is inspect.Parameter.KEYWORD_ONLY
    assert search.parameters['scope'].kind is inspect.Parameter.KEYWORD_ONLY
    filters = inspect.signature(parse_filters)
    for name in (
        'document_id', 'document_ids', 'categories', 'subject_ids',
        'date_point', 'period_start', 'period_end', 'page_from', 'page_to',
    ):
        assert name in filters.parameters
    retrieval = (ROOT / 'src/dd_agent/retrieval.py').read_text(encoding='utf-8')
    mixed = (ROOT / 'src/dd_agent/mixed_search.py').read_text(encoding='utf-8')
    assert "coverage['office_chunks']" in retrieval
    assert 'source_unit' in retrieval
    assert '_office_rows' in mixed


def test_package_stays_out_of_product_dependencies_and_ignores_local_artifacts():
    product = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    requirements = (EVAL_DIR / 'requirements.txt').read_text(encoding='utf-8')
    gitignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
    local_ignore = (EVAL_DIR / '.gitignore').read_text(encoding='utf-8')
    readme = (EVAL_DIR / 'README.md').read_text(encoding='utf-8')
    assert 'ragas' not in product.split('[project.optional-dependencies]', 1)[0]
    assert 'trulens' not in product
    assert 'ragas>=0.4.3' in requirements
    assert 'trulens-providers-openai' in requirements
    requirement_lines = [
        line.strip() for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]
    assert all('trulens-eval' not in line for line in requirement_lines)
    assert '.venv-eval/' in gitignore
    assert '*.local.json' in gitignore
    assert 'testing/quality/rag_eval/output/' in gitignore
    assert 'output/' in local_ignore
    assert '*.local.json' in local_ignore
    assert 'D:\\CodeWork\\DueDiligenceRAG' in readme
    assert 'deepseek-flash' in readme
    assert '--score-from' in readme
    assert 'thinking' in readme
    assert '尽调报告Agent' not in readme
    assert 'dsh-eval' in readme and 'RG-04' in readme
    for path in EVAL_DIR.rglob('*'):
        if path.is_file() and path.suffix in {'.py', '.md', '.json', '.txt', '.gitignore'}:
            text = path.read_text(encoding='utf-8')
            assert '尽调报告Agent' not in text
            assert 'sk-' not in text


def test_trulens_wrapper_forwards_filters_and_ragas_uses_current_metrics():
    trulens = (EVAL_DIR / 'run_trulens.py').read_text(encoding='utf-8')
    ragas = (EVAL_DIR / 'run_ragas.py').read_text(encoding='utf-8')
    assert 'def retrieve(self, query: str, filters: dict | None = None)' in trulens
    assert 'return super().retrieve(query, filters)' in trulens
    assert 'return super().query(query, filters)' in trulens
    assert 'SpanAttributes.SpanType.RETRIEVAL' in trulens
    assert 'SpanAttributes.SpanType.GENERATION' in trulens
    assert 'SpanAttributes.SpanType.RECORD_ROOT' in trulens
    assert 'groundedness_measure_with_cot_reasons_consider_answerability' in trulens
    assert 'relevance_with_cot_reasons' in trulens
    assert 'context_relevance_with_cot_reasons' in trulens
    assert 'run_dashboard' in trulens
    assert 'trulens_eval' not in trulens and 'trulens-eval' not in trulens
    assert 'LangchainLLMWrapper' not in ragas
    assert 'llm_factory' in ragas
    assert "factory_kwargs = {'provider': 'openai', 'client': client}" in ragas
    assert 'ContextRecall' in ragas and 'Faithfulness' in ragas and 'FactualCorrectness' in ragas
    assert 'completion_kwargs(config)' in ragas
    assert '--score-from' in ragas and '--score-from' in trulens
    assert 'openai_client(config)' in trulens
    assert 'RecordReplay' in trulens


def test_framework_imports_when_the_eval_environment_is_installed():
    pytest.importorskip('ragas')
    pytest.importorskip('trulens.core')
    pytest.importorskip('trulens.providers.openai')
    from ragas.llms import llm_factory
    from ragas.metrics.collections import ContextRecall, Faithfulness, FactualCorrectness
    from trulens.apps.app import TruApp
    from trulens.core import Metric, Selector, TruSession
    from trulens.core.otel.instrument import instrument
    from trulens.dashboard import run_dashboard
    from trulens.otel.semconv.trace import SpanAttributes
    from trulens.providers.openai import OpenAI

    assert all((
        llm_factory, ContextRecall, Faithfulness, FactualCorrectness,
        TruApp, Metric, Selector, TruSession, instrument, run_dashboard, OpenAI,
    ))
    assert SpanAttributes.SpanType.RETRIEVAL
    assert SpanAttributes.SpanType.GENERATION
    assert SpanAttributes.SpanType.RECORD_ROOT
    assert hasattr(Faithfulness, 'score') or hasattr(Faithfulness, 'ascore')


def test_openai_generation_does_not_send_deepseek_options(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_MODEL', 'gpt-4o-mini')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.openai.com/v1')
    chat = FakeChat()
    pipeline = RagPipeline(service=FakeSearch(), chat=chat)
    record, _found = pipeline.run_item(PROJECT, {
        'id': 'gen',
        'question': '示例问题',
        'reference': '甲',
    }, generate=True)
    assert record['response'] == '材料未记载。'
    sent = chat.calls[0]
    assert sent['model'] == 'gpt-4o-mini'
    assert 'extra_body' not in sent
    assert 'max_tokens' not in sent
    assert 'reasoning_effort' not in sent
    assert completion_kwargs() == {}


def test_named_deepseek_model_disables_thinking_without_a_deepseek_url():
    options = completion_kwargs({
        'api_key': 'eval-key',
        'base_url': 'https://gateway.example/v1',
        'model': 'deepseek-flash',
    })
    assert options['extra_body']['thinking'] == {'type': 'disabled'}
    assert options['max_tokens'] == DEEPSEEK_MAX_TOKENS


def test_bound_client_keeps_thinking_disabled_and_preserves_caller_fields():
    class Completions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return kwargs

    class Responses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(('create', kwargs))
            return kwargs

        def parse(self, **kwargs):
            self.calls.append(('parse', kwargs))
            return kwargs

    completions = Completions()
    responses = Responses()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        responses=responses,
    )
    options = completion_kwargs({
        'base_url': 'https://api.deepseek.com',
        'model': 'deepseek-flash',
    })
    original = completions.create
    assert bind_completion_options(client, {}) is client
    assert completions.create.__func__ is original.__func__
    bind_completion_options(client, options)
    wrapped = completions.create
    bind_completion_options(client, options)
    assert completions.create is wrapped
    client.chat.completions.create(
        model='deepseek-flash',
        max_tokens=100,
        extra_body={'thinking': {'type': 'enabled'}, 'keep': 1},
    )
    sent = completions.calls[0]
    assert sent['max_tokens'] == 100
    assert sent['extra_body']['keep'] == 1
    assert sent['extra_body']['thinking'] == {'type': 'disabled'}
    assert sent['extra_body']['reasoning_effort'] == 'none'
    assert 'tools' not in sent
    second = client.chat.completions.create(model='deepseek-flash')
    assert not inspect.iscoroutine(second)
    assert not inspect.iscoroutinefunction(client.chat.completions.create)
    assert completions.calls[1]['max_tokens'] == DEEPSEEK_MAX_TOKENS
    client.responses.create(model='deepseek-flash', reasoning={'summary': 'auto'})
    client.responses.parse(model='deepseek-flash')
    created = responses.calls[0][1]['reasoning']
    assert created['effort'] == 'none'
    assert created['summary'] == 'auto'
    assert responses.calls[1][1]['reasoning'] == {'effort': 'none'}
    assert 'max_tokens' not in responses.calls[0][1]
    assert 'extra_body' not in responses.calls[0][1]


def _completion(content='材料未记载。'):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason='stop',
        )],
    )


def test_openai_style_async_create_stays_awaitable_after_binding():
    """AsyncCompletions.create 外层不是协程函数，协程藏在 __wrapped__。"""

    async def raw_create(self, **kwargs):
        self.calls.append(kwargs)
        return _completion('{"statements":[]}')

    def sdk_create(self, **kwargs):
        return raw_create(self, **kwargs)

    sdk_create.__wrapped__ = raw_create

    class Completions:
        def __init__(self):
            self.calls = []

        create = sdk_create

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions), responses=None)
    assert not inspect.iscoroutinefunction(completions.create)
    assert inspect.iscoroutinefunction(completions.create.__wrapped__)
    bind_completion_options(client, completion_kwargs({
        'base_url': 'https://api.deepseek.com',
        'model': 'deepseek-flash',
    }))
    create = client.chat.completions.create
    assert inspect.iscoroutinefunction(create)
    pending = create(model='deepseek-flash')
    assert inspect.iscoroutine(pending) and not hasattr(pending, 'choices')
    pending.close()
    result = asyncio.run(create(model='deepseek-flash'))
    assert result.choices[0].message.content == '{"statements":[]}'
    assert completions.calls[-1]['extra_body']['thinking'] == {'type': 'disabled'}


def test_async_judge_binding_stays_a_coroutine_and_returns_choices():
    """instructor 用 iscoroutinefunction 决定要不要 await。同步包装会把协程当成补全结果。"""

    class Completions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return _completion('{"statements":[]}')

    class Responses:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(('create', kwargs))
            return _completion()

        async def parse(self, **kwargs):
            self.calls.append(('parse', kwargs))
            return _completion()

    completions = Completions()
    responses = Responses()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        responses=responses,
    )
    options = completion_kwargs({
        'base_url': 'https://api.deepseek.com',
        'model': 'deepseek-flash',
    })
    bind_completion_options(client, options)
    create = client.chat.completions.create
    assert inspect.iscoroutinefunction(create)
    pending = create(model='deepseek-flash')
    assert inspect.iscoroutine(pending)
    assert not hasattr(pending, 'choices')
    pending.close()
    result = asyncio.run(create(
        model='deepseek-flash',
        extra_body={'thinking': {'type': 'enabled'}},
    ))
    assert not inspect.iscoroutine(result)
    assert result.choices[0].message.content == '{"statements":[]}'
    assert completions.calls[-1]['extra_body']['thinking'] == {'type': 'disabled'}
    assert completions.calls[-1]['max_tokens'] == DEEPSEEK_MAX_TOKENS
    parsed = asyncio.run(client.responses.parse(model='deepseek-flash'))
    assert parsed.choices[0].message.content == '材料未记载。'
    assert responses.calls[-1][1]['reasoning'] == {'effort': 'none'}


def test_generation_rejects_an_unawaited_coroutine(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.setenv('EVAL_LLM_MODEL', 'deepseek-flash')

    class Completions:
        async def create(self, **kwargs):
            return _completion()

    chat = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    pipeline = RagPipeline(service=FakeSearch(), chat=chat)
    with pytest.raises(RuntimeError, match='未等待的协程'):
        pipeline.run_item(PROJECT, {
            'id': 'gen',
            'question': '示例问题',
            'reference': '甲',
        }, generate=True)


def test_length_finish_with_empty_content_is_rejected():
    message = SimpleNamespace(content='', reasoning_content='思考占满了额度')
    choice = SimpleNamespace(message=message, finish_reason='length')
    with pytest.raises(RuntimeError, match='reasoning_content'):
        completion_text(SimpleNamespace(choices=[choice]))
    plain = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='材料未记载。'), finish_reason='stop')],
    )
    assert completion_text(plain) == '材料未记载。'


def test_score_records_accept_saved_shapes_and_reject_blank_answers(tmp_path):
    path = tmp_path / 'records.json'
    path.write_text(json.dumps([_score_record(reference='  ')], ensure_ascii=False), encoding='utf-8')
    loaded = load_score_records(path)
    assert loaded[0]['reference'] == ''
    assert loaded[0]['retrieved_contexts'] == ['办公材料片段']
    wrapped = tmp_path / 'wrapped.json'
    wrapped.write_text(json.dumps({'records': [_score_record(answer='材料未记载。')]}, ensure_ascii=False), encoding='utf-8')
    # response is already present; answer is ignored when response exists.
    assert load_score_records(wrapped)[0]['response'] == '材料未记载。'
    alias = tmp_path / 'alias.json'
    body = _score_record()
    body.pop('response')
    body.pop('user_input')
    body['question'] = '示例主体的法定代表人是谁？'
    body['answer'] = '甲。'
    alias.write_text(json.dumps([body], ensure_ascii=False), encoding='utf-8')
    aliased = load_score_records(alias)[0]
    assert aliased['user_input'] == '示例主体的法定代表人是谁？'
    assert aliased['response'] == '甲。'
    blank = tmp_path / 'blank.json'
    blank.write_text(json.dumps([_score_record(response='  ')], ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='response 为空'):
        load_score_records(blank)
    missing = tmp_path / 'missing.json'
    missing.write_text('[]', encoding='utf-8')
    with pytest.raises(ValueError, match='非空'):
        load_score_records(missing)
    with pytest.raises(FileNotFoundError):
        load_score_records(tmp_path / 'absent.json')


def test_record_replay_returns_the_stored_answer_without_retrieval():
    records = [
        _score_record(),
        _score_record(id='q2', user_input='示例主体的注册资本是多少？', response='材料未记载。', reference=''),
    ]
    replay = RecordReplay(records)
    app = QueryApp(replay, PROJECT)
    answer = app.query(records[0]['user_input'], filters=None)
    assert answer == '材料未记载。'
    assert app.last_contexts == ['办公材料片段']
    assert replay.generate('示例主体的注册资本是多少？', []) == '材料未记载。'
    with pytest.raises(ValueError, match='重复问题'):
        RecordReplay([_score_record(), _score_record(id='q9')])


def test_ragas_score_from_writes_scores_without_retrieval(tmp_path, monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    script = _load_script('run_ragas')
    source = tmp_path / 'records.json'
    records = [_score_record(), _score_record(
        id='q2',
        user_input='示例主体的注册资本是多少？',
        response='材料未记载。',
        reference='',
    )]
    source.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    original = source.read_text(encoding='utf-8')
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'records.json').write_text('keep-records', encoding='utf-8')
    (out / 'traces.json').write_text('keep-traces', encoding='utf-8')
    seen = {}

    def fake_score(rows, llm=None, scorers=None):
        seen['rows'] = rows
        return {
            'metrics_run': ['Faithfulness'],
            'metrics_skipped': ['LLMContextRecall', 'FactualCorrectness'],
            'missing_reference_ids': ['q2'],
            'metric_keys': {},
            'note': 'fixture',
            'aggregates': {'faithfulness': 1.0},
            'rows': [
                {'id': row['id'], 'user_input': row['user_input'], 'faithfulness': 1.0}
                for row in rows
            ],
        }

    def explode(*args, **kwargs):
        raise AssertionError('score-from 不应构造检索管道')

    monkeypatch.setattr(script, 'score_records', fake_score)
    monkeypatch.setattr(script, 'RagPipeline', explode)
    script.main(['--score-from', str(source), '--out', str(out)])
    assert [row['id'] for row in seen['rows']] == ['q1', 'q2']
    assert seen['rows'][0]['retrieved_contexts'] == ['办公材料片段']
    scores = json.loads((out / 'ragas_scores.json').read_text(encoding='utf-8'))
    assert scores['rows'][0]['faithfulness'] == 1.0
    csv_text = (out / 'ragas_scores.csv').read_text(encoding='utf-8-sig')
    assert 'faithfulness' in csv_text and 'q1' in csv_text
    assert (out / 'records.json').read_text(encoding='utf-8') == 'keep-records'
    assert (out / 'traces.json').read_text(encoding='utf-8') == 'keep-traces'
    assert source.read_text(encoding='utf-8') == original
    with pytest.raises(ValueError, match='retrieve-only'):
        script.main(['--score-from', str(source), '--retrieve-only'])
    with pytest.raises(ValueError, match='project-id'):
        script.main(['--score-from', str(source), '--project-id', PROJECT])
    with pytest.raises(SystemExit):
        script.parse_args(['--dataset', str(source), '--score-from', str(source)])
    with pytest.raises(SystemExit):
        script.parse_args([])


def test_score_records_aggregates_and_redacts_without_ragas(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    script = _load_script('run_ragas')

    class Metric:
        def __init__(self, value):
            self.value = value
            self.calls = []

        def score(self, **kwargs):
            self.calls.append(kwargs)
            if self.value is None:
                raise RuntimeError('failed with eval-key')
            return SimpleNamespace(value=self.value)

    good = Metric(0.5)
    records = [_score_record(), _score_record(id='q2', user_input='第二个示例问题是什么？')]
    scored = script.score_records(records, scorers=[
        ('faithfulness', good, script._faithfulness_kwargs),
    ])
    assert scored['aggregates']['faithfulness'] == 0.5
    assert good.calls[0]['retrieved_contexts'] == ['办公材料片段']
    assert 'eval-key' not in json.dumps(good.calls[0], ensure_ascii=False)
    with pytest.raises(RuntimeError, match='打分失败') as caught:
        script.score_records(records, scorers=[
            ('faithfulness', Metric(None), script._faithfulness_kwargs),
        ])
    assert 'eval-key' not in str(caught.value)


def test_ragas_factory_forwards_deepseek_options_without_tools(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    script = _load_script('run_ragas')
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            return _completion()

    judge = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    def fake_client(config, async_client=False):
        captured['async_client'] = async_client
        captured['config_model'] = config['model']
        return judge

    def llm_factory(model, **kwargs):
        captured['model'] = model
        captured['kwargs'] = kwargs
        return 'llm'

    fake_llms = types.ModuleType('ragas.llms')
    fake_llms.llm_factory = llm_factory
    monkeypatch.setitem(sys.modules, 'ragas.llms', fake_llms)
    monkeypatch.setattr(script, 'openai_client', fake_client)
    assert script.make_ragas_llm() == 'llm'
    assert captured['async_client'] is True
    assert inspect.iscoroutinefunction(judge.chat.completions.create)
    assert captured['model'] == 'deepseek-flash'
    assert captured['kwargs']['provider'] == 'openai'
    assert captured['kwargs']['client'] is judge
    assert captured['kwargs']['extra_body']['thinking'] == {'type': 'disabled'}
    assert captured['kwargs']['extra_body']['reasoning_effort'] == 'none'
    assert captured['kwargs']['max_tokens'] == DEEPSEEK_MAX_TOKENS
    assert 'tools' not in captured['kwargs']

    class SyncCompletions:
        def create(self, **kwargs):
            return _completion()

    sync_judge = SimpleNamespace(chat=SimpleNamespace(completions=SyncCompletions()))
    monkeypatch.setattr(script, 'openai_client', lambda config, async_client=False: sync_judge)
    with pytest.raises(RuntimeError, match='协程函数'):
        script.make_ragas_llm()


def test_sync_wrapper_refuses_to_treat_a_coroutine_as_a_completion():
    class Completions:
        def create(self, **kwargs):
            async def pending():
                return _completion()

            return pending()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
        responses=None,
    )
    bind_completion_options(client, completion_kwargs({
        'base_url': 'https://api.deepseek.com',
        'model': 'deepseek-flash',
    }))
    with pytest.raises(RuntimeError, match='未等待的协程'):
        client.chat.completions.create(model='deepseek-flash')


def test_trulens_score_from_replays_records_without_retrieval(tmp_path, monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    script = _load_script('run_trulens')
    source = tmp_path / 'records.json'
    source.write_text(json.dumps([_score_record(search_status='candidates')], ensure_ascii=False), encoding='utf-8')
    out = tmp_path / 'trulens-out'
    built = {}

    class DummyTru:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_build(project_id, app_version, database_url, pipeline=None):
        assert pipeline is not None
        assert pipeline.__class__.__name__ == 'RecordReplay'
        built['pipeline'] = pipeline
        built['database_url'] = database_url
        app = script.QueryApp(pipeline, project_id)
        session = SimpleNamespace(get_leaderboard=lambda: [{'Groundedness': 0.5}])
        return session, app, DummyTru()

    def explode(*args, **kwargs):
        raise AssertionError('score-from 不应构造检索管道')

    monkeypatch.setattr(script, 'build_wrapped_app', fake_build)
    monkeypatch.setattr(script, 'RagPipeline', explode)
    script.main([
        '--score-from', str(source),
        '--out', str(out),
        '--app-version', 'scored',
    ])
    answers = json.loads((out / 'trulens_answers.json').read_text(encoding='utf-8'))
    assert answers[0]['answer'] == '材料未记载。'
    assert answers[0]['context_count'] == 1
    assert answers[0]['search_status'] == 'candidates'
    assert answers[0]['replayed'] is True
    assert answers[0]['app_version'] == 'scored'
    board = json.loads((out / 'trulens_leaderboard.json').read_text(encoding='utf-8'))
    assert board[0]['Groundedness'] == 0.5
    assert not (out / 'records.json').exists()
    assert not (out / 'traces.json').exists()
    assert 'trulens.sqlite' in built['database_url']
    with pytest.raises(ValueError, match='重复问题'):
        duplicate = tmp_path / 'dup.json'
        duplicate.write_text(json.dumps([
            _score_record(),
            _score_record(id='q9'),
        ], ensure_ascii=False), encoding='utf-8')
        script.main(['--score-from', str(duplicate), '--out', str(tmp_path / 'dup-out')])


def test_trulens_provider_receives_a_bound_client_for_deepseek(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    script = _load_script('run_trulens')
    sentinel = object()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_mod = types.ModuleType('trulens.providers.openai')
    fake_mod.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, 'trulens.providers.openai', fake_mod)
    monkeypatch.setattr(script, 'openai_client', lambda config: sentinel)
    provider = script.make_provider()
    assert provider.kwargs == {'model_engine': 'deepseek-flash', 'client': sentinel}
    monkeypatch.delenv('EVAL_LLM_BASE_URL', raising=False)
    monkeypatch.setenv('EVAL_LLM_MODEL', 'gpt-4o-mini')
    plain = script.make_provider()
    assert plain.kwargs['model_engine'] == 'gpt-4o-mini'
    assert plain.kwargs['api_key'] == 'eval-key'
    assert 'client' not in plain.kwargs
    assert 'extra_body' not in plain.kwargs


_JUDGE_ENV = (
    'EVAL_LLM_API_KEY',
    'EVAL_LLM_BASE_URL',
    'EVAL_LLM_MODEL',
    'OPENAI_API_KEY',
    'OPENAI_BASE_URL',
)


@contextmanager
def _without_judge_env():
    """清掉裁判变量，结束时还原，包括 load_dotenv 写进去的值。"""
    saved = {name: os.environ.get(name) for name in _JUDGE_ENV}
    for name in _JUDGE_ENV:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _script_pipeline(script):
    """入口脚本经 sys.path 导入的 pipeline，和本文件顶部那个模块不是同一个对象。"""
    return sys.modules[script.load_eval_env.__module__]


def _watch_env_then_key(script, monkeypatch):
    order = []
    seen = []
    real_load = script.load_eval_env
    real_require = script.require_api_key

    def load_eval_env():
        order.append('load')
        real_load()

    def require_api_key(config=None):
        order.append('require')
        resolved = real_require(config)
        seen.append({
            'api_key': resolved['api_key'],
            'base_url': resolved['base_url'],
            'model': resolved['model'],
        })
        return resolved

    monkeypatch.setattr(script, 'load_eval_env', load_eval_env)
    monkeypatch.setattr(script, 'require_api_key', require_api_key)
    return order, seen


def _silence_ragas(script, monkeypatch):
    monkeypatch.setattr(script, 'RagPipeline', lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(script, 'collect_records', lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(script, 'score_records', lambda *args, **kwargs: {
        'metrics_run': ['Faithfulness'],
        'metrics_skipped': [],
        'missing_reference_ids': [],
        'metric_keys': {},
        'note': 'fixture',
        'aggregates': {'faithfulness': 1.0},
        'rows': [{'id': 'q1', 'user_input': '示例', 'faithfulness': 1.0}],
    })


def _silence_trulens(script, monkeypatch):
    class DummyTru:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_build(project_id, app_version, database_url, pipeline=None):
        if pipeline is None:
            app = SimpleNamespace(
                last_contexts=[],
                last_found={'status': 'candidates'},
                pipeline=SimpleNamespace(limits=None),
                query=lambda query, filters=None: '材料未记载。',
            )
        else:
            app = script.QueryApp(pipeline, project_id)
        session = SimpleNamespace(get_leaderboard=lambda: None)
        return session, app, DummyTru()

    monkeypatch.setattr(script, 'build_wrapped_app', fake_build)
    monkeypatch.setattr(script, 'retrieve_only', lambda *args, **kwargs: None)
    monkeypatch.setattr(script, 'RagPipeline', lambda *args, **kwargs: SimpleNamespace())


def test_load_eval_env_keeps_process_values_and_fills_the_rest(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text(
        'EVAL_LLM_API_KEY=from-dotenv\n'
        'EVAL_LLM_BASE_URL=https://api.deepseek.com\n'
        'EVAL_LLM_MODEL=from-dotenv-model\n'
        'OPENAI_API_KEY=from-dotenv-openai\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(pipeline, 'REPO_ROOT', tmp_path)
    with _without_judge_env():
        os.environ['EVAL_LLM_API_KEY'] = 'from-process'
        os.environ['EVAL_LLM_MODEL'] = 'from-process-model'
        pipeline.load_eval_env()
        assert os.environ['EVAL_LLM_API_KEY'] == 'from-process'
        assert os.environ['EVAL_LLM_MODEL'] == 'from-process-model'
        assert os.environ['EVAL_LLM_BASE_URL'] == 'https://api.deepseek.com'
        assert os.environ['OPENAI_API_KEY'] == 'from-dotenv-openai'
        os.environ.pop('EVAL_LLM_API_KEY')
        pipeline.load_eval_env()
        assert os.environ['EVAL_LLM_API_KEY'] == 'from-dotenv'
        assert os.environ['EVAL_LLM_MODEL'] == 'from-process-model'


def test_eval_clis_load_repo_env_before_the_judge_key(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text(
        'EVAL_LLM_API_KEY=from-dotenv\n'
        'EVAL_LLM_BASE_URL=https://api.deepseek.com\n'
        'EVAL_LLM_MODEL=from-dotenv-model\n',
        encoding='utf-8',
    )
    dataset = _write(tmp_path, _body())
    records = tmp_path / 'records.json'
    records.write_text(json.dumps([_score_record()], ensure_ascii=False), encoding='utf-8')
    ragas = _load_script('run_ragas')
    trulens = _load_script('run_trulens')
    monkeypatch.setattr(_script_pipeline(ragas), 'REPO_ROOT', tmp_path)
    assert trulens.load_eval_env.__globals__ is ragas.load_eval_env.__globals__
    _silence_ragas(ragas, monkeypatch)
    _silence_trulens(trulens, monkeypatch)
    watched = (
        (ragas, *_watch_env_then_key(ragas, monkeypatch)),
        (trulens, *_watch_env_then_key(trulens, monkeypatch)),
    )

    with _without_judge_env():
        os.environ['EVAL_LLM_MODEL'] = 'from-process'
        for script, order, seen in watched:
            order.clear()
            seen.clear()
            script.main(['--score-from', str(records), '--out', str(tmp_path / f'{script.__name__}-score')])
            assert order == ['load', 'require'], script.__name__
            assert seen[-1] == {
                'api_key': 'from-dotenv',
                'base_url': 'https://api.deepseek.com',
                'model': 'from-process',
            }
            order.clear()
            seen.clear()
            script.main(['--dataset', str(dataset), '--out', str(tmp_path / f'{script.__name__}-run')])
            assert order == ['load', 'require'], script.__name__
            assert seen[0]['api_key'] == 'from-dotenv'
            assert seen[0]['model'] == 'from-process'

        for name in _JUDGE_ENV:
            os.environ.pop(name, None)
        (tmp_path / '.env').write_text('\n', encoding='utf-8')
        for script, order, seen in watched:
            order.clear()
            with pytest.raises(RuntimeError, match='EVAL_LLM_API_KEY'):
                script.main(['--score-from', str(records), '--out', str(tmp_path / f'{script.__name__}-missing')])
            assert order == ['load', 'require'], script.__name__
            order.clear()
            script.main([
                '--dataset', str(dataset),
                '--out', str(tmp_path / f'{script.__name__}-retrieve'),
                '--retrieve-only',
            ])
            assert order == ['load'], script.__name__


def test_factual_scores_use_recall_direction_not_precision_tp():
    factual = _load_script('factual')
    asymmetric = factual.scores_from_verdicts([0], [1])
    assert asymmetric == {'precision': 0.0, 'recall': 1.0, 'f1': 0.0}
    # ragas 0.4.4 的 recall 分子是 precision 方向的 TP。这里 TP=0、FN=0，官方 recall 为 0。
    official_recall = 0 / (0 + 0 + 1e-8)
    assert round(official_recall, 2) == 0.0
    assert asymmetric['recall'] == 1.0
    assert factual.scores_from_verdicts([0, 0], [0]) == {
        'precision': 0.0, 'recall': 0.0, 'f1': 0.0,
    }
    assert factual.scores_from_verdicts([1], [1]) == {
        'precision': 1.0, 'recall': 1.0, 'f1': 1.0,
    }
    assert factual.scores_from_verdicts([1, 0], [1]) == {
        'precision': 0.5, 'recall': 1.0, 'f1': 0.67,
    }
    assert factual.verdict_counts([1, 0, 2]) == {
        'supported': 1, 'unsupported': 1, 'other': 1,
    }


def test_chinese_factual_prompts_are_not_the_english_defaults():
    factual = _load_script('factual')
    assert '多出来的非矛盾措辞' in factual.NLI_INSTRUCTION
    assert '直接推断' in factual.NLI_INSTRUCTION
    assert 'directly inferred' not in factual.NLI_INSTRUCTION
    assert 'Charles Babbage' not in factual.CLAIM_INSTRUCTION
    assert '2018-03-15' in factual.CLAIM_INSTRUCTION
    assert '100万元' in factual.NLI_INSTRUCTION
    assert '百分之三十' in factual.NLI_INSTRUCTION
    assert '稍长的句子' in factual.NLI_INSTRUCTION
    wrapped, other_field, wrong_date = factual.NLI_EXAMPLES[0]['statements']
    assert wrapped['verdict'] == 1
    assert '2018-03-15' in wrapped['statement']
    assert '根据材料' in wrapped['statement']
    assert other_field['verdict'] == 0 and '法定代表人' in other_field['statement']
    assert wrong_date['verdict'] == 0 and '2019' in wrong_date['statement']
    percent, amount, conflict = factual.NLI_EXAMPLES[1]['statements']
    assert percent['verdict'] == 1 and '百分之三十' in percent['statement']
    assert amount['verdict'] == 1 and '1,000,000' in amount['statement']
    assert conflict['verdict'] == 0 and '13%' in conflict['statement']
    kept = factual.CLAIM_EXAMPLES[('low', 'low')][0]
    assert kept[0].startswith('根据材料')
    assert kept[1] == ['示例主体的成立日期为2018-03-15。']
    assert set(factual.CLAIM_EXAMPLES) == {
        ('low', 'low'), ('low', 'high'), ('high', 'low'), ('high', 'high'),
    }
    readme = (EVAL_DIR / 'README.md').read_text(encoding='utf-8')
    ragas = (EVAL_DIR / 'run_ragas.py').read_text(encoding='utf-8')
    assert 'factual_correctness_precision' in readme
    assert 'factual_correctness_recall' in readme
    assert 'factual_judge_trace.json' in readme
    assert 'build_factual_metric' in ragas
    assert 'FactualCorrectness(llm=llm)' not in ragas


def test_score_records_persists_precision_recall_and_compact_trace(tmp_path):
    script = _load_script('run_ragas')
    response = '根据材料，示例主体的成立日期为2018-03-15。'
    reference = '示例主体成立于2018年3月15日。'

    class Metric:
        def score(self, **kwargs):
            result = SimpleNamespace(value=0.0, precision=0.0, recall=1.0, f1=0.0)
            result.judge_trace = {
                'response_claim_count': 1,
                'reference_claim_count': 1,
                'precision_nli': {'supported': 0, 'unsupported': 1, 'other': 0},
                'recall_nli': {'supported': 1, 'unsupported': 0, 'other': 0},
                'precision': 0.0,
                'recall': 1.0,
                'f1': 0.0,
                'response': kwargs['response'],
                'reference': kwargs['reference'],
                'claims': [kwargs['response']],
            }
            return result

    scored = script.score_records([
        _score_record(response=response, reference=reference),
    ], scorers=[
        ('factual_correctness', Metric(), script._factual_kwargs),
    ])
    row = scored['rows'][0]
    assert row['factual_correctness_precision'] == 0.0
    assert row['factual_correctness_recall'] == 1.0
    assert row['factual_correctness_f1'] == 0.0
    assert row['factual_correctness'] == row['factual_correctness_f1']
    assert scored['aggregates']['factual_correctness_recall'] == 1.0
    assert scored['aggregates']['factual_correctness_precision'] == 0.0
    out = tmp_path / 'scores'
    script.write_score_bundle(out, scored)
    trace = json.loads((out / 'factual_judge_trace.json').read_text(encoding='utf-8'))
    blob = json.dumps(trace, ensure_ascii=False)
    assert response not in blob
    assert reference not in blob
    assert '根据材料' not in blob
    assert 'claims' not in trace['rows'][0]
    assert trace['rows'][0]['id'] == 'q1'
    assert trace['rows'][0]['recall_nli'] == {'supported': 1, 'unsupported': 0, 'other': 0}
    assert trace['rows'][0]['precision_nli']['unsupported'] == 1
    scores = json.loads((out / 'ragas_scores.json').read_text(encoding='utf-8'))
    assert 'factual_judge_trace' not in scores
    assert scores['rows'][0]['factual_correctness_recall'] == 1.0
    csv_text = (out / 'ragas_scores.csv').read_text(encoding='utf-8-sig')
    assert 'factual_correctness_precision' in csv_text
    assert 'factual_correctness_recall' in csv_text
    assert response not in csv_text


def _last_prompt_input(prompt):
    marker = '\ninput: '
    index = prompt.rfind(marker)
    raw = prompt[index + len(marker):].split('\nOutput:', 1)[0].strip()
    return json.loads(raw)


def _fact_value(text):
    import re

    facts = {}
    date_cn = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    date_num = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', text)
    match = date_cn or date_num
    if '成立' in text:
        if match:
            year, month, day = match.groups()
            facts['established'] = f'{int(year):04d}-{int(month):02d}-{int(day):02d}'
        else:
            facts['established'] = None
    if '法定代表人' in text:
        person = re.search(r'法定代表人(?:是|为)\s*([^\s，。,；;]+)', text)
        facts['representative'] = person.group(1) if person else None
    if '注册资本' in text:
        wan = re.search(r'(\d+(?:\.\d+)?)\s*万元', text)
        yuan = re.search(r'([\d,]+(?:\.\d+)?)\s*元', text)
        if wan:
            facts['capital'] = float(wan.group(1)) * 10000
        elif yuan:
            facts['capital'] = float(yuan.group(1).replace(',', ''))
        else:
            facts['capital'] = None
    if '持股' in text:
        percent = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
        words = re.search(r'百分之\s*(\d+(?:\.\d+)?|三十|十三)', text)
        if percent:
            facts['holding'] = float(percent.group(1))
        elif words:
            token = words.group(1)
            facts['holding'] = {'三十': 30.0, '十三': 13.0}.get(token)
            if facts['holding'] is None:
                facts['holding'] = float(token)
        else:
            facts['holding'] = None
    return facts


def _aligned_verdict(context, statement, relaxed):
    context_facts = _fact_value(context)
    statement_facts = _fact_value(statement)
    if not statement_facts:
        return 0
    for field, value in statement_facts.items():
        if field not in context_facts or context_facts[field] != value or value is None:
            return 0
    if relaxed:
        return 1
    for phrase in ('根据材料', '如下', '该公司'):
        if phrase in statement and phrase not in context:
            return 0
    return 1


def _scripted_judge():
    from ragas.llms.base import InstructorBaseRagasLLM
    from ragas.metrics.collections.factual_correctness.util import StatementFaithfulnessAnswer

    class ScriptedJudge(InstructorBaseRagasLLM):
        """按提示语选择宽对齐或英文默认的逐字规则。不调用外部模型。"""

        def generate(self, prompt, response_model):
            return asyncio.run(self.agenerate(prompt, response_model))

        async def agenerate(self, prompt, response_model):
            payload = _last_prompt_input(prompt)
            if response_model.__name__ == 'ClaimDecompositionOutput':
                return response_model(claims=[payload['response']])
            if response_model.__name__ != 'NLIStatementOutput':
                raise AssertionError(response_model.__name__)
            relaxed = '多出来的非矛盾措辞' in prompt
            return response_model(statements=[
                StatementFaithfulnessAnswer(
                    statement=statement,
                    reason='synthetic',
                    verdict=_aligned_verdict(payload['context'], statement, relaxed),
                )
                for statement in payload['statements']
            ])

    return ScriptedJudge()


def test_rendered_chinese_prompts_replace_english_defaults():
    pytest.importorskip('ragas')
    from ragas.metrics.collections.factual_correctness.util import (
        ClaimDecompositionInput,
        ClaimDecompositionPrompt,
        NLIStatementInput,
        NLIStatementPrompt,
    )

    factual = _load_script('factual')
    claim = factual.chinese_claim_prompt()
    nli = factual.chinese_nli_prompt()
    claim_text = claim.to_string(ClaimDecompositionInput(
        response='示例主体成立于2018年3月15日。',
        atomicity='low',
        coverage='low',
    ))
    high_text = claim.to_string(ClaimDecompositionInput(
        response='示例主体成立于2018年3月15日。',
        atomicity='low',
        coverage='high',
    ))
    nli_text = nli.to_string(NLIStatementInput(
        context='示例主体成立于2018年3月15日。',
        statements=['根据材料，示例主体的成立日期为2018-03-15。'],
    ))
    assert '套话不要单独成条' in claim_text
    assert 'Charles Babbage' not in claim_text
    assert '示例行业' in claim_text
    assert '持股比例为30%' in high_text
    assert '多出来的非矛盾措辞' in nli_text
    assert '2018-03-15' in nli_text
    assert 'directly inferred' not in nli_text
    assert claim.language == 'chinese'
    assert nli.language == 'chinese'
    english_claim = ClaimDecompositionPrompt().to_string(ClaimDecompositionInput(
        response='x', atomicity='low', coverage='low',
    ))
    english_nli = NLIStatementPrompt().to_string(NLIStatementInput(
        context='x', statements=['y'],
    ))
    assert 'Charles Babbage' in english_claim
    assert 'directly inferred' in english_nli
    assert claim.instruction != ClaimDecompositionPrompt.instruction
    assert nli.instruction != NLIStatementPrompt.instruction


def test_wrapped_chinese_fact_is_not_zero_and_conflict_stays_low():
    pytest.importorskip('ragas')
    from ragas.metrics.collections import FactualCorrectness

    factual = _load_script('factual')
    script = _load_script('run_ragas')
    judge = _scripted_judge()
    metric = factual.build_factual_metric(judge)
    official_recall = FactualCorrectness(llm=judge, mode='recall')
    official_f1 = FactualCorrectness(llm=judge, mode='f1')
    assert official_recall.mode == 'recall'
    assert 'directly inferred' in official_recall.nli_prompt.instruction
    assert 'directly inferred' not in metric.nli_prompt.instruction
    assert metric.__class__.__name__ == 'DueDiligenceFactualCorrectness'

    response = '根据材料，示例主体的成立日期为2018-03-15。'
    reference = '示例主体成立于2018年3月15日。'
    result = metric.score(response=response, reference=reference)
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.value == 1.0
    assert official_recall.score(response=response, reference=reference).value == 0.0
    assert official_f1.score(response=response, reference=reference).value == 0.0
    blob = json.dumps(result.judge_trace, ensure_ascii=False)
    assert response not in blob
    assert reference not in blob
    assert result.judge_trace['response_claim_count'] == 1
    assert result.judge_trace['reference_claim_count'] == 1
    assert result.judge_trace['precision_nli']['supported'] == 1
    assert result.judge_trace['recall_nli']['supported'] == 1

    wrong_field = metric.score(
        response='示例主体的法定代表人是甲。',
        reference=reference,
    )
    assert wrong_field.precision == 0.0
    assert wrong_field.recall == 0.0
    assert wrong_field.value == 0.0
    conflict = metric.score(
        response='示例主体的持股比例为13%。',
        reference='示例主体的持股比例为30%。',
    )
    assert conflict.precision == 0.0
    assert conflict.recall == 0.0
    assert conflict.value == 0.0
    percent = metric.score(
        response='根据材料，示例主体持股百分之三十。',
        reference='示例主体的持股比例为30%。',
    )
    assert percent.precision == 1.0 and percent.recall == 1.0 and percent.value == 1.0
    amount = metric.score(
        response='示例主体的注册资本为1,000,000元。',
        reference='示例主体的注册资本是100万元。',
    )
    assert amount.precision == 1.0 and amount.recall == 1.0 and amount.value == 1.0

    scored = script.score_records([
        _score_record(
            id='date-wrap',
            user_input='示例主体何时成立？',
            response=response,
            reference=reference,
            retrieved_contexts=['合成片段'],
        ),
    ], scorers=[
        ('factual_correctness', metric, script._factual_kwargs),
    ])
    row = scored['rows'][0]
    assert row['factual_correctness_precision'] == 1.0
    assert row['factual_correctness_recall'] == 1.0
    assert row['factual_correctness'] == 1.0
    trace_blob = json.dumps(scored['factual_judge_trace'], ensure_ascii=False)
    assert response not in trace_blob
    assert reference not in trace_blob
    assert '合成片段' not in trace_blob


def test_installed_ragas_recall_mode_still_reuses_precision_tp():
    pytest.importorskip('ragas')
    import numpy as np
    from ragas.metrics.collections import FactualCorrectness

    factual = _load_script('factual')
    judge = _scripted_judge()
    metric = factual.build_factual_metric(judge)

    async def claims_and_verdicts(text, against):
        if against == 'gold':
            return ['kept-out-of-trace'], [0]
        return ['kept-out-of-trace'], [1]

    metric._claims_and_verdicts = claims_and_verdicts
    result = metric.score(response='response-text', reference='gold')
    assert result.precision == 0.0
    assert result.recall == 1.0
    assert result.f1 == 0.0
    assert 'kept-out-of-trace' not in json.dumps(result.judge_trace, ensure_ascii=False)
    assert 'response-text' not in json.dumps(result.judge_trace, ensure_ascii=False)
    assert result.judge_trace['recall_nli']['supported'] == 1
    assert result.judge_trace['precision_nli']['unsupported'] == 1

    async def direction(text, other):
        if text == 'response-text':
            return np.array([False])
        return np.array([True])

    official_recall = FactualCorrectness(llm=judge, mode='recall')
    official_recall._decompose_and_verify_claims = direction
    official_f1 = FactualCorrectness(llm=judge, mode='f1')
    official_f1._decompose_and_verify_claims = direction
    assert official_recall.score(response='response-text', reference='gold').value == 0.0
    assert official_f1.score(response='response-text', reference='gold').value == 0.0
