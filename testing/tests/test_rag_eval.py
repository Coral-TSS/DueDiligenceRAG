"""RAG 评测包的离线检查。不连接数据库，不加载模型，不读客户文件或密钥。"""
import importlib.util
import inspect
import json
import sys
import types
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
    assert sent['max_tokens'] == DEEPSEEK_MAX_TOKENS
    assert sent['extra_body'] == DEEPSEEK_EXTRA_BODY
    assert sent['extra_body']['thinking'] == {'type': 'disabled'}
    assert sent['extra_body']['reasoning_effort'] == 'none'
    assert 'tools' not in sent
    assert 'deepseek_web_search' not in json.dumps(sent, ensure_ascii=False)
    assert sent['messages'][0]['content'] == GENERATION_SYSTEM
    assert '办公材料片段' in sent['messages'][1]['content']
    assert 'eval-key' not in json.dumps(sent, ensure_ascii=False)
    info = describe_judge_llm()
    assert info == {
        'model': 'deepseek-chat',
        'base_url': 'https://api.deepseek.com',
        'api_key_set': True,
        'thinking_disabled': True,
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
    client.chat.completions.create(model='deepseek-flash')
    assert completions.calls[1]['max_tokens'] == DEEPSEEK_MAX_TOKENS
    client.responses.create(model='deepseek-flash', reasoning={'summary': 'auto'})
    client.responses.parse(model='deepseek-flash')
    created = responses.calls[0][1]['reasoning']
    assert created['effort'] == 'none'
    assert created['summary'] == 'auto'
    assert responses.calls[1][1]['reasoning'] == {'effort': 'none'}
    assert 'max_tokens' not in responses.calls[0][1]
    assert 'extra_body' not in responses.calls[0][1]


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
    sentinel = object()

    def fake_client(config, async_client=False):
        captured['async_client'] = async_client
        captured['config_model'] = config['model']
        return sentinel

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
    assert captured['model'] == 'deepseek-flash'
    assert captured['kwargs']['provider'] == 'openai'
    assert captured['kwargs']['client'] is sentinel
    assert captured['kwargs']['extra_body']['thinking'] == {'type': 'disabled'}
    assert captured['kwargs']['extra_body']['reasoning_effort'] == 'none'
    assert captured['kwargs']['max_tokens'] == DEEPSEEK_MAX_TOKENS
    assert 'tools' not in captured['kwargs']


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
