"""主体列表，以及检索／补读附带的材料元数据。合成材料，不读客户文件。"""
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4

from docx import Document
from fastapi.testclient import TestClient

from dd_agent.config import Settings
from dd_agent.mcp_server import TOOLS, _chunk
from dd_agent.office import OfficeIndex
from dd_agent.retrieval import EvidenceService
from test_mcp_server import KEY, _Session, _initialize, _payload, _running
from test_mcp_streamable import _headers, _open, _rpc
from test_retrieval import TinyModels
from test_workspace import PDF, application, client, project, upload


MATERIAL_FIELDS = (
    'category', 'subject_id', 'subject_name', 'subject_role',
    'date_kind', 'point_date', 'period_start', 'period_end',
)
OFFICE_FIELDS = ('locator', 'locator_label', 'unit_id', 'formula_state')


def test_parent_child_chunk_uses_the_reference_version_without_one_source_revision():
    project_id = '11111111-1111-1111-1111-111111111111'
    hit = {
        'index_kind': 'parent_child',
        'document_id': project_id,
        'filenames': ['章程.pdf'],
        'build_id': 'b' * 64,
        'parent_id': 'p' * 64,
        'reference': {'url': '/api/parent', 'params': {'index_version': 'a' * 64}},
        'rerank_score': 0.9,
        'similarity': 0.5,
    }
    child = {
        'id': 'c' * 64,
        'text': '注册资本',
        'start_offset': 0,
        'end_offset': 4,
        'source_spans': [
            {'page_number': 1, 'start': 0, 'end': 2},
            {'page_number': 2, 'start': 0, 'end': 2},
        ],
        'recall': {'fusion': 'rrf', 'channels': ['vector'], 'fusion_score': 0.1},
    }
    item = _chunk(project_id, hit, child)
    assert item['index_kind'] == 'parent_child'
    assert item['text'] == '注册资本'
    assert item['build_id'] == 'b' * 64 and item['parent_id'] == 'p' * 64
    assert item['index_version'] == 'a' * 64
    assert item['recall']['fusion'] == 'rrf'
    assert 'source_revision' not in item
    for key in OFFICE_FIELDS:
        assert key not in item


def test_subjects_and_material_metadata_on_both_transports(tmp_path, database_url):
    migrations = sorted(path.name for path in Path('src/dd_agent/migrations').glob('*.sql'))
    assert migrations[-1].startswith('019_')
    tool = next(item for item in TOOLS if item['name'] == 'list_subjects')
    assert tool['inputSchema']['required'] == ['project_id']
    assert tool['inputSchema']['additionalProperties'] is False

    app = application(tmp_path, database_url, migrate=True, limit=2_000_000)
    with app.state.database.connect() as conn:
        assert conn.execute('SELECT max(version) AS version FROM dd_agent.schema_version').fetchone()['version'] == 19
    models = TinyModels()
    service = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models, database=app.state.database)
    with client(app) as http:
        project_id = project(http, '项目甲')
        other_id = project(http, '项目乙')
        empty_id = project(http, '空项目')
        subject_id = http.post(f'/api/projects/{project_id}/subjects', json={'name': '甲公司', 'role': 'target'}).json()['id']
        unbound_id = http.post(f'/api/projects/{project_id}/subjects', json={'name': '未绑定主体', 'role': 'other'}).json()['id']
        foreign_subject = http.post(f'/api/projects/{other_id}/subjects', json={'name': '乙公司', 'role': 'shareholder'}).json()['id']
        articles = upload(http, project_id, '章程.pdf', PDF + b'\narticles').json()['document']['id']
        plain = upload(http, project_id, '未填.pdf', PDF + b'\nplain').json()['document']['id']
        office = http.post(
            f'/api/projects/{project_id}/documents',
            files={'file': ('说明.docx', _docx('注册资本办公说明。'), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
        )
        assert office.status_code == 201, office.text
        office_id = office.json()['document']['id']
        assert http.patch(
            f'/api/projects/{project_id}/documents/{articles}/metadata',
            json=_meta('articles', subject_id, 'point', '2023-06-01')).status_code == 200
        assert http.patch(
            f'/api/projects/{project_id}/documents/{office_id}/metadata',
            json=_meta('financial_statement', subject_id, 'period', start='2023-01-01', end='2023-12-31')).status_code == 200

    database = app.state.database
    _index(database, service, project_id, articles, '注册资本章程甲。')
    _index(database, service, project_id, plain, '注册资本未填主体。')
    service.index_office(project_id, office_id)
    with database.connect() as conn:
        revision = conn.execute(
            '''SELECT source_revision FROM dd_agent.effective_document_pages
               WHERE document_id = %s AND page_number = 1''', (articles,)).fetchone()['source_revision']
    page_version = service.version()
    office_version = OfficeIndex(service.settings, database, service.models).version()

    sse_app = _mcp(database_url, tmp_path, service)
    with _running(sse_app) as base:
        session = _Session(base, {'Authorization': f'Bearer {KEY}'})
        try:
            _initialize(session)
            names = [item['name'] for item in session.call('tools/list', {}, 20)['result']['tools']]
            assert names[:3] == ['list_projects', 'list_documents', 'search_evidence']
            assert names[-1] == 'list_subjects'

            listed = _payload(session.call('tools/call', {'name': 'list_subjects', 'arguments': {'project_id': project_id}}, 2))
            by_name = {row['name']: row for row in listed['subjects']}
            assert set(by_name) == {'甲公司', '未绑定主体'}
            assert by_name['甲公司']['subject_id'] == subject_id
            assert by_name['甲公司']['project_id'] == project_id
            assert by_name['甲公司']['role'] == 'target'
            assert by_name['未绑定主体']['subject_id'] == unbound_id
            assert by_name['未绑定主体']['role'] == 'other'
            for row in listed['subjects']:
                assert set(row) == {'subject_id', 'project_id', 'name', 'role', 'created_at'}
                assert isinstance(row['created_at'], str) and row['created_at']
            assert '乙公司' not in json.dumps(listed, ensure_ascii=False)

            foreign = _payload(session.call('tools/call', {'name': 'list_subjects', 'arguments': {'project_id': other_id}}, 3))
            assert {row['subject_id'] for row in foreign['subjects']} == {foreign_subject}
            assert '甲公司' not in json.dumps(foreign, ensure_ascii=False)
            empty = _payload(session.call('tools/call', {'name': 'list_subjects', 'arguments': {'project_id': empty_id}}, 4))
            assert empty == {'subjects': []}
            missing = session.call('tools/call', {'name': 'list_subjects', 'arguments': {'project_id': str(uuid4())}}, 5)
            same = session.call('tools/call', {'name': 'list_documents', 'arguments': {'project_id': str(uuid4())}}, 6)
            assert missing['result']['isError'] is True and same['result']['isError'] is True
            assert missing['result']['content'][0]['text'] == same['result']['content'][0]['text'] == '项目不存在。'
            invalid = session.call('tools/call', {'name': 'list_subjects', 'arguments': {'project_id': 'not-a-uuid'}}, 7)
            assert invalid['result']['isError'] is True and '项目标识无效' in invalid['result']['content'][0]['text']
            omitted = session.call('tools/call', {'name': 'list_subjects', 'arguments': {}}, 8)
            assert omitted['result']['isError'] is True and 'project_id' in omitted['result']['content'][0]['text']
            extra = session.call('tools/call', {'name': 'list_subjects', 'arguments': {'project_id': project_id, 'name': '甲公司'}}, 9)
            assert extra['result']['isError'] is True and extra['result']['content'][0]['text'] == '参数无法识别。'

            documents = _payload(session.call('tools/call', {'name': 'list_documents', 'arguments': {'project_id': project_id}}, 10))
            article_row = next(row for row in documents['documents'] if row['id'] == articles)
            assert article_row['category'] == 'articles'
            assert article_row['subject_id'] == subject_id
            assert article_row['subject_name'] == '甲公司'
            assert article_row['date_kind'] == 'point' and article_row['point_date'] == '2023-06-01'
            assert article_row['filenames'] == ['章程.pdf']
            assert 'storage_key' not in json.dumps(documents)

            found, queries = _counting(database, lambda: _payload(session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': project_id, 'query': '注册资本'}}, 11)))
            hits = {row['text']: row for row in found['results']}
            assert set(hits) >= {'注册资本章程甲。', '注册资本未填主体。', '注册资本办公说明。'}
            _assert_material_queries(queries, found['results'])

            pdf = hits['注册资本章程甲。']
            _assert_old_hit(pdf, project_id, articles, '章程.pdf', 'page')
            assert pdf['page_number'] == 1
            assert pdf['category'] == 'articles'
            assert pdf['subject_id'] == subject_id
            assert pdf['subject_name'] == '甲公司'
            assert pdf['subject_role'] == 'target'
            assert pdf['date_kind'] == 'point'
            assert pdf['point_date'] == '2023-06-01'
            assert pdf['period_start'] is None and pdf['period_end'] is None
            assert pdf['index_version'] == page_version
            assert pdf['source_revision'] == revision
            for key in OFFICE_FIELDS:
                assert key not in pdf

            plain_hit = hits['注册资本未填主体。']
            _assert_old_hit(plain_hit, project_id, plain, '未填.pdf', 'page')
            assert plain_hit['category'] == 'uncategorized'
            assert plain_hit['date_kind'] == 'none'
            assert plain_hit['subject_id'] is None
            assert plain_hit['subject_name'] is None
            assert plain_hit['subject_role'] is None
            assert plain_hit['point_date'] is None
            assert plain_hit['period_start'] is None
            assert plain_hit['period_end'] is None
            for key in MATERIAL_FIELDS:
                assert key in plain_hit
            assert plain_hit['index_version'] == page_version
            assert plain_hit['source_revision']
            for key in OFFICE_FIELDS:
                assert key not in plain_hit

            office_hit = hits['注册资本办公说明。']
            _assert_old_hit(office_hit, project_id, office_id, '说明.docx', 'source_unit')
            assert office_hit['category'] == 'financial_statement'
            assert office_hit['subject_id'] == subject_id
            assert office_hit['subject_name'] == '甲公司'
            assert office_hit['subject_role'] == 'target'
            assert office_hit['date_kind'] == 'period'
            assert office_hit['point_date'] is None
            assert office_hit['period_start'] == '2023-01-01'
            assert office_hit['period_end'] == '2023-12-31'
            assert office_hit['locator'] == {'kind': 'docx_paragraph', 'paragraph': 1}
            assert office_hit['locator_label']
            assert 'PDF 第' not in office_hit['locator_label']
            assert office_hit['unit_id']
            assert office_hit['formula_state'] == ''
            assert office_hit['index_version'] == office_version
            assert 'source_revision' not in office_hit
            for key in (*MATERIAL_FIELDS, *OFFICE_FIELDS):
                assert key in office_hit

            read = _payload(session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'evidence_id': pdf['chunk_id'], 'limit': 100}}, 12))
            assert read['text'] == '注册资本章程甲。'
            assert read['evidence_id'] == pdf['chunk_id']
            assert read['project_id'] == project_id
            assert read['document_id'] == articles
            assert read['filenames'] == ['章程.pdf']
            assert read['index_kind'] == 'page'
            assert read['index_version'] == page_version
            assert read['source_revision'] == revision
            assert read['locator']['kind'] == 'pdf' and read['locator']['page'] == 1
            assert read['locator_label'] == 'PDF 第 1 页'
            _assert_material(read, 'articles', subject_id, '甲公司', 'target', 'point', '2023-06-01', None, None)

            ranged = _payload(session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'document_id': articles, 'page_from': 1, 'page_to': 1, 'limit': 4000}}, 13))
            assert ranged['items'][0]['text'] == '注册资本章程甲。'
            assert ranged['items'][0]['source_revision'] == revision
            assert ranged['items'][0]['page_number'] == 1
            assert 'category' not in ranged['items'][0]
            _assert_material(ranged, 'articles', subject_id, '甲公司', 'target', 'point', '2023-06-01', None, None)

            plain_read = _payload(session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'evidence_id': plain_hit['chunk_id']}}, 14))
            assert plain_read['text'] == '注册资本未填主体。'
            assert plain_read['index_kind'] == 'page'
            assert plain_read['source_revision']
            _assert_material(plain_read, 'uncategorized', None, None, None, 'none', None, None, None)

            office_read = _payload(session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'evidence_id': office_hit['chunk_id']}}, 15))
            assert '注册资本办公说明' in office_read['text']
            assert office_read['index_kind'] == 'source_unit'
            assert office_read['index_version'] == office_version
            assert office_read['locator']['kind'] == 'docx_paragraph'
            assert office_read['formula_state'] == ''
            assert 'PDF 第' not in office_read['locator_label']
            _assert_material(office_read, 'financial_statement', subject_id, '甲公司', 'target', 'period', None, '2023-01-01', '2023-12-31')

            office_range = _payload(session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': project_id, 'document_id': office_id, 'limit': 4000}}, 16))
            assert any(item.get('unit_id') and item['locator']['kind'] == 'docx_paragraph' for item in office_range['items'])
            assert 'category' not in office_range['items'][0]
            _assert_material(office_range, 'financial_statement', subject_id, '甲公司', 'target', 'period', None, '2023-01-01', '2023-12-31')
            denied = session.call('tools/call', {'name': 'read_evidence', 'arguments': {
                'project_id': other_id, 'evidence_id': pdf['chunk_id']}}, 17)
            assert denied['result']['isError'] is True
            assert '甲公司' not in denied['result']['content'][0]['text']
            assert '注册资本章程甲' not in denied['result']['content'][0]['text']
        finally:
            session.close()

    stream_app = _mcp(database_url, tmp_path, service)
    with TestClient(stream_app) as http:
        opened = _open(http, '2025-06-18')
        sid = opened['session']
        listed = _stream_payload(http, sid, 'list_subjects', {'project_id': project_id}, 2)
        assert {row['name']: row['subject_id'] for row in listed['subjects']} == {'甲公司': subject_id, '未绑定主体': unbound_id}
        assert '乙公司' not in json.dumps(listed, ensure_ascii=False)
        found = _stream_payload(http, sid, 'search_evidence', {'project_id': project_id, 'query': '注册资本'}, 3)
        hits = {row['text']: row for row in found['results']}
        assert hits['注册资本章程甲。']['subject_name'] == '甲公司'
        assert hits['注册资本章程甲。']['source_revision'] == revision
        assert hits['注册资本章程甲。']['index_version'] == page_version
        assert 'locator' not in hits['注册资本章程甲。']
        assert hits['注册资本办公说明。']['locator']['kind'] == 'docx_paragraph'
        assert hits['注册资本办公说明。']['unit_id']
        assert hits['注册资本办公说明。']['formula_state'] == ''
        assert hits['注册资本办公说明。']['index_version'] == office_version
        assert hits['注册资本办公说明。']['subject_role'] == 'target'
        assert 'source_revision' not in hits['注册资本办公说明。']
        read = _stream_payload(http, sid, 'read_evidence', {
            'project_id': project_id, 'evidence_id': hits['注册资本章程甲。']['chunk_id'], 'limit': 80}, 4)
        assert read['text'] == '注册资本章程甲。'
        assert read['source_revision'] == revision
        _assert_material(read, 'articles', subject_id, '甲公司', 'target', 'point', '2023-06-01', None, None)


def _meta(category, subject, kind, point=None, start=None, end=None):
    return {
        'category': category, 'subject_id': subject, 'date_kind': kind,
        'point_date': point, 'period_start': start, 'period_end': end,
    }


def _docx(paragraph):
    document = Document()
    document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _index(database, service, project_id, document_id, text):
    database.set_reading(document_id, 1, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(
        page_number=1, status='succeeded', method='text', text=text))
    outcome = service.index(project_id, document_id, [1])
    assert outcome['failed_pages'] == [], outcome


def _mcp(database_url, tmp_path, service):
    from dd_agent.mcp_server import create_mcp_app
    app = create_mcp_app(Settings(database_url=database_url, data_dir=tmp_path, mcp_api_key=KEY))
    app.state.evidence = service
    return app


def _assert_old_hit(hit, project_id, document_id, filename, index_kind):
    assert hit['project_id'] == project_id
    assert hit['document_id'] == document_id
    assert hit['chunk_id']
    assert hit['filenames'] == [filename]
    assert hit['index_kind'] == index_kind
    assert 'page_number' in hit and 'start_offset' in hit and 'end_offset' in hit
    assert isinstance(hit['source_spans'], list)
    assert hit['recall']['fusion'] == 'rrf'
    assert hit['recall']['channels']
    assert hit['recall']['fusion_score'] > 0
    assert 'storage_key' not in json.dumps(hit)


def _assert_material(payload, category, subject_id, subject_name, subject_role, date_kind, point, start, end):
    assert payload['category'] == category
    assert payload['subject_id'] == subject_id
    assert payload['subject_name'] == subject_name
    assert payload['subject_role'] == subject_role
    assert payload['date_kind'] == date_kind
    assert payload['point_date'] == point
    assert payload['period_start'] == start
    assert payload['period_end'] == end
    for key in MATERIAL_FIELDS:
        assert key in payload


def _stream_payload(http, session_id, name, arguments, message_id):
    response = http.post('/mcp', headers=_headers(session=session_id, protocol='2025-06-18'), json=_rpc('tools/call', {
        'name': name, 'arguments': arguments}, message_id))
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result.get('isError') is not True, result
    return json.loads(result['content'][0]['text'])


def _counting(database, function):
    original = database.connect
    queries = []

    class Proxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, query, params=None, *args, **kwargs):
            queries.append(' '.join(str(query).lower().split()))
            if params is None:
                return self._conn.execute(query, *args, **kwargs)
            return self._conn.execute(query, params, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def wrapped():
        with original() as conn:
            yield Proxy(conn)

    database.connect = wrapped
    try:
        return function(), queries
    finally:
        database.connect = original


def _assert_material_queries(queries, results):
    material = [query for query in queries if 'subject_role' in query]
    pages = [query for query in queries if 'chunk_index_version' in query]
    offices = [query for query in queries if 'office_index_version' in query]
    assert len(material) == 1 and 'any(' in material[0]
    assert len(pages) == 1 and 'any(' in pages[0]
    assert len(offices) == 1 and 'any(' in offices[0]
    assert sum(row['index_kind'] == 'page' for row in results) >= 2
    assert sum(row['index_kind'] == 'source_unit' for row in results) >= 1
