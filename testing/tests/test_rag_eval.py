"""RAG 评测包的离线检查。不连接数据库，不加载模型，不读客户文件或密钥。"""
import importlib.util
import inspect
import json
import sys
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
GENERATION_SYSTEM = pipeline.GENERATION_SYSTEM
NO_CONTEXT_ANSWER = pipeline.NO_CONTEXT_ANSWER
QueryApp = pipeline.QueryApp
RagPipeline = pipeline.RagPipeline
RetrievalPipeline = pipeline.RetrievalPipeline
contexts_from_search = pipeline.contexts_from_search
describe_judge_llm = pipeline.describe_judge_llm
filter_payload = pipeline.filter_payload
judge_llm_config = pipeline.judge_llm_config
load_dataset = pipeline.load_dataset
metric_skip_message = pipeline.metric_skip_message
prepare_run = pipeline.prepare_run
select_metric_names = pipeline.select_metric_names


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

    def search(self, project_id, query, *, document_id=None, scope=None, cancel=None):
        self.calls.append({
            'project_id': project_id,
            'query': query,
            'document_id': document_id,
            'scope': scope,
            'cancel': cancel,
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
    assert sent['messages'][0]['content'] == GENERATION_SYSTEM
    assert '办公材料片段' in sent['messages'][1]['content']
    assert 'eval-key' not in json.dumps(sent, ensure_ascii=False)
    info = describe_judge_llm()
    assert info == {
        'model': 'deepseek-chat',
        'base_url': 'https://api.deepseek.com',
        'api_key_set': True,
    }
    assert 'eval-key' not in json.dumps(info)


def test_judge_env_prefers_eval_variables_and_hides_query_secrets(monkeypatch):
    monkeypatch.setenv('EVAL_LLM_API_KEY', 'eval-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-key')
    monkeypatch.setenv('EVAL_LLM_BASE_URL', 'https://api.deepseek.com?token=hidden')
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    config = judge_llm_config()
    assert config['api_key'] == 'eval-key'
    assert config['model'] == 'deepseek-chat'
    info = describe_judge_llm(config)
    assert info['base_url'] == 'https://api.deepseek.com'
    assert 'hidden' not in json.dumps(info)
    assert 'eval-key' not in json.dumps(info)

    monkeypatch.delenv('EVAL_LLM_API_KEY', raising=False)
    monkeypatch.delenv('EVAL_LLM_BASE_URL', raising=False)
    monkeypatch.delenv('EVAL_LLM_MODEL', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'openai-key')
    fallback = judge_llm_config()
    assert fallback['api_key'] == 'openai-key'
    assert fallback['model'] == 'gpt-4o-mini'
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
