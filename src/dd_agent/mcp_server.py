"""只读 MCP HTTP/SSE 适配。工具调用现有项目列表、材料列表和混合检索，不重写排序。

传输是 MCP 2024-11-05 的 HTTP + SSE：GET /sse 接收服务端消息，POST /messages 发送 JSON-RPC。
每个请求都要带本机 API 密钥。这里不提供写入、入库、OCR，也不返回 PDF 或页面图。
"""
from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import sys
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import anyio
import psycopg
from fastapi.encoders import jsonable_encoder
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .app import public_document
from .config import Settings
from .coverage_gaps import CoverageGaps
from .db import Database
from .db_connections import ConnectionProvider, DatabaseBusy
from .evidence_read import EvidenceReader
from .material_metadata import MetadataError, parse_filters
from .page_queries import PageQueryError
from .retrieval import EvidenceService, RetrievalError


PROTOCOL_VERSIONS = frozenset({'2024-11-05', '2025-03-26', '2025-06-18'})
DEFAULT_PROTOCOL = '2024-11-05'
SERVER_INFO = {'name': 'dd-agent-evidence', 'version': '0.1.0'}

TOOLS = (
    {
        'name': 'list_projects',
        'description': '列出本机尽调项目的标识和名称。只读，不含材料正文。',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'list_documents',
        'description': (
            '列出指定项目里的材料标识、文件名、登记状态、阅读进度，以及类别、所属主体和日期或期间。'
            '可按类别、主体、日期重叠筛选。不返回原件、页面图或存储路径。'
            '只传 project_id 时仍列出该项目的全部材料。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project_id': {'type': 'string', 'description': '项目 UUID'},
                'categories': {'type': 'array', 'items': {'type': 'string'}, 'description': '可选。类别代码，字段内为 OR'},
                'subject_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '可选。主体 UUID，字段内为 OR'},
                'date_point': {'type': 'string', 'description': '可选。日历日 YYYY-MM-DD，按重叠匹配'},
                'period_start': {'type': 'string', 'description': '可选。期间起日，须与 period_end 同时给出'},
                'period_end': {'type': 'string', 'description': '可选。期间止日'},
            },
            'required': ['project_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'search_evidence',
        'description': (
            '在指定项目中检索证据片段。复用现有混合召回：向量、关键词、RRF 融合，再重排。'
            '结果是候选证据，不是已核实事实，也不返回 PDF 原件。过滤后无命中不等于材料不存在。'
            '可选 document_id 把召回限制在一份材料内。也可传 document_ids、PDF 页范围、类别、主体和日期。'
            '字段之间为 AND，同一字段的多个值为 OR。未传的维度不限制；空数组无效。'
            '不能同时传 document_id 与 document_ids。设了页范围时，没有物理页码的材料不会进入结果。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project_id': {'type': 'string', 'description': '项目 UUID'},
                'query': {'type': 'string', 'description': '检索问题，1–500 字'},
                'document_id': {'type': 'string', 'description': '可选。只在这一份材料内召回。不要和 document_ids 同时传'},
                'document_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '可选。材料 UUID 列表，字段内为 OR'},
                'page_from': {'type': 'integer', 'minimum': 1, 'description': '可选。PDF 物理起页，含端点，须与 page_to 同时给出'},
                'page_to': {'type': 'integer', 'minimum': 1, 'description': '可选。PDF 物理止页，含端点'},
                'categories': {'type': 'array', 'items': {'type': 'string'}, 'description': '可选。类别代码，字段内为 OR'},
                'subject_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '可选。主体 UUID，字段内为 OR'},
                'date_point': {'type': 'string', 'description': '可选。日历日 YYYY-MM-DD，按重叠匹配'},
                'period_start': {'type': 'string', 'description': '可选。期间起日，须与 period_end 同时给出'},
                'period_end': {'type': 'string', 'description': '可选。期间止日'},
            },
            'required': ['project_id', 'query'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'coverage_gaps',
        'description': (
            '列出指定项目的覆盖缺口：未处理、失败、未知、空文本、待重建。'
            '必须提供 project_id，只返回该项目的计数和清单，不检索，也不启动 OCR。'
            '各项计数口径不同，不能相加。空项目的 complete 为 false。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project_id': {'type': 'string', 'description': '项目 UUID'},
                'kind': {'type': 'string', 'description': '可选。unprocessed、failed、unknown、empty_text、pending_rebuild'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'description': '可选。每批条数，默认 50'},
                'cursor': {'type': 'string', 'description': '可选。上一批的 next_cursor'},
            },
            'required': ['project_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'read_evidence',
        'description': (
            '按证据标识，或按材料加来源范围，补读指定项目里的原文。必须提供 project_id。'
            '有长度上限和续读位置。过期引用不会换成新正文。'
            '其他项目的证据标识会拒绝，且不说明该标识是否存在。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project_id': {'type': 'string', 'description': '项目 UUID'},
                'evidence_id': {'type': 'string', 'description': '证据 UUID。不要和 document_id 同时传'},
                'document_id': {'type': 'string', 'description': '材料 UUID。与页范围或来源单元续读一起使用'},
                'page_from': {'type': 'integer', 'minimum': 1, 'description': 'PDF 起页，须与 page_to 同时给出'},
                'page_to': {'type': 'integer', 'minimum': 1, 'description': 'PDF 止页，含端点'},
                'resume_page': {'type': 'integer', 'minimum': 1, 'description': '可选。从该页继续'},
                'offset': {'type': 'integer', 'minimum': 0, 'description': '可选。当前页或当前单元内的字符偏移'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8000, 'description': '可选。本次最多返回的字符数，默认 4000'},
                'source_revision': {'type': 'string', 'description': '可选。绑定起始页的来源版本，不一致则拒绝且不返回新正文'},
                'resume_unit': {'type': 'string', 'description': '可选。没有物理页码时，从该来源单元继续（含本单元）'},
                'index_version': {'type': 'string', 'description': '可选。证据上的索引版本，不一致则拒绝'},
            },
            'required': ['project_id'],
            'additionalProperties': False,
        },
    },
)
TOOL_NAMES = frozenset(tool['name'] for tool in TOOLS)


class ToolFailure(Exception):
    """给调用方看的工具错误。文字里不能带数据库内部细节。"""


def create_mcp_app(settings=None):
    settings = settings or Settings.from_env()
    provider = ConnectionProvider(settings.database_url, max_size=settings.db_pool_size,
                                  timeout=settings.db_pool_timeout, max_waiting=settings.db_pool_max_waiting)
    database = Database(settings.database_url, provider=provider)

    @asynccontextmanager
    async def lifespan(app):
        provider.start()
        try:
            yield
        finally:
            for session in list(app.state.sessions.values()):
                session['queue'].put_nowait(None)
            app.state.sessions.clear()
            from .model_runtime import close_runtime
            close_runtime(settings)
            provider.close()

    app = Starlette(routes=[
        Route('/sse', sse_endpoint, methods=['GET']),
        Route('/messages', messages_endpoint, methods=['POST']),
    ], lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.evidence = EvidenceService(settings, database=database)
    app.state.sessions = {}
    return app


def _presented_key(headers):
    # Authorization 存在时不再回落到另一个头，避免错误的 Bearer 被旁路。
    authorization = headers.get('authorization')
    if authorization is not None:
        scheme, _, rest = authorization.partition(' ')
        if scheme.lower() != 'bearer':
            return ''
        return rest.strip()
    return (headers.get('x-api-key') or '').strip()


def _key_ok(expected, presented):
    if not expected or not presented:
        return False
    try:
        return hmac.compare_digest(presented.encode('utf-8'), expected.encode('utf-8'))
    except (UnicodeError, TypeError):
        return False


def _reject(request):
    origin = request.headers.get('origin')
    if origin is not None:
        parsed = urlsplit(origin)
        host = request.headers.get('host', '')
        if parsed.scheme not in ('http', 'https') or parsed.netloc != host:
            return JSONResponse({'detail': '跨站来源被拒绝。'}, status_code=403)
    if not _key_ok(request.app.state.settings.mcp_api_key, _presented_key(request.headers)):
        return JSONResponse({'detail': '需要有效的 MCP API 密钥。'}, status_code=401, headers={
            'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})
    return None


def _sse(event, data):
    body = ''.join(f'data: {line}\n' for line in str(data).split('\n'))
    return f'event: {event}\n{body}\n'


async def sse_endpoint(request):
    denied = _reject(request)
    if denied is not None:
        return denied
    session_id = uuid4().hex
    session = {'queue': asyncio.Queue(), 'lock': asyncio.Lock(), 'initialized': False}
    request.app.state.sessions[session_id] = session

    async def events():
        try:
            yield _sse('endpoint', f'/messages?session_id={session_id}')
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(session['queue'].get(), 15)
                except TimeoutError:
                    yield ': keepalive\n\n'
                    continue
                if item is None:
                    break
                yield _sse('message', json.dumps(item, ensure_ascii=False, separators=(',', ':')))
        finally:
            request.app.state.sessions.pop(session_id, None)

    return StreamingResponse(events(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


async def messages_endpoint(request):
    denied = _reject(request)
    if denied is not None:
        return denied
    session = request.app.state.sessions.get(request.query_params.get('session_id', ''))
    if session is None:
        return JSONResponse({'detail': 'MCP 会话不存在或已结束。'}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'detail': '请求不是有效的 JSON。'}, status_code=400)
    if isinstance(payload, list):
        return JSONResponse({'detail': '不支持批量请求。'}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({'detail': '请求不是 JSON-RPC 对象。'}, status_code=400)
    async with session['lock']:
        response = await anyio.to_thread.run_sync(lambda: dispatch(request.app, session, payload))
    if response is not None:
        await session['queue'].put(response)
    return Response(status_code=202)


def dispatch(app, session, payload):
    has_id = isinstance(payload, dict) and 'id' in payload
    message_id = payload.get('id') if has_id else None

    def fail(code, message):
        if not has_id:
            return None
        return {'jsonrpc': '2.0', 'id': message_id, 'error': {'code': code, 'message': message}}

    if not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0' or not isinstance(payload.get('method'), str):
        return fail(-32600, '请求不是有效的 JSON-RPC。')
    method = payload['method']
    params = payload.get('params') if payload.get('params') is not None else {}
    if not isinstance(params, dict):
        return fail(-32602, '参数须为对象。')
    try:
        if method == 'initialize':
            result = _initialize(session, params)
        elif method == 'ping':
            result = {}
        elif method in ('notifications/initialized', 'notifications/cancelled'):
            return None
        elif method in ('tools/list', 'tools/call'):
            if not session.get('initialized'):
                return fail(-32002, '请先完成 initialize。')
            result = {'tools': list(TOOLS)} if method == 'tools/list' else _call_tool(app, params)
        else:
            return fail(-32601, '不支持的方法。')
    except ToolFailure as exc:
        if method == 'tools/call' and has_id:
            return {'jsonrpc': '2.0', 'id': message_id, 'result': _tool_error(str(exc))}
        return fail(-32602, str(exc))
    if not has_id:
        return None
    return {'jsonrpc': '2.0', 'id': message_id, 'result': result}


def _initialize(session, params):
    requested = params.get('protocolVersion')
    session['initialized'] = True
    session['protocol'] = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
    return {
        'protocolVersion': session['protocol'],
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': dict(SERVER_INFO),
        'instructions': (
            '只读尽调证据服务。先 list_projects，再 list_documents。'
            'coverage_gaps 查看当前项目的缺口，search_evidence 取候选片段，read_evidence 按证据标识或来源范围补读。'
            '每次调用都要带 project_id。结果是候选证据，不是已核实事实。过滤后无命中不等于材料不存在。'
        ),
    }


def _call_tool(app, params):
    name = params.get('name')
    if name not in TOOL_NAMES:
        raise ToolFailure('未知工具。')
    try:
        if name == 'list_projects':
            _arguments(params, (), ())
            payload = _list_projects(app.state.database)
        elif name == 'list_documents':
            arguments = _arguments(params, (
                'project_id', 'categories', 'subject_ids', 'date_point', 'period_start', 'period_end'), ('project_id',))
            payload = _list_documents(app.state.database, _uuid(arguments['project_id'], '项目标识'), arguments)
        elif name == 'coverage_gaps':
            arguments = _arguments(params, ('project_id', 'kind', 'limit', 'cursor'), ('project_id',))
            payload = _coverage(app, _uuid(arguments['project_id'], '项目标识'), arguments)
        elif name == 'read_evidence':
            arguments = _arguments(params, (
                'project_id', 'evidence_id', 'document_id', 'page_from', 'page_to', 'resume_page',
                'offset', 'limit', 'source_revision', 'resume_unit', 'index_version'), ('project_id',))
            payload = _read(app, _uuid(arguments['project_id'], '项目标识'), arguments)
        else:
            arguments = _arguments(params, (
                'project_id', 'query', 'document_id', 'document_ids', 'page_from', 'page_to',
                'categories', 'subject_ids', 'date_point', 'period_start', 'period_end'), ('project_id', 'query'))
            payload = _search(app.state.evidence, _uuid(arguments['project_id'], '项目标识'), arguments['query'], arguments)
    except ToolFailure:
        raise
    except (RetrievalError, LookupError, ValueError, PageQueryError) as exc:
        raise ToolFailure(str(exc)) from None
    except DatabaseBusy:
        raise ToolFailure('数据库连接繁忙或暂不可用，请稍后重试。') from None
    except psycopg.Error:
        raise ToolFailure('数据库暂不可用或尚未初始化。') from None
    except Exception as exc:
        print(f'MCP 工具调用失败：{type(exc).__name__}', file=sys.stderr)
        raise ToolFailure('请求未能完成，请稍后重试。') from None
    return _tool_ok(payload)


def _arguments(params, allowed, required):
    raw = params.get('arguments') if params.get('arguments') is not None else {}
    if not isinstance(raw, dict):
        raise ToolFailure('工具参数须为对象。')
    if set(raw) - set(allowed):
        raise ToolFailure('参数无法识别。')
    missing = [name for name in required if raw.get(name) is None or (isinstance(raw.get(name), str) and not raw[name].strip())]
    if missing:
        raise ToolFailure('缺少 ' + '、'.join(missing) + '。')
    return raw


def _uuid(value, label):
    try:
        return UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        raise ToolFailure(f'{label}无效。') from None


def _list_projects(database):
    projects = [{'id': row['id'], 'name': row['name'], 'created_at': row['created_at']} for row in database.projects()]
    return {'projects': projects}


def _list_documents(database, project_id, arguments):
    if not database.project_exists(project_id):
        raise ToolFailure('项目不存在。')
    try:
        scope = parse_filters(
            purpose='list', categories=arguments.get('categories'), subject_ids=arguments.get('subject_ids'),
            date_point=arguments.get('date_point'), period_start=arguments.get('period_start'),
            period_end=arguments.get('period_end'))
    except MetadataError as exc:
        raise ToolFailure(f'请求无效（{exc.status}）：{exc}') from None
    documents = []
    for row in database.documents(project_id, scope=scope):
        # 与网页材料列表同一投影：去掉存储键，附上阅读进度和业务元数据，不读原件。
        documents.append({**public_document(row), 'reading': database.reading(row['id'])})
    return {'documents': documents}


def _optional_int(arguments, name):
    value = arguments.get(name)
    if value is None:
        return None
    if type(value) is not int:
        raise ToolFailure(f'{name}须为整数。')
    return value


def _coverage(app, project_id, arguments):
    if not app.state.database.project_exists(project_id):
        raise ToolFailure('项目不存在。')
    return CoverageGaps(app.state.database, app.state.evidence).page(
        project_id, kind=arguments.get('kind'), limit=_optional_int(arguments, 'limit'),
        cursor=arguments.get('cursor'))


def _read(app, project_id, arguments):
    evidence_id = arguments.get('evidence_id')
    document_id = arguments.get('document_id')
    if evidence_id and document_id:
        raise ToolFailure('不能同时给出证据标识和材料范围。')
    if not evidence_id and not document_id:
        raise ToolFailure('需要证据标识，或材料标识与来源范围。')
    reader = EvidenceReader(app.state.database, app.state.settings)
    if evidence_id:
        return reader.by_id(
            project_id, _uuid(evidence_id, '证据标识'), offset=_optional_int(arguments, 'offset') or 0,
            limit=_optional_int(arguments, 'limit'), index_version=arguments.get('index_version'))
    resume_unit = arguments.get('resume_unit')
    return reader.by_range(
        project_id, _uuid(document_id, '材料标识'),
        page_from=_optional_int(arguments, 'page_from'), page_to=_optional_int(arguments, 'page_to'),
        resume_page=_optional_int(arguments, 'resume_page'), offset=_optional_int(arguments, 'offset') or 0,
        limit=_optional_int(arguments, 'limit'), source_revision=arguments.get('source_revision'),
        resume_unit=_uuid(resume_unit, '来源单元') if resume_unit else None)


def _search(evidence, project_id, query, arguments):
    if not isinstance(query, str):
        raise ToolFailure('查询须为文本。')
    try:
        scope = parse_filters(
            document_id=arguments.get('document_id'), document_ids=arguments.get('document_ids'),
            page_from=arguments.get('page_from'), page_to=arguments.get('page_to'),
            categories=arguments.get('categories'), subject_ids=arguments.get('subject_ids'),
            date_point=arguments.get('date_point'), period_start=arguments.get('period_start'),
            period_end=arguments.get('period_end'))
    except MetadataError as exc:
        raise ToolFailure(f'请求无效（{exc.status}）：{exc}') from None
    found = evidence.search(project_id, query, scope=scope)
    document_id = arguments.get('document_id')
    chunks = []
    for hit in found.get('results') or []:
        if hit.get('matched_children'):
            for child in hit['matched_children']:
                chunks.append(_chunk(project_id, hit, child))
        else:
            chunks.append(_chunk(project_id, hit, hit))
    payload = {
        'project_id': project_id,
        'document_id': document_id,
        'query': query.strip(),
        'status': found.get('status') or ('candidates' if chunks else 'no_matches'),
        'results': chunks,
    }
    if 'coverage' in found:
        payload['coverage'] = found['coverage']
    for key in ('excluded_uncategorized', 'excluded_unsubjected', 'excluded_undated', 'message'):
        if key in found:
            payload[key] = found[key]
    if arguments.get('document_ids') is not None:
        payload['document_ids'] = arguments.get('document_ids')
    return payload


def _chunk(project_id, hit, source):
    spans = source.get('source_spans')
    if spans is None:
        spans = hit.get('source_spans') or []
    page = source.get('page_number', hit.get('page_number'))
    if page is None and spans and isinstance(spans[0], dict):
        page = spans[0].get('page_number')
    item = {
        'chunk_id': source.get('id'),
        'project_id': project_id,
        'document_id': source.get('document_id') or hit.get('document_id'),
        'page_number': page,
        'start_offset': source.get('start_offset'),
        'end_offset': source.get('end_offset'),
        'source_spans': spans or [],
        'text': source.get('text'),
        'filenames': list(hit.get('filenames') or source.get('filenames') or []),
        'index_kind': hit.get('index_kind') or source.get('index_kind') or 'page',
    }
    recall = source.get('recall') if source.get('recall') is not None else hit.get('recall')
    if recall is not None:
        item['recall'] = recall
    for key in ('similarity', 'rerank_score', 'method', 'verification', 'source_url'):
        value = source.get(key) if source.get(key) is not None else hit.get(key)
        if value is not None:
            item[key] = value
    for key in ('parent_id', 'build_id'):
        if hit.get(key) is not None:
            item[key] = hit[key]
    return item


def _tool_ok(payload):
    text = json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(',', ':'))
    return {'content': [{'type': 'text', 'text': text}]}


def _tool_error(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}
