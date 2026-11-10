"""HTTP/SSE MCP：密钥、项目／材料列表，以及不加载真实模型的混合检索。"""
from contextlib import contextmanager
import json
import socket
import sys
import threading
import time
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from dd_agent.config import Settings
from dd_agent.mcp_server import create_mcp_app
from test_retrieval import setup
from test_workspace import PDF, application, client, project, upload

KEY = 'mcp-test-key'


def test_api_key_rejects_missing_or_wrong_and_accepts_a_matching_key(tmp_path):
    app = create_mcp_app(Settings(data_dir=tmp_path, mcp_api_key=KEY))
    with TestClient(app) as http:
        missing = http.get('/sse')
        wrong = http.get('/sse', headers={'Authorization': 'Bearer wrong-key'})
        other_header = http.post('/messages', headers={'X-API-Key': 'wrong-key'})
        bad_scheme = http.get('/sse', headers={'Authorization': f'Basic {KEY}'})
        cross_site = http.get('/sse', headers={'Authorization': f'Bearer {KEY}', 'Origin': 'http://evil.example'})
    for response in (missing, wrong, other_header, bad_scheme):
        assert response.status_code == 401
        assert response.json()['detail'] == '需要有效的 MCP API 密钥。'
        assert KEY not in response.text
    assert cross_site.status_code == 403
    assert KEY not in cross_site.text

    with _running(app) as base:
        bearer = _Session(base, {'Authorization': f'Bearer {KEY}'})
        try:
            assert bearer.endpoint().startswith('/messages?session_id=')
        finally:
            bearer.close()
        alternate = _Session(base, {'X-API-Key': KEY})
        try:
            assert 'session_id=' in alternate.endpoint()
        finally:
            alternate.close()


def test_list_projects_and_documents_include_identifiers_and_empty_lists(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    mcp = create_mcp_app(Settings(database_url=database_url, data_dir=tmp_path, mcp_api_key=KEY))
    with _running(mcp) as base:
        session = _Session(base, {'Authorization': f'Bearer {KEY}'})
        try:
            early = session.call('tools/list', {}, 2)
            assert early['error']['message'] == '请先完成 initialize。'
            _initialize(session)
            names = [tool['name'] for tool in _result(session.call('tools/list', {}, 20))['tools']]
            assert names[:3] == ['list_projects', 'list_documents', 'search_evidence']
            assert 'coverage_gaps' in names and 'read_evidence' in names
            assert _payload(session.call('tools/call', {'name': 'list_projects', 'arguments': {}}, 3)) == {'projects': []}
            with client(app) as http:
                project_id = project(http)
                uploaded = upload(http, project_id)
                document_id = uploaded.json()['document']['id']
                storage_key = app.state.database.document(project_id, document_id)['storage_key']
                empty_id = project(http, '空项目')
            listed = _payload(session.call('tools/call', {'name': 'list_projects', 'arguments': {}}, 4))
            assert any(row['id'] == project_id and row['name'] == '合成尽调项目' for row in listed['projects'])
            documents = _payload(session.call('tools/call', {
                'name': 'list_documents', 'arguments': {'project_id': project_id}}, 5))
            text = json.dumps(documents, ensure_ascii=False)
            assert documents['documents'][0]['id'] == document_id
            assert documents['documents'][0]['filenames'] == ['合同.pdf']
            assert 'storage_key' not in text and storage_key not in text and '.bin' not in text
            assert '%PDF' not in text
            empty = _payload(session.call('tools/call', {
                'name': 'list_documents', 'arguments': {'project_id': empty_id}}, 6))
            assert empty == {'documents': []}
            missing = session.call('tools/call', {'name': 'list_documents', 'arguments': {'project_id': str(uuid4())}}, 7)
            assert missing['result']['isError'] is True
            assert missing['result']['content'][0]['text'] == '项目不存在。'
            invalid = session.call('tools/call', {'name': 'list_documents', 'arguments': {'project_id': 'not-a-uuid'}}, 8)
            assert invalid['result']['isError'] is True
            assert '项目标识无效' in invalid['result']['content'][0]['text']
            assert 'psycopg' not in invalid['result']['content'][0]['text']
        finally:
            session.close()


def test_search_returns_chunks_empty_list_and_rejects_bad_key(tmp_path, database_url):
    app, project_id, document_id, service, _ = setup(tmp_path, database_url)
    service.index(project_id, document_id, [1, 2])
    with client(app) as http:
        other = upload(http, project_id, '另一份.pdf', body=PDF + b'\nsecond').json()['document']['id']
        empty_id = project(http, '空项目')
    database = app.state.database
    database.set_reading(other, 1, 'scan-v1')
    database.save_page(other, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text='本公司未记载注册资本。'))
    service.index(project_id, other, [1])
    mcp = create_mcp_app(Settings(database_url=database_url, data_dir=tmp_path, mcp_api_key=KEY))
    mcp.state.evidence = service
    with _running(mcp) as base:
        session = _Session(base, {'Authorization': f'Bearer {KEY}'})
        try:
            _initialize(session)
            found = _payload(session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': project_id, 'query': '注册资本'}}, 2))
            assert found['status'] == 'candidates'
            hit = next(row for row in found['results'] if row['text'] == '注册资本为2000万元。')
            assert hit['project_id'] == project_id
            assert hit['document_id'] == document_id
            assert hit['page_number'] == 1
            assert hit['chunk_id']
            assert hit['recall']['fusion'] == 'rrf'
            assert hit['recall']['channels']
            assert hit['recall']['fusion_score'] > 0
            assert 'storage_key' not in json.dumps(found)
            narrowed = _payload(session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': project_id, 'query': '注册资本', 'document_id': other}}, 3))
            assert narrowed['results']
            assert {row['document_id'] for row in narrowed['results']} == {other}
            assert all('注册资本为2000万元' not in row['text'] for row in narrowed['results'])
            assert all(row['recall']['fusion'] == 'rrf' for row in narrowed['results'])
            empty = _payload(session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': empty_id, 'query': '注册资本'}}, 4))
            assert empty['status'] == 'not_indexed'
            assert empty['results'] == []
            denied = httpx.post(base + session.endpoint(), headers={'Authorization': 'Bearer wrong-key'},
                                json={'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {
                                    'name': 'search_evidence', 'arguments': {'project_id': project_id, 'query': '注册资本'}}},
                                timeout=10, trust_env=False)
            assert denied.status_code == 401
            assert '注册资本为2000万元' not in denied.text
            assert KEY not in denied.text
            # 数据库异常只返回固定说明，不把连接串或 SQL 带回客户端。
            mcp.state.evidence = _BrokenSearch()
            hidden = session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': project_id, 'query': '注册资本'}}, 6)
            message = hidden['result']['content'][0]['text']
            assert hidden['result']['isError'] is True
            assert message == '数据库暂不可用或尚未初始化。'
            assert 'super-secret' not in message and 'SELECT' not in message
        finally:
            session.close()


def test_mcp_command_requires_key_and_binds_configured_host(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr('dd_agent.config.load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setenv('DDA_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('DDA_DATABASE_URL', 'postgresql://unused')
    monkeypatch.delenv('DDA_MCP_API_KEY', raising=False)
    monkeypatch.setattr(sys, 'argv', ['dd_agent', 'mcp'])
    from dd_agent.__main__ import main
    assert main() == 1
    assert 'DDA_MCP_API_KEY' in capsys.readouterr().out
    with pytest.raises(ValueError):
        Settings(mcp_host='http://0.0.0.0', mcp_port=8766)
    with pytest.raises(ValueError):
        Settings(mcp_port=0)
    monkeypatch.setenv('DDA_MCP_API_KEY', 'local-test-key')
    monkeypatch.setenv('DDA_MCP_HOST', '0.0.0.0')
    monkeypatch.setenv('DDA_MCP_PORT', '8899')
    captured = {}

    def run(app, host, port, **kwargs):
        captured['host'] = host
        captured['port'] = port
        captured['paths'] = sorted(route.path for route in app.router.routes)

    monkeypatch.setattr('dd_agent.__main__.uvicorn.run', run)
    assert main() == 0
    assert captured == {'host': '0.0.0.0', 'port': 8899, 'paths': ['/messages', '/sse']}
    assert Settings.from_env().mcp_api_key == 'local-test-key'


class _BrokenSearch:
    def search(self, *args, **kwargs):
        raise psycopg.OperationalError('password=super-secret DETAIL: SELECT secret_table')


def _initialize(session):
    opened = session.call('initialize', {
        'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '0'}}, 1)
    assert opened['result']['protocolVersion'] == '2024-11-05'
    assert opened['result']['serverInfo']['name'] == 'dd-agent-evidence'
    session.call('notifications/initialized', {}, note=True)


def _result(message):
    assert 'error' not in message
    assert message['result'].get('isError') is not True
    return message['result']


def _payload(message):
    body = _result(message)
    return json.loads(body['content'][0]['text'])


@contextmanager
def _running(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    from uvicorn import Config, Server
    server = Server(Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, name='mcp-test', daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError('MCP test server did not start')
        time.sleep(0.02)
    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class _Session:
    def __init__(self, base, headers):
        self.base = base
        self.headers = headers
        self.lines = []
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.error = None
        self.status = None
        self._endpoint = None
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise AssertionError(f'SSE 未建立：{self.error}')
        assert self.status == 200, self.status

    def _read(self):
        try:
            with httpx.Client(timeout=None, trust_env=False) as http:
                with http.stream('GET', self.base + '/sse', headers=self.headers) as response:
                    self.status = response.status_code
                    self.ready.set()
                    for line in response.iter_lines():
                        with self.lock:
                            self.lines.append(line)
                        if self.stop.is_set():
                            break
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def endpoint(self):
        if self._endpoint is None:
            self._endpoint = self._wait(lambda events: next((data for name, data in events if name == 'endpoint'), None))
        return self._endpoint

    def call(self, method, params=None, message_id=1, note=False):
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        if not note:
            payload['id'] = message_id
        response = httpx.post(self.base + self.endpoint(), headers={**self.headers, 'Content-Type': 'application/json'},
                              json=payload, timeout=30, trust_env=False)
        assert response.status_code == 202, response.text
        if note:
            return None
        return json.loads(self._wait(lambda events: next((data for name, data in events
            if name == 'message' and json.loads(data).get('id') == message_id), None)))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)

    def _wait(self, pick):
        deadline = time.time() + 20
        while time.time() < deadline:
            found = pick(_events(self._snapshot()))
            if found is not None:
                return found
            if self.error is not None:
                raise AssertionError(self.error)
            time.sleep(0.02)
        raise AssertionError(self._snapshot())

    def _snapshot(self):
        with self.lock:
            return list(self.lines)


def _events(lines):
    events = []
    name = 'message'
    data = []
    for line in lines:
        if line == '':
            if data:
                events.append((name, '\n'.join(data)))
            name = 'message'
            data = []
            continue
        if line.startswith(':'):
            continue
        if line.startswith('event:'):
            name = line.split(':', 1)[1].strip()
        elif line.startswith('data:'):
            data.append(line.split(':', 1)[1].lstrip())
    return events
