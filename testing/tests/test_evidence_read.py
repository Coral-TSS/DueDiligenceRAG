"""按证据标识或页范围补读。过期不返回新正文，跨项目与不存在是同一句拒绝。"""
from io import BytesIO
from uuid import uuid4

from docx import Document

from dd_agent.config import Settings
from dd_agent.retrieval import EvidenceService
from dd_agent.storage import Storage
from test_retrieval import TinyModels
from test_workspace import PDF, application, client, project, upload


NEW_TEXT = '新正文不应在过期响应里出现。'
MARKER = '注册资本为2000万元。'


def _service(app, tmp_path, database_url):
    models = TinyModels()
    service = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models, database=app.state.database)
    app.state.evidence = service
    return service


def _chunk(database, document_id):
    with database.connect() as conn:
        return conn.execute(
            'SELECT id FROM dd_agent.evidence_chunks WHERE document_id = %s ORDER BY ordinal',
            (document_id,)).fetchone()['id']


def test_evidence_id_and_page_range_are_bounded_and_project_scoped(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    service = _service(app, tmp_path, database_url)
    with client(app) as http:
        project_id = project(http, '项目甲')
        other_id = project(http, '项目乙')
        document_id = upload(http, project_id).json()['document']['id']
        foreign_id = upload(http, other_id, '乙.pdf', PDF + b'\nB').json()['document']['id']
    database = app.state.database
    database.set_reading(document_id, 2, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text=MARKER + ('甲' * 30)))
    database.save_page(document_id, 'scan-v1', dict(page_number=2, status='succeeded', method='text', text='第二页住所。'))
    service.index(project_id, document_id, [1])
    database.set_reading(foreign_id, 1, 'scan-v1')
    database.save_page(foreign_id, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text='乙项目独有正文。'))
    service.index(other_id, foreign_id, [1])
    chunk_id = str(_chunk(database, document_id))
    foreign_chunk = str(_chunk(database, foreign_id))
    with client(app) as http:
        found = http.get(f'/api/projects/{project_id}/evidence/{chunk_id}?limit=10')
        assert found.status_code == 200, found.text
        body = found.json()
        assert body['text'] == (MARKER + ('甲' * 30))[:10]
        assert body['truncated'] is True and body['next_offset'] == 10
        assert body['filenames'] == ['合同.pdf']
        assert body['locator']['kind'] == 'pdf' and body['locator']['page'] == 1
        assert 'storage_key' not in found.text
        rest = http.get(f'/api/projects/{project_id}/evidence/{chunk_id}?limit=80&offset={body["next_offset"]}')
        assert body['text'] + rest.json()['text'] == MARKER + ('甲' * 30)
        denied = http.get(f'/api/projects/{other_id}/evidence/{chunk_id}')
        missing = http.get(f'/api/projects/{project_id}/evidence/{uuid4()}')
        assert denied.status_code == missing.status_code == 404
        assert denied.json()['detail'] == missing.json()['detail']
        assert MARKER not in denied.text and '乙项目独有正文' not in denied.text
        assert '存在于' not in denied.text
        swapped = http.get(f'/api/projects/{project_id}/evidence/{foreign_chunk}')
        assert swapped.status_code == 404
        assert '乙项目独有正文' not in swapped.text
        ranged = http.get(f'/api/projects/{project_id}/documents/{document_id}/evidence-range?page_from=1&page_to=2&limit=12')
        assert ranged.status_code == 200, ranged.text
        assert ranged.json()['limit'] == 12
        assert ranged.json()['next']['page_number'] == 1
        assert ranged.json()['truncated'] is True
        assert len(ranged.json()['items'][0]['text']) == 12
        continued = http.get(
            f'/api/projects/{project_id}/documents/{document_id}/evidence-range'
            f'?page_from=1&page_to=2&resume_page={ranged.json()["next"]["page_number"]}'
            f'&offset={ranged.json()["next"]["offset"]}&limit=4000'
            f'&source_revision={ranged.json()["items"][0]["source_revision"]}')
        assert continued.status_code == 200, continued.text
        assert any(item['page_number'] == 2 and '第二页住所' in item['text'] for item in continued.json()['items'])
        wrong_doc = http.get(f'/api/projects/{other_id}/documents/{document_id}/evidence-range?page_from=1&page_to=1')
        absent = http.get(f'/api/projects/{project_id}/documents/{uuid4()}/evidence-range?page_from=1&page_to=1')
        assert wrong_doc.status_code == absent.status_code == 404
        assert wrong_doc.json()['detail'] == absent.json()['detail']
        assert MARKER not in wrong_doc.text


def test_stale_reference_does_not_return_the_new_text(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    service = _service(app, tmp_path, database_url)
    with client(app) as http:
        project_id = project(http)
        document_id = upload(http, project_id).json()['document']['id']
    database = app.state.database
    database.set_reading(document_id, 1, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text=MARKER))
    service.index(project_id, document_id, [1])
    chunk_id = _chunk(database, document_id)
    with client(app) as http:
        current = http.get(f'/api/projects/{project_id}/documents/{document_id}/evidence-range?page_from=1&page_to=1')
        revision = current.json()['items'][0]['source_revision']
        database.save_page(document_id, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text=NEW_TEXT))
        stale_id = http.get(f'/api/projects/{project_id}/evidence/{chunk_id}')
        stale_range = http.get(
            f'/api/projects/{project_id}/documents/{document_id}/evidence-range?page_from=1&page_to=1&source_revision={revision}')
    assert stale_id.status_code == 409
    assert stale_range.status_code == 409
    assert NEW_TEXT not in stale_id.text and NEW_TEXT not in stale_range.text
    assert stale_id.json()['code'] == 'stale' and 'text' not in stale_id.json()


def test_missing_file_and_unread_page_are_distinct(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    service = _service(app, tmp_path, database_url)
    with client(app) as http:
        project_id = project(http)
        document_id = upload(http, project_id).json()['document']['id']
    database = app.state.database
    database.set_reading(document_id, 2, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text=MARKER))
    service.index(project_id, document_id, [1])
    chunk_id = _chunk(database, document_id)
    key = database.document(project_id, document_id)['storage_key']
    Storage(tmp_path, 1024).path(key).unlink()
    with client(app) as http:
        missing = http.get(f'/api/projects/{project_id}/evidence/{chunk_id}')
    assert missing.status_code == 404
    assert missing.json()['code'] == 'missing_artifact'
    assert MARKER not in missing.text
    with client(app) as http:
        other = project(http, '仍有原件')
        kept = upload(http, other, '仍在.pdf', PDF + b'\nkept').json()['document']['id']
    database.set_reading(kept, 2, 'scan-v1')
    database.save_page(kept, 'scan-v1', dict(page_number=1, status='succeeded', method='text', text='仍在的第一页。'))
    with client(app) as http:
        gap = http.get(f'/api/projects/{other}/documents/{kept}/evidence-range?page_from=1&page_to=2&limit=4000')
    assert gap.status_code == 200, gap.text
    assert gap.json()['items'][1]['status'] == 'not_processed'
    assert gap.json()['items'][1]['text'] == ''
    assert gap.json()['items'][1]['recognition'] == 'unprocessed'


def test_office_evidence_keeps_its_locator_and_rejects_the_other_project(tmp_path, database_url):
    document = Document()
    document.add_paragraph('甲公司章程约定注册资本。')
    buffer = BytesIO()
    document.save(buffer)
    app = application(tmp_path, database_url, migrate=True, limit=2_000_000)
    service = _service(app, tmp_path, database_url)
    with client(app) as http:
        project_id = project(http, '项目甲')
        other_id = project(http, '项目乙')
        uploaded = http.post(
            f'/api/projects/{project_id}/documents',
            files={'file': ('章程.docx', buffer.getvalue(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()['document']['id']
    service.index_office(project_id, document_id)
    with app.state.database.connect() as conn:
        evidence_id = conn.execute(
            '''SELECT id FROM dd_agent.source_unit_chunks WHERE document_id = %s AND text LIKE %s''',
            (document_id, '%章程约定注册资本%')).fetchone()['id']
    with client(app) as http:
        found = http.get(f'/api/projects/{project_id}/evidence/{evidence_id}')
        denied = http.get(f'/api/projects/{other_id}/evidence/{evidence_id}')
        ranged = http.get(f'/api/projects/{project_id}/documents/{document_id}/evidence-range?limit=4000')
    assert found.status_code == 200, found.text
    body = found.json()
    assert body['index_kind'] == 'source_unit'
    assert body['locator']['kind'] == 'docx_paragraph'
    assert 'PDF 第' not in body['locator_label']
    assert '甲公司章程约定注册资本' in body['text']
    assert denied.status_code == 404
    assert '章程约定注册资本' not in denied.text
    assert denied.json()['detail'] == '证据不存在或不属于当前项目。'
    assert ranged.status_code == 200, ranged.text
    assert any(item['locator']['kind'] == 'docx_paragraph' for item in ranged.json()['items'])
    assert 'PDF 第' not in ranged.text
