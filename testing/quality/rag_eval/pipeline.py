"""尽调 RAG 评测链路：黄金集 → 检索 →（可选）生成 → 记录。

检索只调用现有 EvidenceService.search，不另建向量库。
生成只根据检索片段作答，供 RAGAS / TruLens 的答案类指标使用。
本模块不导入 ragas 或 trulens，默认测试环境可以单独加载。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_DIR = Path(__file__).resolve().parent

QUESTION_MIN = 1
QUESTION_MAX = 500
ZERO_PROJECT_ID = '00000000-0000-0000-0000-000000000000'
NO_CONTEXT_ANSWER = '检索没有返回可用片段，因此无法根据材料作答。'
GENERATION_SYSTEM = (
    '你只根据给定的检索片段作答。片段里没有的信息必须明确写成“材料未记载”，'
    '不要推断，也不要补充片段以外的知识。用中文回答。'
)
REFERENCE_METRICS = ('LLMContextRecall', 'FactualCorrectness')
FILTER_KEYS = (
    'document_id', 'document_ids', 'categories', 'subject_ids',
    'date_point', 'period_start', 'period_end', 'page_from', 'page_to',
)
CHILD_KEYS = ('matched_children', 'children', 'chunks')


def load_eval_env():
    """先读仓库根目录 .env（数据库和本地模型），不覆盖已经设置的裁判模型变量。"""
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / '.env', override=False)


def parse_project_id(value):
    text = str(value or '').strip()
    if not text:
        raise ValueError('数据集缺少 project_id。')
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError('project_id 必须是 UUID。') from exc
    if parsed.int == 0 or str(parsed) == ZERO_PROJECT_ID:
        raise ValueError('project_id 不能使用全零占位符。请换成本机已索引项目的 UUID。')
    return str(parsed)


def load_dataset(path):
    """读取调用方给出的 JSON 黄金集。路径可以在仓库外。"""
    file = Path(path)
    try:
        payload = json.loads(file.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'数据集不是合法 JSON：{exc}') from exc
    if not isinstance(payload, dict):
        raise ValueError('数据集必须是 JSON 对象。')
    project_id = parse_project_id(payload.get('project_id'))
    items = payload.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError('数据集 items 必须是非空列表。')
    normalized = [normalize_item(item, index) for index, item in enumerate(items)]
    return {
        'project_id': project_id,
        'items': normalized,
        'customer_data': bool(payload.get('customer_data', False)),
    }


def prepare_run(path, project_id_override=''):
    dataset = load_dataset(path)
    override = str(project_id_override or '').strip()
    project_id = parse_project_id(override) if override else dataset['project_id']
    return dataset, project_id


def normalize_item(item, index):
    if not isinstance(item, dict):
        raise ValueError(f'第 {index + 1} 条必须是对象。')
    question = str(item.get('question') or '').strip()
    if not QUESTION_MIN <= len(question) <= QUESTION_MAX:
        raise ValueError(f'第 {index + 1} 条 question 须为 {QUESTION_MIN}–{QUESTION_MAX} 个字符。')
    reference = item.get('reference', '')
    if reference is None:
        reference = ''
    if not isinstance(reference, str):
        raise ValueError(f'第 {index + 1} 条 reference 须为字符串。')
    item_id = item.get('id')
    if item_id is None or str(item_id).strip() == '':
        item_id = f'item-{index + 1}'
    normalized = {
        'id': str(item_id).strip(),
        'question': question,
        'reference': reference.strip(),
    }
    try:
        normalized.update(filter_payload(item))
        build_scope(normalized)
    except ValueError as exc:
        raise ValueError(f'第 {index + 1} 条过滤条件无效：{exc}') from exc
    return normalized


def filter_payload(item):
    """只保留检索过滤字段。空数组和空字符串视为未设置。"""
    if not isinstance(item, dict):
        raise ValueError('过滤条件必须是对象。')
    payload = {}
    for key in FILTER_KEYS:
        if key in ('document_ids', 'categories', 'subject_ids'):
            payload[key] = _optional_list(item, key)
        elif key in ('page_from', 'page_to'):
            payload[key] = item.get(key)
        else:
            payload[key] = _blank_to_none(item.get(key))
    if payload['document_id'] and payload['document_ids']:
        raise ValueError('不能同时传 document_id 与 document_ids。')
    return payload


def build_scope(item):
    """把一条黄金集的过滤字段交给当前 parse_filters。没有过滤时返回 None。"""
    from dd_agent.material_metadata import MetadataError, parse_filters

    filters = filter_payload(item)
    try:
        return parse_filters(
            document_id=filters.get('document_id'),
            document_ids=filters.get('document_ids'),
            categories=filters.get('categories'),
            subject_ids=filters.get('subject_ids'),
            date_point=filters.get('date_point'),
            period_start=filters.get('period_start'),
            period_end=filters.get('period_end'),
            page_from=filters.get('page_from'),
            page_to=filters.get('page_to'),
        )
    except MetadataError as exc:
        raise ValueError(str(exc)) from exc


def _optional_list(item, key):
    if key not in item:
        return None
    value = item.get(key)
    if value is None or value == []:
        return None
    if isinstance(value, str) or not isinstance(value, list):
        raise ValueError(f'{key} 必须是数组。')
    return value


def _blank_to_none(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return value


def contexts_from_search(found):
    """从 EvidenceService.search 的结果抽出评测用文本。

    页级命中和 Office source_unit 命中使用扁平 text。
    父子命中使用 matched_children 的 text；children / chunks 仅作旧结果兼容。
    子块没有文本时才回退到 context.text。
    """
    if not isinstance(found, dict):
        return []
    contexts = []
    seen = set()
    for hit in found.get('results') or []:
        for text in _hit_contexts(hit):
            if text not in seen:
                seen.add(text)
                contexts.append(text)
    return contexts


def _hit_contexts(hit):
    if not isinstance(hit, dict):
        return []
    children = _first_children(hit)
    if children is not None:
        texts = []
        for child in children:
            raw = child.get('text') if isinstance(child, dict) else child
            text = _clean_text(raw)
            if text:
                texts.append(text)
        if texts:
            return texts
    text = _clean_text(hit.get('text'))
    if text:
        return [text]
    context = hit.get('context')
    if isinstance(context, dict):
        parent = _clean_text(context.get('text'))
        if parent:
            return [parent]
    return []


def _first_children(hit):
    for key in CHILD_KEYS:
        value = hit.get(key)
        if isinstance(value, list) and value:
            return value
    return None


def _clean_text(value):
    if not isinstance(value, str):
        return ''
    return value.strip()


def select_metric_names(records):
    """Faithfulness 不需要 reference。另外两项只在每条都有 reference 时保留。"""
    missing = [
        str(record.get('id') or '')
        for record in records
        if not str(record.get('reference') or '').strip()
    ]
    selected = ['Faithfulness']
    skipped = []
    if missing:
        skipped = list(REFERENCE_METRICS)
    else:
        selected.extend(REFERENCE_METRICS)
    return {
        'selected': selected,
        'skipped': skipped,
        'missing_reference_ids': missing,
    }


def metric_skip_message(selection):
    if not selection['skipped']:
        return ''
    ids = '、'.join(item for item in selection['missing_reference_ids'] if item) or '未命名'
    return (
        f"有 {len(selection['missing_reference_ids'])} 条没有 reference（{ids}），"
        '本次跳过 LLMContextRecall（当前 RAGAS 集合指标名 ContextRecall）和 FactualCorrectness，'
        '只计算 Faithfulness。'
    )


def judge_llm_config():
    api_key = (os.environ.get('EVAL_LLM_API_KEY') or os.environ.get('OPENAI_API_KEY') or '').strip()
    base_url = (os.environ.get('EVAL_LLM_BASE_URL') or os.environ.get('OPENAI_BASE_URL') or '').strip() or None
    model = (os.environ.get('EVAL_LLM_MODEL') or '').strip()
    if not model:
        model = 'deepseek-chat' if base_url and 'deepseek' in base_url.lower() else 'gpt-4o-mini'
    return {'api_key': api_key, 'base_url': base_url, 'model': model}


def describe_judge_llm(config=None):
    """供日志使用的裁判模型摘要。不含密钥。"""
    config = config or judge_llm_config()
    base_url = config.get('base_url') or ''
    return {
        'model': config.get('model'),
        'base_url': base_url.split('?', 1)[0] or None,
        'api_key_set': bool(config.get('api_key')),
    }


def require_api_key(config=None):
    config = config or judge_llm_config()
    if config.get('api_key'):
        return config
    raise RuntimeError(
        '未设置裁判模型密钥。请设置 EVAL_LLM_API_KEY，或回退使用 OPENAI_API_KEY。'
        '若用 DeepSeek 等兼容接口，再设置 EVAL_LLM_BASE_URL 和 EVAL_LLM_MODEL。'
    )


def redact_secrets(text, config=None):
    rendered = str(text)
    secret = (config or judge_llm_config()).get('api_key') or ''
    if secret:
        rendered = rendered.replace(secret, '***')
    return rendered


class RetrievalPipeline:
    """同一进程内复用 EvidenceService，避免重复加载本地嵌入和重排模型。"""

    def __init__(self, service=None):
        if service is None:
            load_eval_env()
            from dd_agent.config import Settings
            from dd_agent.retrieval import EvidenceService

            settings = Settings.from_env()
            if not settings.database_url:
                raise RuntimeError('DDA_DATABASE_URL 为空，请先在仓库 .env 中写好数据库连接。')
            service = EvidenceService(settings)
        self.service = service

    def retrieve(self, project_id, question, item=None):
        item = item or {}
        # document_id 已进入 scope.document_ids。再传关键字会和 scope 冲突。
        found = self.service.search(
            project_id,
            question,
            document_id=None,
            scope=build_scope(item),
        )
        return contexts_from_search(found), found


class RagPipeline(RetrievalPipeline):
    """检索后按片段生成答案。没有片段时不调用裁判模型。"""

    def __init__(self, service=None, chat=None):
        super().__init__(service=service)
        self._chat = chat

    def generate(self, question, contexts):
        if not contexts:
            return NO_CONTEXT_ANSWER
        config = require_api_key()
        client = self._chat if self._chat is not None else _openai_client(config)
        joined = '\n\n---\n\n'.join(contexts)
        completion = client.chat.completions.create(
            model=config['model'],
            temperature=0,
            messages=[
                {'role': 'system', 'content': GENERATION_SYSTEM},
                {'role': 'user', 'content': f'问题：{question}\n\n检索片段：\n{joined}'},
            ],
        )
        content = completion.choices[0].message.content if completion.choices else ''
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get('text') or ''))
                else:
                    parts.append(str(getattr(part, 'text', part) or ''))
            content = ''.join(parts)
        return (content or '').strip()

    def run_item(self, project_id, item, *, generate=True):
        question = item['question'].strip()
        contexts, found = self.retrieve(project_id, question, item)
        response = self.generate(question, contexts) if generate else ''
        record = {
            'id': item.get('id') or '',
            'user_input': question,
            'retrieved_contexts': contexts,
            'response': response,
            'reference': str(item.get('reference') or '').strip(),
            'search_status': found.get('status') if isinstance(found, dict) else None,
        }
        return record, found


def _openai_client(config):
    from openai import OpenAI

    kwargs = {'api_key': config['api_key']}
    if config.get('base_url'):
        kwargs['base_url'] = config['base_url']
    return OpenAI(**kwargs)


class QueryApp:
    """TruLens 包装前的检索、生成和整段查询。每条的 filters 会进入 retrieve。"""

    def __init__(self, pipeline, project_id):
        self.pipeline = pipeline
        self.project_id = str(project_id)
        self.last_contexts = []
        self.last_found = None

    def retrieve(self, query: str, filters: dict | None = None) -> list:
        contexts, found = self.pipeline.retrieve(self.project_id, query, filters or {})
        self.last_contexts = list(contexts)
        self.last_found = found
        return contexts

    def generate_completion(self, query: str, context_list: list) -> str:
        return self.pipeline.generate(query, context_list)

    def query(self, query: str, filters: dict | None = None) -> str:
        context_list = self.retrieve(query=query, filters=filters)
        return self.generate_completion(query=query, context_list=context_list)


def trace_for(record, found, item):
    coverage = found.get('coverage') if isinstance(found, dict) else None
    return {
        'id': record['id'],
        'question': record['user_input'],
        'search_status': record.get('search_status'),
        'context_count': len(record['retrieved_contexts']),
        'coverage': coverage,
        'filters': filter_payload(item),
    }


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n',
        encoding='utf-8',
    )


def write_csv(path, rows):
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def collect_records(pipeline, project_id, items, *, generate):
    records = []
    traces = []
    for item in items:
        record, found = pipeline.run_item(project_id, item, generate=generate)
        records.append(record)
        traces.append(trace_for(record, found, item))
        print(
            f"{record['id']}: status={record['search_status']} "
            f"contexts={len(record['retrieved_contexts'])}"
        )
    return records, traces
