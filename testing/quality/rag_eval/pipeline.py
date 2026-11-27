"""尽调 RAG 评测链路：黄金集 → 检索 →（可选）生成 → 记录。

检索只调用现有 EvidenceService.search，不另建向量库。
生成只根据检索片段作答，供 RAGAS / TruLens 的答案类指标使用。
本模块不导入 ragas 或 trulens，默认测试环境可以单独加载。
"""
from __future__ import annotations

import copy
import inspect
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
DEEPSEEK_MODEL = 'deepseek-flash'
# 非思考模式的官方默认补全额度是 8K。显式写出，用来盖过 RAGAS 的 1024。
DEEPSEEK_MAX_TOKENS = 8192
# thinking.type 优先于 reasoning_effort。两者都关掉，避免网关只认其中一个。
DEEPSEEK_EXTRA_BODY = {
    'thinking': {'type': 'disabled'},
    'reasoning_effort': 'none',
}
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
# 产品界面和 MCP 仍是各路 12 条、融合后最多 12 条进重排、重排后保留 5 条。
# 评测默认加宽两路召回。融合上限取两路之和，否则多召回的片段会在进重排前被切回 12 条。
# 重排后再多留一些，避免金标只是在最后 5 条被截掉。这些数只传给这一次 search()。
EVAL_VECTOR_LIMIT = 20
EVAL_KEYWORD_LIMIT = 20
EVAL_CANDIDATE_LIMIT = 40
EVAL_RESULT_LIMIT = 15
EVAL_LIMIT_MAX = 50
_EVAL_LIMITS = (
    ('vector_limit', 'EVAL_RETRIEVAL_VECTOR_LIMIT', EVAL_VECTOR_LIMIT, '向量召回条数'),
    ('keyword_limit', 'EVAL_RETRIEVAL_KEYWORD_LIMIT', EVAL_KEYWORD_LIMIT, '关键词召回条数'),
    ('candidate_limit', 'EVAL_RETRIEVAL_CANDIDATE_LIMIT', EVAL_CANDIDATE_LIMIT, '送入重排的候选条数'),
    ('result_limit', 'EVAL_RETRIEVAL_RESULT_LIMIT', EVAL_RESULT_LIMIT, '重排后保留条数'),
)


def eval_retrieval_limits():
    """评测这次检索用的四个上限。只读 EVAL_RETRIEVAL_*，不读产品的 DDA_RETRIEVAL_*。"""
    return {
        key: _eval_limit(env_name, default, label)
        for key, env_name, default, label in _EVAL_LIMITS
    }


def resolve_eval_limits(overrides=None):
    """环境变量为底，调用方传入的字典覆盖同名键。未知键直接拒绝。"""
    limits = eval_retrieval_limits()
    if not overrides:
        return limits
    if not isinstance(overrides, dict):
        raise ValueError('检索上限必须是对象。')
    unknown = set(overrides) - set(limits)
    if unknown:
        raise ValueError('未知的检索上限：' + '、'.join(sorted(unknown)))
    limits.update(overrides)
    return {
        key: _checked_limit(limits[key], label, env_name)
        for key, env_name, _default, label in _EVAL_LIMITS
    }


def format_retrieval_limits(limits):
    return (
        f"检索窗口：向量 {limits['vector_limit']}，关键词 {limits['keyword_limit']}，"
        f"融合后送入重排 {limits['candidate_limit']}，重排后保留 {limits['result_limit']}。"
        '产品界面和 MCP 仍只保留重排后的 5 条。'
    )


def public_retrieval_limits(limits):
    """写入痕迹的四个整数。没有窗口时返回 None，回放记录不补这组字段。"""
    if not limits:
        return None
    return {key: limits[key] for key, _env_name, _default, _label in _EVAL_LIMITS}


def _eval_limit(name, default, label):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return _checked_limit(default, label, name)
    text = str(raw).strip()
    if text.startswith('+') or text.startswith('-') or not text.isdigit():
        raise ValueError(f'{name} 须为 1–{EVAL_LIMIT_MAX} 的整数。')
    return _checked_limit(int(text), label, name)


def _checked_limit(value, label, name):
    if type(value) is not int or not 1 <= value <= EVAL_LIMIT_MAX:
        raise ValueError(f'{label}（{name}）须为 1–{EVAL_LIMIT_MAX} 的整数。')
    return value


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
        model = DEEPSEEK_MODEL if base_url and 'deepseek' in base_url.lower() else 'gpt-4o-mini'
    return {'api_key': api_key, 'base_url': base_url, 'model': model}


def uses_deepseek(config=None):
    """接口地址或模型名表明这是 DeepSeek。deepseek-chat 已停用，默认名是 deepseek-flash。"""
    config = config or judge_llm_config()
    base_url = str(config.get('base_url') or '').lower()
    model = str(config.get('model') or '').lower()
    return 'deepseek' in base_url or model.startswith('deepseek')


def completion_kwargs(config=None):
    """DeepSeek 补全请求要附带的字段。其他接口返回空字典。

    deepseek-flash 默认思考。思考写进 reasoning_content，占满 max_tokens 后
    finish_reason=length，message.content 为空，RAGAS 读不到 JSON。
    官方关闭方式是 thinking.type=disabled（OpenAI SDK 用 extra_body 送出）。
    reasoning_effort=none 是同一开关的另一种写法，type 优先。
    不声明 tools，因此不会打开 deepseek_web_search 或 deepseek_read_url。
    """
    config = config or judge_llm_config()
    if not uses_deepseek(config):
        return {}
    return {
        'extra_body': copy.deepcopy(DEEPSEEK_EXTRA_BODY),
        'max_tokens': DEEPSEEK_MAX_TOKENS,
    }


def describe_judge_llm(config=None):
    """供日志使用的裁判模型摘要。不含密钥。"""
    config = config or judge_llm_config()
    base_url = config.get('base_url') or ''
    return {
        'model': config.get('model'),
        'base_url': base_url.split('?', 1)[0] or None,
        'api_key_set': bool(config.get('api_key')),
        'thinking_disabled': bool(completion_kwargs(config)),
    }


def format_judge_log(config=None, suffix=''):
    info = describe_judge_llm(config)
    thinking = '已关闭' if info['thinking_disabled'] else '未改动'
    text = (
        f"裁判模型：{info['model']}，接口：{info['base_url'] or '默认 OpenAI'}，"
        f"已配置密钥：{info['api_key_set']}，思考模式：{thinking}"
    )
    if suffix:
        text = f'{text}，{suffix}'
    return text


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


def apply_completion_kwargs(kwargs, options):
    """把思考开关并进一次 chat.completions 调用。调用方写过的 max_tokens 保留。"""
    if not options:
        return kwargs
    extra = {}
    incoming = kwargs.get('extra_body')
    if isinstance(incoming, dict):
        extra.update(incoming)
    forced = options.get('extra_body')
    if isinstance(forced, dict):
        extra.update(copy.deepcopy(forced))
    if extra:
        kwargs['extra_body'] = extra
    for key, value in options.items():
        if key == 'extra_body':
            continue
        kwargs.setdefault(key, value)
    return kwargs


def _apply_responses_kwargs(kwargs):
    """Responses API 用 reasoning.effort=none 关闭思考，不把 chat 的 max_tokens 带过去。"""
    reasoning = dict(kwargs.get('reasoning') or {})
    reasoning['effort'] = 'none'
    kwargs['reasoning'] = reasoning
    return kwargs


def _is_coroutine_callable(func):
    """沿着 __wrapped__ 判断是不是异步调用。

    OpenAI 的 AsyncCompletions.create 本身不是协程函数，真正的协程在 __wrapped__ 上，
    但调用它仍会返回协程。只看外层会把它包成同步函数，instructor 随后读取 choices 失败。
    """
    seen = set()
    current = func
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if inspect.iscoroutinefunction(current):
            return True
        wrapped = getattr(current, '__wrapped__', None)
        current = wrapped if wrapped is not None else getattr(current, '__func__', None)
    return False


def _close_awaitable(result):
    close = getattr(result, 'close', None)
    if close is not None:
        close()


def _bind_method(owner, name, transform):
    if owner is None or not hasattr(owner, name):
        return
    current = getattr(owner, name)
    if getattr(current, '_dda_eval_bound', False):
        return
    if _is_coroutine_callable(current):
        # 必须仍是协程函数。同步包装会把 AsyncCompletions.create 的协程原样返回，
        # instructor 再去读 choices，Faithfulness 就报 'coroutine' object has no attribute 'choices'。
        async def wrapped(*args, **kwargs):
            transform(kwargs)
            result = current(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
    else:
        def wrapped(*args, **kwargs):
            transform(kwargs)
            result = current(*args, **kwargs)
            if inspect.isawaitable(result):
                _close_awaitable(result)
                raise RuntimeError(
                    '同步客户端的补全调用返回了未等待的协程，无法读取 choices。'
                    '生成和 TruLens 使用同步客户端；RAGAS 的异步客户端必须保持 create 为协程函数。'
                )
            return result

    wrapped._dda_eval_bound = True
    try:
        setattr(owner, name, wrapped)
    except (AttributeError, TypeError):
        return


def bind_completion_options(client, options):
    """在客户端上固定 DeepSeek 的思考开关。

    RAGAS 的 instructor 和 TruLens 自己发请求，调用方不一定能把 extra_body 传进去。
    绑在 chat.completions.create 上之后，后一层丢掉该参数也会重新补上。
    原来的 create 若是协程函数，绑定后必须仍是协程函数并在内部 await，
    否则 Faithfulness.score() 里的 agenerate 会拿到未等待的协程。
    TruLens 会先试 Responses API，那边用 reasoning.effort=none。
    不改写 reasoning_effort 这个顶层参数：TruLens 发现不支持时会删掉它再重试，
    包一层又加回去会让重试失败。
    """
    if not options or client is None:
        return client
    chat = getattr(client, 'chat', None)
    completions = getattr(chat, 'completions', None) if chat is not None else None
    _bind_method(
        completions,
        'create',
        lambda kwargs: apply_completion_kwargs(kwargs, options),
    )
    responses = getattr(client, 'responses', None)
    _bind_method(responses, 'create', _apply_responses_kwargs)
    _bind_method(responses, 'parse', _apply_responses_kwargs)
    return client


def openai_client(config, *, async_client=False):
    if async_client:
        from openai import AsyncOpenAI as Client
    else:
        from openai import OpenAI as Client

    kwargs = {'api_key': config['api_key']}
    if config.get('base_url'):
        kwargs['base_url'] = config['base_url']
    return bind_completion_options(Client(**kwargs), completion_kwargs(config))


def completion_text(completion):
    """取出正文。思考把额度用完、正文为空时直接失败，避免把空答案送去打分。"""
    if inspect.isawaitable(completion):
        _close_awaitable(completion)
        raise RuntimeError(
            '生成结果是未等待的协程，无法读取 choices。'
            '评测生成使用同步客户端。'
        )
    choice = completion.choices[0] if getattr(completion, 'choices', None) else None
    message = getattr(choice, 'message', None) if choice is not None else None
    content = getattr(message, 'content', '') if message is not None else ''
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get('text') or ''))
            else:
                parts.append(str(getattr(part, 'text', part) or ''))
        content = ''.join(parts)
    text = content.strip() if isinstance(content, str) else str(content or '').strip()
    if text:
        return text
    finish = getattr(choice, 'finish_reason', None) if choice is not None else None
    reasoning = getattr(message, 'reasoning_content', None) if message is not None else None
    if finish == 'length' or reasoning:
        raise RuntimeError(
            '模型没有返回答案正文'
            f'（finish_reason={finish}）。'
            'DeepSeek 默认把补全额度写进 reasoning_content，正文因此为空。'
            '评测请求已关闭思考（thinking.type=disabled）。'
            '请确认 EVAL_LLM_BASE_URL 指向的接口接受该字段。'
        )
    return ''


class RetrievalPipeline:
    """同一进程内复用 EvidenceService，避免重复加载本地嵌入和重排模型。"""

    def __init__(self, service=None, *, limits=None):
        # 先读 .env，再校验窗口，最后才加载模型。错误的 EVAL_RETRIEVAL_* 不必等到第一次检索。
        # load_dotenv 不覆盖进程里已经设置的变量，因此会话里的覆盖优先于仓库 .env。
        if service is None:
            load_eval_env()
        self.limits = resolve_eval_limits(limits)
        self._announced_limits = False
        if service is None:
            from dd_agent.config import Settings
            from dd_agent.retrieval import EvidenceService

            settings = Settings.from_env()
            if not settings.database_url:
                raise RuntimeError('DDA_DATABASE_URL 为空，请先在仓库 .env 中写好数据库连接。')
            service = EvidenceService(settings)
        self.service = service

    def retrieve(self, project_id, question, item=None):
        item = item or {}
        if not self._announced_limits:
            print(format_retrieval_limits(self.limits))
            self._announced_limits = True
        # document_id 已进入 scope.document_ids。再传关键字会和 scope 冲突。
        # 四个上限只属于这一次评测调用，不改产品设置，界面和 MCP 仍保留 5 条。
        found = self.service.search(
            project_id,
            question,
            document_id=None,
            scope=build_scope(item),
            vector_limit=self.limits['vector_limit'],
            keyword_limit=self.limits['keyword_limit'],
            candidate_limit=self.limits['candidate_limit'],
            result_limit=self.limits['result_limit'],
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
        client = self._chat if self._chat is not None else openai_client(config)
        joined = '\n\n---\n\n'.join(contexts)
        completion = client.chat.completions.create(
            model=config['model'],
            temperature=0,
            messages=[
                {'role': 'system', 'content': GENERATION_SYSTEM},
                {'role': 'user', 'content': f'问题：{question}\n\n检索片段：\n{joined}'},
            ],
            **completion_kwargs(config),
        )
        return completion_text(completion)

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


def trace_for(record, found, item, limits=None):
    coverage = found.get('coverage') if isinstance(found, dict) else None
    trace = {
        'id': record['id'],
        'question': record['user_input'],
        'search_status': record.get('search_status'),
        'context_count': len(record['retrieved_contexts']),
        'coverage': coverage,
        'filters': filter_payload(item),
    }
    window = public_retrieval_limits(limits)
    if window is not None:
        trace['retrieval_limits'] = window
    rewrite = found.get('query_rewrite') if isinstance(found, dict) else None
    if isinstance(rewrite, dict) and isinstance(rewrite.get('original'), str) and isinstance(rewrite.get('rewritten'), str):
        # 只留下原句和轻改写。不写关键词补充表，也不写片段。
        trace['query_rewrite'] = {
            'original': rewrite['original'],
            'rewritten': rewrite['rewritten'],
            'enabled': bool(rewrite.get('enabled')),
        }
    return trace


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n',
        encoding='utf-8',
    )


def load_score_records(path):
    """读取已经生成过的 records.json。不检索，也不重新生成答案。"""
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f'找不到记录文件：{file}')
    try:
        payload = json.loads(file.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'记录文件不是合法 JSON：{exc}') from exc
    if isinstance(payload, dict) and isinstance(payload.get('records'), list):
        payload = payload['records']
    if not isinstance(payload, list) or not payload:
        raise ValueError('记录文件必须是非空 JSON 数组，或含非空 records 数组的对象。')
    return [normalize_score_record(item, index) for index, item in enumerate(payload)]


def normalize_score_record(item, index):
    label = f'第 {index + 1} 条'
    if not isinstance(item, dict):
        raise ValueError(f'{label}必须是对象。')
    question = item.get('user_input', item.get('question'))
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f'{label}缺少 user_input。')
    contexts = item.get('retrieved_contexts', item.get('contexts'))
    if not isinstance(contexts, list) or not all(isinstance(text, str) for text in contexts):
        raise ValueError(f'{label}的 retrieved_contexts 必须是字符串数组。')
    if 'response' in item:
        response = item.get('response')
    elif 'answer' in item:
        response = item.get('answer')
    else:
        raise ValueError(f'{label}缺少 response，不能在没有答案的记录上打分。')
    if not isinstance(response, str) or not response.strip():
        raise ValueError(
            f'{label}的 response 为空。请使用已经生成答案的 records.json，'
            '而不是 --retrieve-only 的记录。'
        )
    reference = item.get('reference', '')
    if reference is None:
        reference = ''
    if not isinstance(reference, str):
        raise ValueError(f'{label}的 reference 须为字符串。')
    item_id = item.get('id')
    if item_id is None or str(item_id).strip() == '':
        item_id = f'item-{index + 1}'
    record = {
        'id': str(item_id).strip(),
        'user_input': question.strip(),
        'retrieved_contexts': list(contexts),
        'response': response.strip(),
        'reference': reference.strip(),
    }
    if item.get('search_status') is not None:
        record['search_status'] = item.get('search_status')
    return record


class RecordReplay:
    """按已保存的记录回放片段和答案。不调用 EvidenceService，也不再生成。"""

    def __init__(self, records):
        self.by_question = {}
        for record in records:
            question = record['user_input']
            if question in self.by_question:
                raise ValueError(
                    f'记录里有重复问题，TruLens 无法按问题回放：{record.get("id") or question}'
                )
            self.by_question[question] = record

    def retrieve(self, project_id, question, item=None):
        record = self._record(question)
        found = {
            'status': record.get('search_status'),
            'coverage': None,
        }
        return list(record['retrieved_contexts']), found

    def generate(self, question, contexts):
        return self._record(question)['response']

    def _record(self, question):
        key = str(question or '').strip()
        try:
            return self.by_question[key]
        except KeyError as exc:
            raise ValueError(f'记录里没有这个问题：{key}') from exc


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
        traces.append(trace_for(record, found, item, getattr(pipeline, 'limits', None)))
        print(
            f"{record['id']}: status={record['search_status']} "
            f"contexts={len(record['retrieved_contexts'])}"
        )
    return records, traces
