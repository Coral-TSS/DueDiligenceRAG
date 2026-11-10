"""Streamable HTTP /mcp。旧 SSE 仍由 test_mcp_server.py 覆盖。"""
import json
import time

from fastapi.testclient import TestClient

from dd_agent.config import Settings
from dd_agent.mcp_server import HTTP_SESSION_LIMIT, create_mcp_app

KEY = 'mcp-test-key'
ACCEPT = 'application/json, text/event-stream'


def test_streamable_rejects_bad_key_and_origin_before_session(tmp_path):
    app = create_mcp_app(Settings(data_dir=tmp_path, mcp_api_key=KEY))
    with TestClient(app) as http:
        missing = http.post('/mcp', json=_initialize_body())
        wrong = http.post('/mcp', headers={'Authorization': 'Bearer wrong-key', 'Accept': ACCEPT}, json=_initialize_body())
        basic = http.post('/mcp', headers={'Authorization': f'Basic {KEY}', 'Accept': ACCEPT}, json=_initialize_body())
        other = http.post('/mcp', headers={'X-API-Key': 'wrong-key', 'Accept': ACCEPT}, json=_initialize_body())
        # 两个头都在时以 Authorization 为准，正确的 X-API-Key 不能绕过错误的 Bearer。
        both = http.post('/mcp', headers={
            'Authorization': 'Bearer wrong-key', 'X-API-Key': KEY, 'Accept': ACCEPT}, json=_initialize_body())
        # 密钥错误且没有会话头时仍是 401，不先返回缺会话。
        no_session = http.post('/mcp', json=_rpc('tools/list'))
        cross = http.post('/mcp', headers=_headers(origin='http://evil.example'), json=_initialize_body())
        bad_origin_and_key = http.get('/mcp', headers={'Origin': 'http://evil.example'})
        same_origin = http.post('/mcp', headers=_headers(origin='http://testserver'), json=_initialize_body())
        alternate = http.post('/mcp', headers={
            'X-API-Key': KEY, 'Accept': ACCEPT, 'Content-Type': 'application/json'}, json=_initialize_body('2025-03-26', 2))
    for response in (missing, wrong, basic, other, both, no_session):
        assert response.status_code == 401, response.text
        assert response.json()['detail'] == '需要有效的 MCP API 密钥。'
        assert response.headers['www-authenticate'] == 'Bearer'
        assert KEY not in response.text
    assert cross.status_code == 403
    assert bad_origin_and_key.status_code == 403
    assert KEY not in cross.text and KEY not in bad_origin_and_key.text
    assert same_origin.status_code == 200
    assert alternate.status_code == 200
    assert alternate.json()['result']['protocolVersion'] == '2025-03-26'


def test_session_header_version_accept_and_notification(tmp_path, monkeypatch):
    app = create_mcp_app(Settings(data_dir=tmp_path, mcp_api_key=KEY))
    app.state.database = _Projects()
    with TestClient(app) as http:
        early = http.post('/mcp', headers=_headers(), json=_rpc('tools/list'))
        assert early.status_code == 400
        assert early.json()['detail'] == '缺少 MCP 会话标识。'
        unknown = http.post('/mcp', headers=_headers(session='deadbeef'), json=_rpc('tools/list'))
        assert unknown.status_code == 404
        assert unknown.json()['detail'] == 'MCP 会话不存在或已结束。'

        opened = _open(http, '2025-11-25')
        assert opened['protocolVersion'] == '2025-06-18'
        sid = opened['session']
        assert sid and set(sid) <= set('0123456789abcdef')
        again = _open(http, '2025-06-18')
        assert again['protocolVersion'] == '2025-06-18'
        older = _open(http, '2025-03-26')
        assert older['protocolVersion'] == '2025-03-26'
        legacy = _open(http, '2024-11-05')
        assert legacy['protocolVersion'] == '2025-06-18'

        bad_version = http.post('/mcp', headers=_headers(session=sid, protocol='2025-11-25'), json=_rpc('tools/list', {}, 2))
        assert bad_version.status_code == 400
        assert bad_version.json()['detail'] == '不支持的 MCP-Protocol-Version。'
        # 无效版本头在查会话之前拒绝，未知会话也是 400 而不是 404。
        bad_on_unknown = http.post('/mcp', headers=_headers(session='nope', protocol='1999-01-01'), json=_rpc('tools/list'))
        assert bad_on_unknown.status_code == 400
        still = http.post('/mcp', headers=_headers(session=sid, protocol='2025-06-18'), json=_rpc('ping', {}, 3))
        assert still.status_code == 200

        only_json = http.post('/mcp', headers=_headers(session=sid, accept='application/json'), json=_rpc('ping', {}, 4))
        only_sse = http.post('/mcp', headers=_headers(session=sid, accept='text/event-stream'), json=_rpc('ping', {}, 5))
        bare = http.post('/mcp', headers=_headers(session=sid, accept=''), json=_rpc('ping', {}, 6))
        for response in (only_json, only_sse, bare):
            assert response.status_code == 406, response.text
        weighted = http.post('/mcp', headers=_headers(
            session=sid, accept='text/event-stream;q=0.5, application/json;q=1'), json=_rpc('ping', {}, 7))
        assert weighted.status_code == 200

        # 不带协议头时按 2025-03-26 接受。
        omitted = http.post('/mcp', headers=_headers(session=sid), json=_rpc('tools/list', {}, 8))
        assert omitted.status_code == 200, omitted.text
        names = [tool['name'] for tool in omitted.json()['result']['tools']]
        assert names == ['list_projects', 'list_documents', 'search_evidence', 'coverage_gaps', 'read_evidence']
        assert 'text/event-stream' not in omitted.headers['content-type']
        assert omitted.headers['content-type'].startswith('application/json')

        noted = http.post('/mcp', headers=_headers(session=sid, protocol='2025-06-18'),
                          json=_rpc('notifications/initialized', {}, note=True))
        assert noted.status_code == 202
        assert noted.content == b''
        reply = http.post('/mcp', headers=_headers(session=sid, protocol='2025-03-26'), json={
            'jsonrpc': '2.0', 'id': 9, 'result': {}})
        assert reply.status_code == 202
        assert reply.content == b''
        batch = http.post('/mcp', headers=_headers(session=sid, protocol='2025-06-18'), json=[
            _rpc('ping', {}, 10), _rpc('ping', {}, 11)])
        assert batch.status_code == 400
        assert batch.json()['detail'] == '不支持批量请求。'

        called = http.post('/mcp', headers=_headers(session=sid, protocol='2025-06-18'), json=_rpc('tools/call', {
            'name': 'list_projects', 'arguments': {}}, 12))
        assert called.status_code == 200
        result = called.json()['result']
        assert result.get('isError') is not True
        assert json.loads(result['content'][0]['text']) == {'projects': [{
            'id': 'p', 'name': '桩项目', 'created_at': '2026-01-01T00:00:00Z'}]}
        failed = http.post('/mcp', headers=_headers(session=sid, protocol='2025-06-18'), json=_rpc('tools/call', {
            'name': 'missing-tool', 'arguments': {}}, 13))
        assert failed.json()['result']['isError'] is True

        denied = http.get('/mcp')
        assert denied.status_code == 401
        get = http.get('/mcp', headers=_headers(session=sid, protocol='2025-06-18'))
        assert get.status_code == 405
        assert get.headers['allow'] == 'POST, DELETE'
        get_bad = http.get('/mcp', headers=_headers(session=sid, protocol='2025-11-25'))
        assert get_bad.status_code == 400

        removed = http.delete('/mcp', headers=_headers(session=sid, protocol='2025-06-18'))
        assert removed.status_code == 204
        assert removed.content == b''
        gone = http.post('/mcp', headers=_headers(session=sid, protocol='2025-06-18'), json=_rpc('ping', {}, 14))
        assert gone.status_code == 404
        missing_delete = http.delete('/mcp', headers=_headers(protocol='2025-06-18'))
        assert missing_delete.status_code == 400
        unknown_delete = http.delete('/mcp', headers=_headers(session='deadbeef', protocol='2025-06-18'))
        assert unknown_delete.status_code == 404

        expired_id = _open(http, '2025-06-18')['session']
        app.state.http_sessions[expired_id]['touched'] = time.monotonic() - 30 * 60 - 5
        expired = http.post('/mcp', headers=_headers(session=expired_id, protocol='2025-06-18'), json=_rpc('ping', {}, 15))
        assert expired.status_code == 404

    monkeypatch.setattr('dd_agent.mcp_server.HTTP_SESSION_LIMIT', 1)
    limited = create_mcp_app(Settings(data_dir=tmp_path, mcp_api_key=KEY))
    with TestClient(limited) as http:
        assert _open(http, '2025-06-18')['protocolVersion'] == '2025-06-18'
        full = http.post('/mcp', headers=_headers(), json=_initialize_body('2025-06-18', 2))
        assert full.status_code == 503
        assert full.json()['detail'] == 'MCP 会话数量已达上限。'
        assert 'mcp-session-id' not in full.headers
    assert HTTP_SESSION_LIMIT == 128


class _Projects:
    def projects(self):
        return [{'id': 'p', 'name': '桩项目', 'created_at': '2026-01-01T00:00:00Z'}]


def _headers(*, session=None, protocol=None, accept=ACCEPT, origin=None):
    headers = {'Authorization': f'Bearer {KEY}', 'Accept': accept, 'Content-Type': 'application/json'}
    if session is not None:
        headers['Mcp-Session-Id'] = session
    if protocol is not None:
        headers['MCP-Protocol-Version'] = protocol
    if origin is not None:
        headers['Origin'] = origin
    return headers


def _rpc(method, params=None, message_id=1, note=False):
    payload = {'jsonrpc': '2.0', 'method': method}
    if params is not None:
        payload['params'] = params
    if not note:
        payload['id'] = message_id
    return payload


def _initialize_body(version='2025-06-18', message_id=1):
    return _rpc('initialize', {
        'protocolVersion': version, 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '0'}}, message_id)


def _open(http, version):
    response = http.post('/mcp', headers=_headers(), json=_initialize_body(version))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['jsonrpc'] == '2.0'
    assert 'event:' not in response.text
    return {'session': response.headers['mcp-session-id'], 'protocolVersion': body['result']['protocolVersion']}
