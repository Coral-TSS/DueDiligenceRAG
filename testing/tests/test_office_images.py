"""Word 嵌入图必须识别成真实文字；材料只有向量齐全才算已处理。"""
import base64
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4
import zipfile

from docx import Document
from psycopg.types.json import Jsonb

from dd_agent.office import PARSE_VERSION, terminal_office_outcome
from test_legacy_office import _app as legacy_app
from test_legacy_office import _fake
from test_office_formats import _app, _service
from test_retrieval import TinyModels
from test_workspace import client, project


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
PLACEHOLDER = '此处有图、未提取'
STATIC = Path(__file__).resolve().parents[2] / 'src' / 'dd_agent' / 'static'


def _docx_with_pictures():
    document = Document()
    document.add_paragraph('甲公司章程约定注册资本。')
    document.add_picture(BytesIO(PNG))
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.paragraphs[0].add_run().add_picture(BytesIO(PNG))
    document.sections[0].header.paragraphs[0].add_run().add_picture(BytesIO(PNG))
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _empty_docx():
    document = Document()
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _recognizer(image):
    assert image['bytes'].startswith(b'\x89PNG')
    part = image['locator']['part']
    ordinal = image['locator']['ordinal']
    if part == 'document' and ordinal == 1:
        return {'ok': True, 'text': '正文图片附言资本'}
    if part == 'document':
        return {'ok': True, 'text': '表格图片附言资本'}
    if part.startswith('header'):
        return {'ok': True, 'text': '页眉图片附言资本'}
    return {'ok': False, 'error_code': 'image_ocr_failed'}


def _bind(app, models=None):
    models = models or TinyModels()
    app.state.pipeline.evidence.models = models
    app.state.evidence.models = models
    app.state.jobs.wake = lambda: None
    return models


def _upload(http, project_id, name, body):
    uploaded = http.post(
        f'/api/projects/{project_id}/documents',
        files={'file': (name, body, 'application/octet-stream')})
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()['document']['id']


def _catalog(http, project_id, document_id):
    listed = http.get(f'/api/projects/{project_id}/material-catalog', params={'limit': 100})
    assert listed.status_code == 200, listed.text
    return next(item for item in listed.json()['items'] if item['document_id'] == document_id)


def _run(app, project_id, document_id):
    job = app.state.jobs.enqueue(project_id, document_id, wake=False)
    app.state.jobs._run_job(job)
    return app.state.jobs.latest(project_id, document_id)


def test_terminal_outcome_requires_every_required_vector():
    assert terminal_office_outcome(unit_failed=0, image_failed=0, indexable=2, indexed=2, version_known=True) == 'complete'
    assert terminal_office_outcome(unit_failed=0, image_failed=0, indexable=2, indexed=1, version_known=True) == 'indexing_incomplete'
    assert terminal_office_outcome(unit_failed=0, image_failed=0, indexable=2, indexed=2, version_known=False) == 'indexing_incomplete'
    assert terminal_office_outcome(unit_failed=1, image_failed=1, indexable=1, indexed=1, version_known=True) == 'partial'
    assert terminal_office_outcome(unit_failed=1, image_failed=1, indexable=0, indexed=0, version_known=True) == 'image_failed'
    assert terminal_office_outcome(unit_failed=0, image_failed=0, indexable=0, indexed=0, version_known=True) == 'empty'
    assert terminal_office_outcome(unit_failed=1, image_failed=0, indexable=0, indexed=0, version_known=True) == 'failed'


def test_extracts_inline_anchor_table_header_and_skips_fallback_duplicate():
    from dd_agent.docx_images import extract_docx_images
    png = PNG
    document = '''<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
      xmlns:v="urn:schemas-microsoft-com:vml"
      xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">
      <w:body>
        <w:p><w:r><w:drawing><wp:inline><a:blip r:embed="rIdInline"/></wp:inline></w:drawing></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:drawing><wp:inline><a:blip r:embed="rIdTable"/></wp:inline></w:drawing></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:r><w:drawing><wp:anchor><a:blip r:embed="rIdAnchor"/></wp:anchor></w:drawing></w:r></w:p>
        <mc:AlternateContent>
          <mc:Choice><w:drawing><wp:inline><a:blip r:embed="rIdChoice"/></wp:inline></w:drawing></mc:Choice>
          <mc:Fallback><w:pict><v:imagedata r:id="rIdFallback"/></w:pict></mc:Fallback>
        </mc:AlternateContent>
        <w:p><w:r><w:drawing><wp:inline><a:blip r:embed="rIdMissing"/></wp:inline></w:drawing></w:r></w:p>
      </w:body>
    </w:document>'''
    header = '''<?xml version="1.0" encoding="UTF-8"?>
    <w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
      <w:p><w:r><w:drawing><wp:anchor><a:blip r:embed="rIdHeader"/></wp:anchor></w:drawing></w:r></w:p>
    </w:hdr>'''
    rels = '''<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdInline" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/inline.png"/>
      <Relationship Id="rIdTable" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/table.png"/>
      <Relationship Id="rIdAnchor" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/anchor.png"/>
      <Relationship Id="rIdChoice" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/choice.png"/>
      <Relationship Id="rIdFallback" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/fallback.png"/>
      <Relationship Id="rIdMissing" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/missing.png"/>
    </Relationships>'''
    header_rels = '''<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rIdHeader" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/header.png"/>
    </Relationships>'''
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as package:
        package.writestr('word/document.xml', document)
        package.writestr('word/_rels/document.xml.rels', rels)
        package.writestr('word/header1.xml', header)
        package.writestr('word/_rels/header1.xml.rels', header_rels)
        for name in ('inline.png', 'table.png', 'anchor.png', 'choice.png', 'fallback.png', 'header.png'):
            package.writestr(f'word/media/{name}', png)
    images = extract_docx_images(buffer.getvalue())
    assert [(item['locator']['part'], item['locator']['ordinal'], item['locator']['placement'], item['error_code']) for item in images] == [
        ('document', 1, 'inline', ''),
        ('document', 2, 'inline', ''),
        ('document', 3, 'anchor', ''),
        ('document', 4, 'inline', ''),
        ('document', 5, 'inline', 'image_extract_failed'),
        ('header1', 1, 'anchor', ''),
    ]
    assert images[0]['bytes'] == png
    assert images[4]['bytes'] is None
    assert all(item['bytes'] != b'fallback-only' for item in images)


def test_embedded_word_image_is_recognized_and_indexed(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    models = _bind(app)
    body = _docx_with_pictures()
    with client(app) as http:
        project_id = project(http)
        document_id = _upload(http, project_id, '附言.docx', body)
    _seed_placeholder(app, document_id)
    app.state.pipeline.evidence.image_recognizer = _recognizer
    finished = _run(app, project_id, document_id)
    assert finished['status'] == 'completed'
    assert finished['progress']['outcome'] == 'complete'
    service = _service(app, tmp_path, database_url, models)
    found = service.search(project_id, '正文图片附言')['results']
    assert any(hit['text'] == '正文图片附言资本' for hit in found)
    assert any(hit['text'] == '表格图片附言资本' for hit in service.search(project_id, '表格图片附言')['results'])
    assert any(hit['text'] == '页眉图片附言资本' for hit in service.search(project_id, '页眉图片附言')['results'])
    with app.state.database.connect() as conn:
        texts = [row['text'] for row in conn.execute(
            'SELECT text FROM dd_agent.source_units WHERE document_id=%s', (document_id,)).fetchall()]
        assert PLACEHOLDER not in texts
        assert '正文图片附言资本' in texts
    with client(app) as http:
        listed = _catalog(http, project_id, document_id)
    assert listed['office_outcome'] == 'complete'
    assert listed['reason']
    assert '已全部入库' in listed['reason']
    assert '不能视为已处理' not in listed['reason']


def test_ocr_or_extract_failure_is_not_complete(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    _bind(app)
    app.state.pipeline.evidence.image_recognizer = lambda image: {'ok': False, 'error_code': 'image_ocr_failed'}
    with client(app) as http:
        project_id = project(http)
        failed_id = _upload(http, project_id, '坏图.docx', _docx_with_pictures())
        waiting_doc = Document()
        waiting_doc.add_paragraph('尚未处理的另一份章程。')
        waiting_buf = BytesIO()
        waiting_doc.save(waiting_buf)
        placeholder_id = _upload(http, project_id, '尚未处理.docx', waiting_buf.getvalue())
    finished = _run(app, project_id, failed_id)
    assert finished['status'] != 'completed'
    assert finished['progress']['outcome'] == 'partial'
    assert finished['error_code'] == ''
    with app.state.database.connect() as conn:
        rows = conn.execute(
            '''SELECT status, error_code, text FROM dd_agent.source_units
               WHERE document_id=%s AND locator->>'kind'=%s''', (failed_id, 'docx_image')).fetchall()
        assert rows and all(row['status'] == 'failed' and row['text'] == '' for row in rows)
        assert conn.execute(
            'SELECT count(*) AS n FROM dd_agent.source_unit_chunks WHERE document_id=%s AND text=%s',
            (failed_id, PLACEHOLDER)).fetchone()['n'] == 0
    with client(app) as http:
        listed = _catalog(http, project_id, failed_id)
    assert listed['office_outcome'] == 'partial'
    assert '不能视为已处理' in listed['reason']
    assert '已全部入库' not in listed['reason']

    app.state.pipeline.evidence.image_recognizer = lambda image: {'ok': True, 'text': PLACEHOLDER}
    # 只有图片、没有正文时，占位文字也不能算识别成功。
    only_image = _picture_only_docx()
    with client(app) as http:
        only_id = _upload(http, project_id, '仅图.docx', only_image)
    only_job = _run(app, project_id, only_id)
    assert only_job['status'] != 'completed'
    assert only_job['progress']['outcome'] == 'image_failed'
    with client(app) as http:
        only_listed = _catalog(http, project_id, only_id)
    assert only_listed['office_outcome'] == 'image_failed'
    assert '图片未能识别' in only_listed['reason']

    missing = _missing_image_docx()
    app.state.pipeline.evidence.image_recognizer = lambda image: {'ok': True, 'text': '不该被调用'}
    with client(app) as http:
        missing_id = _upload(http, project_id, '缺图.docx', missing)
    missing_job = _run(app, project_id, missing_id)
    assert missing_job['progress']['outcome'] == 'image_failed'
    assert missing_job['status'] != 'completed'
    # placeholder_id 仍停在登记，不能因为别的材料失败就被标成已处理。
    with client(app) as http:
        waiting = _catalog(http, project_id, placeholder_id)
    assert waiting['office_outcome'] == 'registered'


def test_document_is_fully_processed_only_when_all_vectors_exist(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    models = _bind(app)

    def fail_second(texts, query=False):
        if not query and any('失败单元' in text for text in texts):
            raise RuntimeError('嵌入失败')
        return TinyModels.embed(models, texts, query=query)

    models.embed = fail_second
    document = Document()
    document.add_paragraph('第一段保留资本。')
    # 标题单独成单元，嵌入失败才不会把前一段一起丢掉。
    document.add_heading('失败单元不要入库。', level=1)
    buffer = BytesIO()
    document.save(buffer)
    with client(app) as http:
        project_id = project(http)
        document_id = _upload(http, project_id, '向量.docx', buffer.getvalue())
    first = _run(app, project_id, document_id)
    assert first['status'] == 'partial'
    assert first['progress']['outcome'] == 'partial'
    with client(app) as http:
        partial = _catalog(http, project_id, document_id)
    assert partial['office_outcome'] == 'partial'
    assert '已全部入库' not in partial['reason']

    models.embed = TinyModels.embed.__get__(models, TinyModels)
    second = _run(app, project_id, document_id)
    assert second['status'] == 'completed'
    assert second['progress']['outcome'] == 'complete'
    with client(app) as http:
        done = _catalog(http, project_id, document_id)
    assert done['office_outcome'] == 'complete'
    assert '已全部入库' in done['reason']

    with app.state.database.connect() as conn:
        conn.execute('DELETE FROM dd_agent.source_unit_chunks WHERE document_id=%s AND text=%s', (document_id, '失败单元不要入库。'))
    with client(app) as http:
        incomplete = _catalog(http, project_id, document_id)
    assert incomplete['office_outcome'] == 'indexing_incomplete'
    assert '不能视为已处理' in incomplete['reason']
    assert '已全部入库' not in incomplete['reason']


def test_empty_parse_is_not_fully_processed(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    _bind(app)
    with client(app) as http:
        project_id = project(http)
        document_id = _upload(http, project_id, '空白.docx', _empty_docx())
    finished = _run(app, project_id, document_id)
    assert finished['status'] != 'completed'
    assert finished['progress']['outcome'] == 'empty'
    assert finished['progress']['indexed_units'] == []
    assert finished['progress']['failed_units'] == []
    assert finished['error_code'] == 'empty_extract'
    saved = app.state.database.document(project_id, document_id)
    assert '不能视为已处理' in saved['reason']
    assert '没有提取到' in saved['reason']
    with app.state.database.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.source_units WHERE document_id=%s', (document_id,)).fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.source_unit_chunks WHERE document_id=%s', (document_id,)).fetchone()['n'] == 0
    with client(app) as http:
        listed = _catalog(http, project_id, document_id)
    assert listed['office_outcome'] == 'empty'
    assert '不能视为已处理' in listed['reason']


def test_converted_doc_image_is_recognized_on_the_same_path(tmp_path, database_url, monkeypatch):
    from legacy_ole import word_document
    binary = _fake(tmp_path, 'success')
    app = legacy_app(tmp_path, database_url, soffice_path=binary, soffice_timeout_seconds=30)
    models = _bind(app)
    body = _docx_with_pictures()

    def converted(settings, source, ext):
        return {
            'ok': True, 'blob': body, 'target_extension': '.docx',
            'reason': '已用 LibreOffice 24.2.5.2 将旧版 Word（.doc）转为 docx 后解析。证据仍指向原件，转换中间文件已删除。',
            'conversion': {
                'source_extension': '.doc', 'target_extension': '.docx', 'tool': 'soffice',
                'converter': 'LibreOffice 24.2.5.2', 'outcome': 'converted',
            },
        }

    monkeypatch.setattr('dd_agent.legacy_office.convert_for_parse', converted)
    app.state.pipeline.evidence.image_recognizer = _recognizer
    with client(app) as http:
        project_id = project(http)
        uploaded = http.post(
            f'/api/projects/{project_id}/documents',
            files={'file': ('回单.doc', word_document(), 'application/octet-stream')})
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()['document']['id']
        assert uploaded.json()['document']['status'] == 'registered'
    finished = _run(app, project_id, document_id)
    assert finished['status'] == 'completed'
    assert finished['progress']['outcome'] == 'complete'
    assert finished['progress']['conversion']['source_extension'] == '.doc'
    saved = app.state.database.document(project_id, document_id)
    assert 'LibreOffice 24.2.5.2' in saved['reason']
    assert '.doc' in saved['reason']
    assert '已全部入库' in saved['reason']
    assert '图片文字已识别' in saved['reason']
    service = _service(app, tmp_path, database_url, models)
    hits = service.search(project_id, '正文图片附言')['results']
    assert any(hit['text'] == '正文图片附言资本' and hit['filenames'] == ['回单.doc'] for hit in hits)


def test_materials_page_uses_chinese_office_outcomes_without_changing_pdf_labels():
    script = (STATIC / 'app.js').read_text(encoding='utf-8')
    assert script.index('d.office_outcome') < script.index('job === "completed" ? "已处理"')
    for label in ('已登记 · 待解析', '正在转换', '正在解析', '正在识别图片', '正在建立向量',
                  '图片未能识别', '部分完成', '未提取到内容', '向量尚未完成', '已处理'):
        assert label in script
    assert 'complete: "已处理"' in script
    assert 'empty: "未提取到内容"' in script


def _picture_only_docx():
    document = Document()
    document.add_picture(BytesIO(PNG))
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _missing_image_docx():
    document = Document()
    document.add_picture(BytesIO(PNG))
    raw = BytesIO()
    document.save(raw)
    edited = BytesIO()
    with zipfile.ZipFile(BytesIO(raw.getvalue())) as source, zipfile.ZipFile(edited, 'w') as target:
        for info in source.infolist():
            if info.filename.startswith('word/media/'):
                continue
            target.writestr(info, source.read(info))
    return edited.getvalue()


def _seed_placeholder(app, document_id):
    locator = {'kind': 'docx_image', 'ordinal': 1}
    key = json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    vector = '[' + ','.join(['0'] * 1024) + ']'
    unit_id = uuid4()
    with app.state.database.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.source_units
            (id, document_id, locator, locator_key, text, formula_state, parse_version, status, error_code)
            VALUES (%s,%s,%s,%s,%s,'',%s,'succeeded','')''',
            (unit_id, document_id, Jsonb(locator), key, PLACEHOLDER, PARSE_VERSION))
        conn.execute('''INSERT INTO dd_agent.source_unit_chunks
            (id, unit_id, document_id, index_version, ordinal, text, embedding, locator)
            VALUES (%s,%s,%s,%s,0,%s,%s::public.vector,%s)''',
            (uuid4(), unit_id, document_id, 'old-index', PLACEHOLDER, vector, Jsonb(locator)))
