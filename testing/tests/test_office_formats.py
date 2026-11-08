"""现代 Office 要能解析、定位并被当前项目检索。只登记成“暂不支持”不算完成。"""
from io import BytesIO
import zipfile

from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from test_retrieval import TinyModels
from test_workspace import application, client, project


def _app(tmp_path, database_url):
    return application(tmp_path, database_url, migrate=True, limit=2_000_000)


def _service(app, tmp_path, database_url, models=None):
    from dd_agent.config import Settings
    from dd_agent.retrieval import EvidenceService
    return EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models or TinyModels(), database=app.state.database)


def _docx_bytes():
    document = Document()
    document.add_paragraph('甲公司章程约定注册资本。')
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = '股东'
    table.rows[0].cells[1].text = '持股比例百分之二十'
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_paragraph_and_table_are_searchable_without_a_fake_pdf_page(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    with client(app) as http:
        project_id = project(http)
        uploaded = http.post(
            f'/api/projects/{project_id}/documents',
            files={'file': ('章程.docx', _docx_bytes(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        assert uploaded.status_code == 201, uploaded.text
        body = uploaded.json()
        assert body['document']['status'] == 'registered'
        document_id = body['document']['id']
    service = _service(app, tmp_path, database_url)
    # 处理队列可能还没跑完；测试直接调用与队列相同的入库入口，确认检索结果。
    service.index_office(project_id, document_id)
    result = service.search(project_id, '章程约定')
    assert result['results']
    hit = result['results'][0]
    assert hit['text'] == '甲公司章程约定注册资本。'
    assert hit['locator'] == {'kind': 'docx_paragraph', 'paragraph': 1}
    assert 'PDF 第' not in hit['locator_label']
    table_hits = service.search(project_id, '持股比例')['results']
    table_hit = next(hit for hit in table_hits if hit['text'] == '持股比例百分之二十')
    assert table_hit['locator'] == {'kind': 'docx_table_cell', 'table': 1, 'row': 1, 'column': 2, 'paragraph': 1}
    assert 'PDF 第' not in str(table_hit['locator_label'])
    with client(app) as http:
        other = project(http, '另一个项目')
        denied = http.get(hit['source_url'].replace(f'/api/projects/{project_id}/', f'/api/projects/{other}/'))
        assert denied.status_code == 404
        assert '甲公司章程' not in denied.text


def test_office_job_indexes_through_the_existing_queue(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    models = TinyModels()
    app.state.pipeline.evidence.models = models
    app.state.jobs.wake = lambda: None
    with client(app) as http:
        project_id = project(http)
        uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': ('章程.docx', _docx_bytes(), 'application/octet-stream')})
        document_id = uploaded.json()['document']['id']
        job = app.state.jobs.enqueue(project_id, document_id, wake=False)
    app.state.jobs._run_job(job)
    assert app.state.jobs.latest(project_id, document_id)['status'] == 'completed'
    found = _service(app, tmp_path, database_url, models).search(project_id, '章程约定')['results']
    assert any(hit['text'] == '甲公司章程约定注册资本。' for hit in found)


def _xlsx_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = '利润表'
    sheet['A1'] = '营业收入'
    sheet['B1'] = '=1+1'
    sheet['C1'] = '=9+9'
    buffer = BytesIO()
    book.save(buffer)
    raw = buffer.getvalue()
    edited = BytesIO()
    with zipfile.ZipFile(BytesIO(raw)) as source, zipfile.ZipFile(edited, 'w') as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == 'xl/worksheets/sheet1.xml':
                text = data.decode('utf-8')
                text = text.replace('<f>1+1</f>', '<f>1+1</f><v>2</v>', 1)
                data = text.encode('utf-8')
            target.writestr(info, data)
    return edited.getvalue()


def test_xlsx_uses_cached_values_and_does_not_invent_formula_results(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    with client(app) as http:
        project_id = project(http)
        uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': ('利润表.xlsx', _xlsx_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        document_id = uploaded.json()['document']['id']
    service = _service(app, tmp_path, database_url)
    service.index_office(project_id, document_id)
    revenue = next(hit for hit in service.search(project_id, '营业收入')['results'] if hit['text'] == '营业收入')
    assert revenue['locator'] == {'kind': 'xlsx_cell', 'sheet': '利润表', 'row': 1, 'column': 'A'}
    cached = next(hit for hit in service.search(project_id, '2')['results'] if hit['text'] == '2')
    assert cached['locator']['column'] == 'B'
    assert cached['formula_state'] == 'not_recalculated'
    assert '未重算' in cached['locator_label']
    assert all(hit['text'] not in {'=9+9', '18'} for hit in service.search(project_id, '9+9')['results'])


def test_pptx_slide_shape_paragraph_is_not_labeled_as_a_pdf_page(tmp_path, database_url):
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(0, 0, 100, 100)
    shape.text_frame.text = '第二页并购方案'
    buffer = BytesIO()
    deck.save(buffer)
    app = _app(tmp_path, database_url)
    with client(app) as http:
        project_id = project(http)
        uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': ('方案.pptx', buffer.getvalue(), 'application/vnd.openxmlformats-officedocument.presentationml.presentation')})
        document_id = uploaded.json()['document']['id']
    service = _service(app, tmp_path, database_url)
    service.index_office(project_id, document_id)
    hit = next(item for item in service.search(project_id, '并购方案')['results'] if item['text'] == '第二页并购方案')
    assert hit['locator'] == {'kind': 'pptx_paragraph', 'slide': 2, 'shape': 1, 'paragraph': 1}
    assert 'PDF 第' not in hit['locator_label']


def test_legacy_macro_and_encrypted_packages_do_not_become_evidence(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    encrypted = BytesIO()
    with zipfile.ZipFile(encrypted, 'w') as package:
        package.writestr('EncryptionInfo', 'locked')
        package.writestr('EncryptedPackage', '密文甲公司注册资本')
    macro = BytesIO()
    with zipfile.ZipFile(macro, 'w') as package:
        package.writestr('word/document.xml', '<p>宏里的注册资本</p>')
        package.writestr('word/vbaProject.bin', 'macro')
    with client(app) as http:
        project_id = project(http)
        for name, body, marker in (
            ('旧件.doc', b'\xd0\xcf\x11\xe0doc', '旧版'),
            ('旧表.xls', b'\xd0\xcf\x11\xe0xls', '旧版'),
            ('旧演示.ppt', b'\xd0\xcf\x11\xe0ppt', '旧版'),
            ('加密.docx', encrypted.getvalue(), '密码'),
            ('宏.docx', macro.getvalue(), '宏'),
        ):
            uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': (name, body, 'application/octet-stream')})
            assert uploaded.status_code == 201, uploaded.text
            status = uploaded.json()['document']
            assert status['status'] != 'registered', name
            assert marker in status['reason'] or '不' in status['reason'] or '未' in status['reason']
            assert '密文甲公司' not in status['reason']
    with app.state.database.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.source_unit_chunks').fetchone()['n'] == 0


def test_repeat_upload_does_not_duplicate_vectors_and_failed_unit_can_retry(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    models = TinyModels()
    def fail_second(texts, query=False):
        if not query and any('失败单元' in text for text in texts):
            raise RuntimeError('嵌入失败')
        return TinyModels.embed(models, texts, query=query)
    models.embed = fail_second
    document = Document()
    document.add_paragraph('第一段保留。')
    document.add_paragraph('失败单元不要入库。')
    buffer = BytesIO()
    document.save(buffer)
    body = buffer.getvalue()
    with client(app) as http:
        project_id = project(http)
        first = http.post(f'/api/projects/{project_id}/documents', files={'file': ('重试.docx', body, 'application/octet-stream')})
        document_id = first.json()['document']['id']
        again = http.post(f'/api/projects/{project_id}/documents', files={'file': ('重试-副本.docx', body, 'application/octet-stream')})
        assert again.json()['duplicate'] is True
        assert again.json()['document']['id'] == document_id
    service = _service(app, tmp_path, database_url, models)
    service.index_office(project_id, document_id)
    kept = service.search(project_id, '第一段')['results']
    assert [hit['text'] for hit in kept] == ['第一段保留。']
    assert all('失败单元' not in hit['text'] for hit in service.search(project_id, '失败单元')['results'])
    models.embed = TinyModels.embed.__get__(models, TinyModels)
    service.index_office(project_id, document_id)
    assert any(hit['text'] == '失败单元不要入库。' for hit in service.search(project_id, '失败单元')['results'])
    with app.state.database.connect() as conn:
        count = conn.execute('SELECT count(*) AS n FROM dd_agent.source_unit_chunks WHERE document_id=%s', (document_id,)).fetchone()['n']
    assert count == 2
