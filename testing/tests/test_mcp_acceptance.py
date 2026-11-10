"""仓库内的 MCP 对接：列项目和材料、覆盖、检索、补读，并拒绝跨项目。

教练 Bot 的 Harness 冻结调用不在这里。本文件只用合成客户端。
"""
import json
from uuid import uuid4

from dd_agent.config import Settings
from dd_agent.mcp_server import create_mcp_app
from dd_agent.retrieval import EvidenceService
from test_mcp_server import KEY, _Session, _initialize, _payload, _running
from test_retrieval import TinyModels
from test_workspace import PDF, application, client, project, upload


MARKER = '甲项目注册资本句子。'
FOREIGN = '乙项目不该被补读的句子。'


def test_mcp_lists_coverage_search_and_reread_inside_one_project(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    models = TinyModels()
    service = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models, database=app.state.database)
    with client(app) as http:
        project_id = project(http, '项目甲')
        other_id = project(http, '项目乙')
        empty_id = project(http, '空项目')
        document_id = upload(http, project_id, '章程.pdf').json()['document']['id']
        foreign_id = upload(http, other_id, '章程.pdf', PDF + b'\nB').json()['document']['id']
    database = app.state.database
    database.set_reading(document_id, 2, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text=MARKER))
    database.set_reading(foreign_id, 1, 'scan-v1')
    database.save_page(foreign_id, 'scan-v1', dict(page_number=1, status='failed', method='ocr', text=FOREIGN, error_code='synthetic'))
    service.index(project_id, document_id, [1])
    with database.connect() as conn:
        chunk_id = str(conn.execute(
            'SELECT id FROM dd_agent.evidence_chunks WHERE document_id=%s', (document_id,)).fetchone()['id'])
    mcp = create_mcp_app(Settings(database_url=database_url, data_dir=tmp_path, mcp_api_key=KEY))
    mcp.state.evidence = service
    with _running(mcp) as base:
        session = _Session(base, {'Authorization': f'Bearer {KEY}'})
        try:
            _initialize(session)
            names = [tool['name'] for tool in _payload_tools(session)]
            assert names[:3] == ['list_projects', 'list_documents', 'search_evidence']
            projects = _payload(session.call('tools/call', {'name': 'list_projects', 'arguments': {}}, 2))
            assert {row['id'] for row in projects['projects']} >= {project_id, other_id, empty_id}
            documents = _payload(session.call('tools/call', {'name': 'list_documents', 'arguments': {'project_id': project_id}}, 3))
            assert [row['id'] for row in documents['documents']] == [document_id]
            assert FOREIGN not in json.dumps(documents, ensure_ascii=False)
            coverage = _payload(session.call('tools/call', {'name': 'coverage_gaps', 'arguments': {'project_id': project_id}}, 4))
            assert coverage['project_id'] == project_id
            assert coverage['complete'] is False
            assert any(item['document_id'] == document_id and 'unprocessed' in item['kinds'] for item in coverage['items'])
            assert FOREIGN not in json.dumps(coverage, ensure_ascii=False)
            empty = _payload(session.call('tools/call', {'name': 'coverage_gaps', 'arguments': {'project_id': empty_id}}, 5))
            assert empty['complete'] is False and empty['items'] == []
            found = _payload(session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': project_id, 'query': '注册资本'}}, 6))
            assert any(row['text'] == MARKER for row in found['results'])
            assert all(row['project_id'] == project_id for row in found['results'])
            read = _payload(session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'evidence_id': chunk_id, 'limit': 100}}, 7))
            assert read['text'] == MARKER
            assert read['project_id'] == project_id
            ranged = _payload(session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'document_id': document_id, 'page_from': 1, 'page_to': 2, 'limit': 4000}}, 8))
            assert ranged['items'][0]['text'] == MARKER
            assert ranged['items'][1]['status'] == 'not_processed'
            denied = session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': other_id, 'evidence_id': chunk_id}}, 9)
            missing = session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'evidence_id': str(uuid4())}}, 10)
            assert denied['result']['isError'] is True and missing['result']['isError'] is True
            assert denied['result']['content'][0]['text'] == missing['result']['content'][0]['text']
            assert MARKER not in denied['result']['content'][0]['text']
            assert '存在于' not in denied['result']['content'][0]['text']
            no_project = session.call('tools/call', {'name': 'read_evidence', 'arguments': {'evidence_id': chunk_id}}, 11)
            assert no_project['result']['isError'] is True
            assert 'project_id' in no_project['result']['content'][0]['text']
            assert MARKER not in no_project['result']['content'][0]['text']
            foreign_range = session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'document_id': foreign_id, 'page_from': 1, 'page_to': 1}}, 12)
            assert foreign_range['result']['isError'] is True
            assert FOREIGN not in foreign_range['result']['content'][0]['text']
        finally:
            session.close()


def _payload_tools(session):
    return session.call('tools/list', {}, 30)['result']['tools']
