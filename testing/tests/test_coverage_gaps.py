"""当前项目的覆盖缺口。不断言检索质量，也不把任务进度当成可检索页数。"""
from uuid import uuid4

from psycopg.types.json import Jsonb

from dd_agent.config import Settings
from dd_agent.retrieval import EvidenceService
from test_retrieval import TinyModels
from test_workspace import PDF, application, client, project, upload


SECRET_B = '乙项目缺口正文不应出现。'


def _bind(app, tmp_path, database_url, models):
    app.state.evidence = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models, database=app.state.database)
    return app.state.evidence


def _uncertain(database, document_id, page_number, text):
    current = database.page_row(document_id, page_number)
    with database.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.machine_pages
            (id, document_id, page_number, base_revision, profile_version, result)
            VALUES (%s, %s, %s, %s, %s, %s)''', (
                uuid4(), document_id, page_number, current['raw_revision'], 'synthetic',
                Jsonb({'text': text, 'verification': 'machine_uncertain'})))


def test_empty_project_is_not_complete_and_other_projects_stay_out(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    _bind(app, tmp_path, database_url, TinyModels())
    with client(app) as http:
        empty_id = project(http, '空项目')
        other_id = project(http, '项目乙')
        uploaded = upload(http, other_id, '乙.pdf', PDF + b'\nB')
        document_b = uploaded.json()['document']['id']
    database = app.state.database
    database.set_reading(document_b, 1, 'scan-v1')
    database.save_page(document_b, 'scan-v1', dict(
        page_number=1, status='failed', method='ocr', text=SECRET_B, error_code='synthetic'))
    calls = []
    app.state.evidence.search = lambda *args, **kwargs: calls.append('search')
    app.state.evidence.models.embed = lambda *args, **kwargs: calls.append('embed')
    with client(app) as http:
        response = http.get(f'/api/projects/{empty_id}/coverage')
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['project_id'] == empty_id
        assert body['complete'] is False
        assert body['items'] == []
        assert '不能相加' in body['definition']
        assert calls == []
        assert SECRET_B not in response.text
        assert 'processed_pages' not in body['counts']
        assert 'total' not in body
        missing = http.get(f'/api/projects/{uuid4()}/coverage')
        assert missing.status_code == 404
        assert SECRET_B not in missing.text


def test_lists_each_gap_kind_without_search_or_other_projects(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    service = _bind(app, tmp_path, database_url, TinyModels())
    with client(app) as http:
        project_id = project(http, '项目甲')
        other_id = project(http, '项目乙')
        failed = upload(http, project_id, '失败.pdf').json()['document']['id']
        blank = upload(http, project_id, '空白.pdf', PDF + b'\nblank').json()['document']['id']
        unknown = upload(http, project_id, '未知.pdf', PDF + b'\nunknown').json()['document']['id']
        stale = upload(http, project_id, '待重建.pdf', PDF + b'\nstale').json()['document']['id']
        unread = upload(http, project_id, '未预检.pdf', PDF + b'\nunread').json()['document']['id']
        ready = upload(http, project_id, '已索引.pdf', PDF + b'\nready').json()['document']['id']
        foreign = upload(http, other_id, '乙.pdf', PDF + b'\nforeign').json()['document']['id']
    database = app.state.database
    database.set_reading(failed, 3, 'scan-v1')
    database.save_page(failed, 'scan-v1', dict(page_number=1, status='failed', method='ocr', text='', error_code='synthetic'))
    database.save_page(failed, 'scan-v1', dict(page_number=2, status='failed', method='ocr', text='', error_code='synthetic'))
    database.set_reading(blank, 1, 'scan-v1')
    database.save_page(blank, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text='  \n'))
    database.set_reading(unknown, 1, 'scan-v1')
    database.save_page(unknown, 'scan-v1', dict(page_number=1, status='succeeded', method='ocr', text='视觉未能确认的句子。'))
    _uncertain(database, unknown, 1, '视觉未能确认的句子。')
    service.index(project_id, unknown, [1])
    database.set_reading(stale, 1, 'scan-v1')
    database.save_page(stale, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text='来源已变尚未重建。'))
    database.set_reading(ready, 1, 'scan-v1')
    database.save_page(ready, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text='注册资本为壹元。'))
    service.index(project_id, ready, [1])
    database.set_reading(foreign, 1, 'scan-v1')
    database.save_page(foreign, 'scan-v1', dict(page_number=1, status='failed', method='ocr', text=SECRET_B, error_code='synthetic'))
    with client(app) as http:
        response = http.get(f'/api/projects/{project_id}/coverage?limit=50')
        assert response.status_code == 200, response.text
        body = response.json()
        by_document = {}
        for item in body['items']:
            by_document.setdefault(item['document_id'], set()).update(item['kinds'])
        assert 'failed' in by_document[failed] and 'unprocessed' in by_document[failed]
        assert {'empty_text', 'pending_rebuild'} <= by_document[blank]
        assert 'unknown' in by_document[unknown] and 'pending_rebuild' not in by_document[unknown]
        assert 'pending_rebuild' in by_document[stale]
        assert by_document[unread] == {'unprocessed'}
        assert ready not in by_document
        assert body['complete'] is False
        assert body['counts']['known_pages'] == 7
        assert body['counts']['unknown_page_documents'] == 1
        for name in ('failed', 'unprocessed', 'unknown', 'empty_text', 'pending_rebuild'):
            assert body['counts'][name] >= 1
        assert SECRET_B not in response.text and 'storage_key' not in response.text
        assert 'processed_pages' not in body['counts']
        first = http.get(f'/api/projects/{project_id}/coverage?limit=1&kind=failed')
        second = http.get(f'/api/projects/{project_id}/coverage?limit=1&kind=failed&cursor={first.json()["next_cursor"]}')
    assert first.status_code == 200 and second.status_code == 200
    pages = {first.json()['items'][0]['page_number'], second.json()['items'][0]['page_number']}
    assert pages == {1, 2}
    assert all(item['document_id'] == failed for item in first.json()['items'] + second.json()['items'])
    assert SECRET_B not in first.text and SECRET_B not in second.text


def test_indexed_project_can_be_complete(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    service = _bind(app, tmp_path, database_url, TinyModels())
    with client(app) as http:
        project_id = project(http, '已覆盖')
        document_id = upload(http, project_id).json()['document']['id']
    database = app.state.database
    database.set_reading(document_id, 1, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text='注册资本为壹元。'))
    service.index(project_id, document_id, [1])
    with client(app) as http:
        body = http.get(f'/api/projects/{project_id}/coverage').json()
    assert body['complete'] is True
    assert body['items'] == []
    assert body['counts']['indexed_pages'] == 1
    assert body['counts']['known_pages'] == 1


def test_missing_fingerprint_does_not_start_a_model(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    with client(app) as http:
        project_id = project(http)
        document_id = upload(http, project_id).json()['document']['id']
    app.state.database.set_reading(document_id, 1, 'scan-v1')
    app.state.database.save_page(document_id, 'scan-v1', dict(
        page_number=1, status='succeeded', method='text', text='不应启动模型的正文。'))
    started = []
    models = app.state.evidence.models
    models.embed = lambda *args, **kwargs: started.append('embed')
    models.one = lambda *args, **kwargs: started.append('one')
    models.records = lambda *args, **kwargs: started.append('records')
    with client(app) as http:
        response = http.get(f'/api/projects/{project_id}/coverage')
    assert response.status_code == 200, response.text
    body = response.json()
    assert started == []
    assert body['index_version_known'] is False
    assert body['complete'] is False
    item = next(row for row in body['items'] if row['document_id'] == document_id and row['page_number'] == 1)
    assert 'pending_rebuild' in item['kinds']
    assert 'index_version_unknown' in item['reasons']
    assert '不应启动模型的正文' not in response.text
