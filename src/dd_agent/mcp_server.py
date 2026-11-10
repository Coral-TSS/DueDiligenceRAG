"""只读 MCP 适配。工具调用现有项目列表、材料列表和混合检索，不重写排序。

同一进程提供两种传输。`/mcp` 是 Streamable HTTP：POST 返回单个 JSON，不开启 SSE 流。
`/sse` 与 `/messages` 仍是 MCP 2024-11-05 的 HTTP + SSE，给还在用旧协议的客户端。
每个请求都要带本机 API 密钥。这里不提供写入、入库、OCR，也不返回 PDF 或页面图。
"""
from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import secrets
import sys
import time
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
from .material_metadata import MetadataError, material_view, parse_filters
from .page_queries import PageQueryError
from .retrieval import EvidenceService, RetrievalError


PROTOCOL_VERSIONS = frozenset({'2024-11-05', '2025-03-26', '2025-06-18'})
DEFAULT_PROTOCOL = '2024-11-05'
# Streamable HTTP 只协商 2025-03-26 与 2025-06-18。客户端若请求更新的版本（例如官方 SDK 的
# 2025-11-25），按规范回一个本端支持的版本，这里固定回 2025-06-18。2025-11-25 的传输规则与
# 2025-06-18 相同，但多了任务、图标等本服务不实现的能力，因此不把它算作已支持版本。
STREAMABLE_PROTOCOL_VERSIONS = frozenset({'2025-03-26', '2025-06-18'})
STREAMABLE_DEFAULT_PROTOCOL = '2025-06-18'
# 2025-06-18 起，initialize 之后若缺少 MCP-Protocol-Version，按 2025-03-26 接受。
STREAMABLE_MISSING_HEADER_PROTOCOL = '2025-03-26'
# 会话只在本进程内存中。空闲超时或超过数量上限后失效，重启即全部失效。
HTTP_SESSION_IDLE_SECONDS = 30 * 60
HTTP_SESSION_LIMIT = 128
SERVER_INFO = {'name': 'dd-agent-evidence', 'version': '0.1.0'}
_STREAMABLE_ACCEPT = frozenset({'application/json', 'text/event-stream'})

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
            '每条命中都带材料的类别、主体和日期；没有值的字段为 null。Office 命中另有定位。'
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
            '响应附带该材料的类别、主体和日期；没有值的字段为 null。'
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
    {
        'name': 'list_subjects',
        'description': (
            '列出指定项目的全部主体：标识、名称、角色和创建时间。'
            '没有绑定任何材料的主体也会返回。只读，不含材料正文。'
            '必须提供 project_id，只返回该项目的主体。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project_id': {'type': 'string', 'description': '项目 UUID'},
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
            app.state.http_sessions.clear()
            from .model_runtime import close_runtime
            close_runtime(settings)
            provider.close()

    app = Starlette(routes=[
        Route('/mcp', mcp_endpoint, methods=['GET', 'POST', 'DELETE']),
        Route('/sse', sse_endpoint, methods=['GET']),
        Route('/messages', messages_endpoint, methods=['POST']),
    ], lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.evidence = EvidenceService(settings, database=database)
    app.state.sessions = {}
    app.state.http_sessions = {}
    app.state.http_session_lock = asyncio.Lock()
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


def _media_types(header):
    found = set()
    for part in (header or '').split(','):
        media = part.split(';', 1)[0].strip().lower()
        if media:
            found.add(media)
    return found


def _streamable_version_error(request):
    """initialize 之后才检查 MCP-Protocol-Version。缺头按 2025-03-26 接受。"""
    if 'mcp-protocol-version' not in request.headers:
        version = STREAMABLE_MISSING_HEADER_PROTOCOL
    else:
        version = request.headers.get('mcp-protocol-version', '').strip()
    if version in STREAMABLE_PROTOCOL_VERSIONS:
        return None
    return JSONResponse({'detail': '不支持的 MCP-Protocol-Version。'}, status_code=400)


def _session_header(request):
    if 'mcp-session-id' not in request.headers:
        return None
    value = request.headers.get('mcp-session-id', '').strip()
    return value or None


def _expire_http_sessions(app):
    now = time.monotonic()
    stale = [sid for sid, session in app.state.http_sessions.items()
             if now - session['touched'] > HTTP_SESSION_IDLE_SECONDS]
    for sid in stale:
        app.state.http_sessions.pop(sid, None)


async def _open_http_session(app):
    async with app.state.http_session_lock:
        _expire_http_sessions(app)
        if len(app.state.http_sessions) >= HTTP_SESSION_LIMIT:
            return None, JSONResponse({'detail': 'MCP 会话数量已达上限。'}, status_code=503)
        session = {
            'id': secrets.token_hex(16),
            'lock': asyncio.Lock(),
            'initialized': False,
            'touched': time.monotonic(),
            'protocol_versions': STREAMABLE_PROTOCOL_VERSIONS,
            'protocol_default': STREAMABLE_DEFAULT_PROTOCOL,
        }
        app.state.http_sessions[session['id']] = session
        return session, None


async def _drop_http_session(app, session_id):
    async with app.state.http_session_lock:
        app.state.http_sessions.pop(session_id, None)


async def _lookup_http_session(app, session_id):
    if not session_id:
        return None, JSONResponse({'detail': '缺少 MCP 会话标识。'}, status_code=400)
    async with app.state.http_session_lock:
        _expire_http_sessions(app)
        session = app.state.http_sessions.get(session_id)
        if session is None:
            return None, JSONResponse({'detail': 'MCP 会话不存在或已结束。'}, status_code=404)
        session['touched'] = time.monotonic()
        return session, None


def _message_kind(payload):
    if not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0':
        return 'invalid'
    method = payload.get('method')
    has_method = isinstance(method, str) and bool(method)
    has_id = 'id' in payload
    if has_method and has_id:
        return 'request'
    if has_method and not has_id:
        return 'notification'
    has_result = 'result' in payload
    has_error = 'error' in payload
    if not has_method and has_id and has_result != has_error:
        return 'response'
    return 'invalid'


def _is_initialize(payload):
    return _message_kind(payload) == 'request' and payload.get('method') == 'initialize'


async def _dispatch_locked(app, session, payload):
    async with session['lock']:
        return await anyio.to_thread.run_sync(lambda: dispatch(app, session, payload))


async def mcp_endpoint(request):
    denied = _reject(request)
    if denied is not None:
        return denied
    if request.method == 'GET':
        rejected = _streamable_version_error(request)
        if rejected is not None:
            return rejected
        return Response(status_code=405, headers={'Allow': 'POST, DELETE'})
    if request.method == 'DELETE':
        return await _mcp_delete(request)
    return await _mcp_post(request)


async def _mcp_delete(request):
    rejected = _streamable_version_error(request)
    if rejected is not None:
        return rejected
    session_id = _session_header(request)
    if not session_id:
        return JSONResponse({'detail': '缺少 MCP 会话标识。'}, status_code=400)
    async with request.app.state.http_session_lock:
        _expire_http_sessions(request.app)
        session = request.app.state.http_sessions.pop(session_id, None)
    if session is None:
        return JSONResponse({'detail': 'MCP 会话不存在或已结束。'}, status_code=404)
    return Response(status_code=204)


async def _mcp_post(request):
    if not _STREAMABLE_ACCEPT <= _media_types(request.headers.get('accept')):
        return JSONResponse(
            {'detail': 'Accept 须同时包含 application/json 和 text/event-stream。'}, status_code=406)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'detail': '请求不是有效的 JSON。'}, status_code=400)
    if isinstance(payload, list):
        return JSONResponse({'detail': '不支持批量请求。'}, status_code=400)
    if _message_kind(payload) == 'invalid':
        return JSONResponse({'detail': '请求不是有效的 JSON-RPC。'}, status_code=400)
    if _is_initialize(payload) and _session_header(request) is None:
        return await _mcp_initialize(request, payload)
    if not _is_initialize(payload):
        rejected = _streamable_version_error(request)
        if rejected is not None:
            return rejected
    session, missing = await _lookup_http_session(request.app, _session_header(request))
    if missing is not None:
        return missing
    if _message_kind(payload) == 'response':
        return Response(status_code=202)
    response = await _dispatch_locked(request.app, session, payload)
    if response is None:
        return Response(status_code=202)
    headers = {'Mcp-Session-Id': session['id']} if _is_initialize(payload) and 'result' in response else None
    return JSONResponse(response, headers=headers)


async def _mcp_initialize(request, payload):
    session, rejected = await _open_http_session(request.app)
    if rejected is not None:
        return rejected
    response = await _dispatch_locked(request.app, session, payload)
    if not isinstance(response, dict) or 'result' not in response:
        await _drop_http_session(request.app, session['id'])
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)
    return JSONResponse(response, headers={'Mcp-Session-Id': session['id']})


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
    allowed = session.get('protocol_versions')
    if allowed is None:
        allowed = PROTOCOL_VERSIONS
    fallback = session.get('protocol_default')
    if fallback is None:
        fallback = DEFAULT_PROTOCOL
    session['initialized'] = True
    session['protocol'] = requested if requested in allowed else fallback
    return {
        'protocolVersion': session['protocol'],
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': dict(SERVER_INFO),
        'instructions': (
            '只读尽调证据服务。先 list_projects，再 list_documents 或 list_subjects。'
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
        elif name == 'list_subjects':
            arguments = _arguments(params, ('project_id',), ('project_id',))
            payload = _list_subjects(app.state.database, _uuid(arguments['project_id'], '项目标识'))
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


def _list_subjects(database, project_id):
    # subjects() 在项目不存在时抛 LookupError，与 list_documents 的「项目不存在。」同一句。
    # 标识沿用材料上的 subject_id。角色仍叫 role，与主体表和现有主体接口一致。
    subjects = []
    for row in database.subjects(project_id):
        subjects.append({
            'subject_id': row['id'],
            'project_id': row['project_id'],
            'name': row['name'],
            'role': row['role'],
            'created_at': row['created_at'],
        })
    return {'subjects': subjects}


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
    _attach_material(evidence.database, project_id, chunks)
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
    if item['index_kind'] == 'source_unit':
        item.update(_office_locator(hit, source))
    elif item['index_kind'] == 'parent_child':
        # 父子块的索引版本已经在引用参数里，不必再查库。一条子块可以跨页，没有单一 source_revision。
        version = _reference_version(hit)
        if version:
            item['index_version'] = version
    return item


def _office_locator(hit, source):
    def pick(name):
        if source.get(name) is not None:
            return source.get(name)
        return hit.get(name)

    # formula_state 的空字符串是库里的合法状态（没有公式标记），字段仍保留，不改成省略。
    return {
        'locator': pick('locator'),
        'locator_label': pick('locator_label'),
        'unit_id': pick('unit_id'),
        'formula_state': pick('formula_state'),
    }


def _reference_version(hit):
    reference = hit.get('reference')
    if not isinstance(reference, dict):
        return None
    params = reference.get('params')
    if not isinstance(params, dict):
        return None
    version = params.get('index_version')
    if isinstance(version, str) and version:
        return version
    return None


def _as_uuid(value):
    try:
        return value if isinstance(value, UUID) else UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _attach_material(database, project_id, chunks):
    """一次连接里按材料、页级片段、Office 片段各查一批。不按命中逐条查库。"""
    if not chunks:
        return
    documents, pages, offices = [], [], []
    seen_documents = set()
    for chunk in chunks:
        document_id = _as_uuid(chunk.get('document_id'))
        if document_id is not None and document_id not in seen_documents:
            seen_documents.add(document_id)
            documents.append(document_id)
        chunk_id = _as_uuid(chunk.get('chunk_id'))
        if chunk_id is None:
            continue
        if chunk.get('index_kind') == 'source_unit':
            offices.append(chunk_id)
        elif chunk.get('index_kind') == 'page':
            pages.append(chunk_id)
    blank = material_view({})
    with database.connect() as conn:
        materials = _material_rows(conn, project_id, documents) if documents else {}
        page_versions = _page_versions(conn, project_id, pages) if pages else {}
        office_versions = _office_versions(conn, project_id, offices) if offices else {}
    for chunk in chunks:
        chunk.update(materials.get(str(chunk.get('document_id')), blank))
        # 只有页级命中能对上当前页的单一 source_revision。Office 没有这份修订；父子块可以跨页。
        if chunk.get('index_kind') == 'page':
            extra = page_versions.get(str(chunk.get('chunk_id')))
            if not extra:
                continue
            if extra.get('index_version'):
                chunk['index_version'] = extra['index_version']
            if extra.get('source_revision'):
                chunk['source_revision'] = extra['source_revision']
        elif chunk.get('index_kind') == 'source_unit':
            extra = office_versions.get(str(chunk.get('chunk_id')))
            if extra and extra.get('index_version'):
                chunk['index_version'] = extra['index_version']


def _material_rows(conn, project_id, document_ids):
    rows = conn.execute('''SELECT d.id, d.category, d.subject_id, sub.name AS subject_name,
            sub.role AS subject_role, d.date_kind, d.point_date, d.period_start, d.period_end
        FROM dd_agent.documents d
        LEFT JOIN dd_agent.project_subjects sub
          ON sub.id = d.subject_id AND sub.project_id = d.project_id
        WHERE d.project_id = %s AND d.id = ANY(%s::uuid[])''', (project_id, list(document_ids))).fetchall()
    return {str(row['id']): material_view(row) for row in rows}


def _page_versions(conn, project_id, chunk_ids):
    rows = conn.execute('''SELECT c.id::text AS chunk_id, c.index_version AS chunk_index_version, p.source_revision
        FROM dd_agent.evidence_chunks c
        JOIN dd_agent.documents d ON d.id = c.document_id
        JOIN dd_agent.indexed_pages i
          ON i.document_id = c.document_id AND i.page_number = c.page_number AND i.index_version = c.index_version
        JOIN dd_agent.effective_document_pages p
          ON p.document_id = i.document_id AND p.page_number = i.page_number
         AND p.pipeline_version = i.pipeline_version AND p.updated_at = i.source_updated_at
         AND p.source_revision = i.source_revision AND p.status = 'succeeded'
        WHERE d.project_id = %s AND c.id = ANY(%s::uuid[])''', (project_id, list(chunk_ids))).fetchall()
    return {row['chunk_id']: {'index_version': row['chunk_index_version'], 'source_revision': row['source_revision']}
            for row in rows}


def _office_versions(conn, project_id, chunk_ids):
    rows = conn.execute('''SELECT c.id::text AS chunk_id, c.index_version AS office_index_version
        FROM dd_agent.source_unit_chunks c
        JOIN dd_agent.documents d ON d.id = c.document_id
        WHERE d.project_id = %s AND c.id = ANY(%s::uuid[])''', (project_id, list(chunk_ids))).fetchall()
    return {row['chunk_id']: {'index_version': row['office_index_version']} for row in rows}


def _tool_ok(payload):
    text = json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(',', ':'))
    return {'content': [{'type': 'text', 'text': text}]}


def _tool_error(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}
