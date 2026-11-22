"""材料元数据与检索范围过滤。合成材料，不读客户文件或真实模型。"""
from contextlib import contextmanager
from datetime import date
from io import BytesIO
import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from docx import Document
from psycopg.errors import CheckViolation
import pytest

from dd_agent.material_metadata import CATEGORIES, dates_overlap, parse_filters, validate_category, validate_date_fields
from dd_agent.mcp_server import TOOLS, ToolFailure, _call_tool
from dd_agent.office import OfficeIndex
from dd_agent.parent_queries import ParentQueries
from dd_agent.retrieval import valid_vector
from test_project_ui import _run
from test_retrieval import TinyModels
from test_workspace import PDF, application, client, project, upload


def _category_codes(sql):
    constraint = re.search(r'documents_category_known CHECK \(category IN \((.*?)\)\)', sql, re.S)
    assert constraint, sql
    return re.findall(r"'([a-z_]+)'", constraint.group(1))


def _ui_categories(path):
    text = Path(path).read_text(encoding='utf-8')
    block = text.split('const CATEGORIES = [', 1)[1].split('];', 1)[0]
    return re.findall(r'\["([a-z_]+)", "([^"]+)"\]', block)


LEGACY_CATEGORY_MAP = {
    'uncategorized': 'other',
    'business_license': 'incorporation',
    'articles': 'organizational_docs',
    'shareholder_resolution': 'organizational_docs',
    'board_resolution': 'organizational_docs',
    'equity_structure': 'shareholders',
    'audit_report': 'finance',
    'financial_statement': 'finance',
    'major_contract': 'major_contracts',
    'litigation': 'litigation_admin',
    'ip_certificate': 'ip',
    'regulatory_filing': 'other',
    'hr_labor': 'labor_hr',
    'interview_record': 'other',
    'other': 'other',
}


def test_confirmed_category_table_and_overlap_rules():
    original = Path('src/dd_agent/migrations/019_material_metadata.sql').read_text(encoding='utf-8')
    added = Path('src/dd_agent/migrations/020_interview_record.sql').read_text(encoding='utf-8')
    directories = Path('src/dd_agent/migrations/021_dd_directories.sql').read_text(encoding='utf-8')
    drop_uncategorized = Path('src/dd_agent/migrations/022_drop_uncategorized.sql').read_text(encoding='utf-8')
    health = Path('src/dd_agent/db.py').read_text(encoding='utf-8')
    assert 'uncategorized' not in CATEGORIES
    assert list(CATEGORIES)[0] == 'incorporation'
    assert list(CATEGORIES)[-1] == 'other'
    assert list(CATEGORIES) == [
        'incorporation', 'organizational_docs', 'shareholders', 'outbound_investment',
        'business', 'major_contracts', 'major_assets', 'ip', 'litigation_admin',
        'environmental', 'product_quality', 'labor_hr', 'finance', 'attachments',
        'subsidiary_aic', 'other',
    ]
    assert CATEGORIES['incorporation'] == '公司的设立、变更'
    assert CATEGORIES['shareholders'] == '股东及实际控制人'
    assert CATEGORIES['attachments'] == '附件'
    assert CATEGORIES['other'] == '其他'
    assert 'interview_record' not in CATEGORIES
    assert 'business_license' not in CATEGORIES
    assert 'business_license' in _category_codes(original)
    assert 'interview_record' in _category_codes(added)
    assert 'uncategorized' in _category_codes(directories)
    assert 'VALUES (21)' in directories
    assert _category_codes(drop_uncategorized) == list(CATEGORIES)
    assert 'uncategorized' not in _category_codes(drop_uncategorized)
    assert "WHEN 'uncategorized' THEN 'other'" in drop_uncategorized
    assert "ELSE 'other'" in drop_uncategorized
    assert 'VALUES (22)' in drop_uncategorized
    assert 'row["version"] != 22' in health
    assert _ui_categories('src/dd_agent/static/app.js') == list(CATEGORIES.items())
    assert _ui_categories('src/dd_agent/static/search.js') == list(CATEGORIES.items())
    # 022 只负责把 uncategorized 与残留并入 other；旧文书种类对照仍由 021 完成。
    for code in CATEGORIES:
        assert f"WHEN '{code}' THEN '{code}'" in drop_uncategorized
    assert parse_filters(categories=['attachments', 'organizational_docs']).categories == ('attachments', 'organizational_docs')
    assert validate_category('finance') == 'finance'
    try:
        validate_category('articles')
    except ValueError as exc:
        assert '类别' in str(exc)
    else:
        raise AssertionError('旧文书种类代码应拒绝')
    try:
        validate_category('interview_record')
    except ValueError as exc:
        assert '类别' in str(exc)
    else:
        raise AssertionError('已取消的访谈记录类别应拒绝')
    try:
        validate_category('uncategorized')
    except ValueError as exc:
        assert '类别' in str(exc)
        assert getattr(exc, 'status', 422) == 422
    else:
        raise AssertionError('已取消的未分类代码应拒绝')

    assert dates_overlap('period', None, date(2023, 1, 1), date(2023, 6, 30), period_start=date(2023, 1, 1), period_end=date(2023, 12, 31))
    assert dates_overlap('point', date(2023, 6, 1), None, None, period_start=date(2023, 1, 1), period_end=date(2023, 12, 31))
    assert not dates_overlap('period', None, date(2022, 1, 1), date(2022, 12, 31), period_start=date(2023, 1, 1), period_end=date(2023, 12, 31))
    assert not dates_overlap('none', None, None, None, date_point=date(2023, 6, 1))
    assert dates_overlap('point', date(2023, 6, 1), None, None, date_point=date(2023, 6, 1))
    assert not dates_overlap('point', date(2024, 1, 1), None, None, date_point=date(2023, 6, 1))
    try:
        validate_date_fields('period', '2023-06-01', '2023-01-01', '2023-12-31')
    except ValueError as exc:
        assert '时点与期间' in str(exc)
    else:
        raise AssertionError('时点与期间同时有值应拒绝')
    try:
        validate_date_fields('period', None, '2023-12-31', '2023-01-01')
    except ValueError as exc:
        assert '起日' in str(exc)
    else:
        raise AssertionError('起日晚于止日应拒绝')
    try:
        parse_filters(document_id='11111111-1111-1111-1111-111111111111', document_ids=['22222222-2222-2222-2222-222222222222'])
    except ValueError as exc:
        assert '422' not in str(exc) or '不能同时传入' in str(exc)
        assert '不能同时传入' in str(exc)
    else:
        raise AssertionError('document_id 与 document_ids 应拒绝')
    try:
        parse_filters(categories=[])
    except ValueError as exc:
        assert '空' in str(exc)
    else:
        raise AssertionError('空类别列表应拒绝')
    try:
        parse_filters(page_from=5, page_to=2)
    except ValueError as exc:
        assert '页范围' in str(exc)
    else:
        raise AssertionError('非法页范围应拒绝')
    try:
        parse_filters(categories=['uncategorized'])
    except ValueError as exc:
        assert '类别' in str(exc)
        assert getattr(exc, 'status', 422) == 422
    else:
        raise AssertionError('按未分类过滤应拒绝')
    try:
        parse_filters(categories=['uncategorized', 'organizational_docs'])
    except ValueError as exc:
        assert '类别' in str(exc)
        assert getattr(exc, 'status', 422) == 422
    else:
        raise AssertionError('夹带未分类的过滤应拒绝')
    assert parse_filters(categories=['other', 'organizational_docs']).categories == ('other', 'organizational_docs')


def test_workspace_and_search_forms_edit_metadata_and_filters():
    result = _run('material-metadata-regression.cjs')
    assert result.returncode == 0, result.stdout + result.stderr


def _service(app, tmp_path, database_url):
    from dd_agent.config import Settings
    from dd_agent.retrieval import EvidenceService
    models = TinyModels()
    models.cache_profile = lambda kind: models.version + '-' + kind
    service = EvidenceService(Settings(database_url=database_url, data_dir=tmp_path), models=models, database=app.state.database)
    app.state.evidence = service
    return service


def _index(app, service, project_id, document_id, texts):
    db = app.state.database
    db.set_reading(document_id, len(texts), 'scan-v1')
    for number, text in enumerate(texts, 1):
        db.save_page(document_id, 'scan-v1', dict(page_number=number, status='succeeded', method='text', text=text))
    outcome = service.index(project_id, document_id, list(range(1, len(texts) + 1)))
    assert outcome['failed_pages'] == [], outcome
    return document_id


def _docx(paragraph):
    document = Document()
    document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _body(category='other', subject=None, kind='none', point=None, start=None, end=None):
    return {'category': category, 'subject_id': subject, 'date_kind': kind, 'point_date': point, 'period_start': start, 'period_end': end}


def _ids(payload):
    return {row['document_id'] for row in payload['results']}


def _single_document_queries(database, function):
    original = database.connect
    counter = {'n': 0}
    pattern = re.compile(r'(^|[^a-z_])id\s*=\s*%s')

    class Proxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, query, params=None, *args, **kwargs):
            compact = ' '.join(str(query).lower().split())
            if 'dd_agent.documents' in compact and pattern.search(compact) and 'any(' not in compact:
                counter['n'] += 1
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
        return function(), counter['n']
    finally:
        database.connect = original


def test_metadata_filters_isolation_and_mcp(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True, limit=2_000_000)
    assert app.state.database.health()['status'] == 'ready'
    with app.state.database.connect() as conn:
        assert conn.execute('SELECT max(version) AS version FROM dd_agent.schema_version').fetchone()['version'] == 22
    service = _service(app, tmp_path, database_url)
    with client(app) as http:
        project_a = project(http, '项目甲')
        project_b = project(http, '项目乙')
        license_doc = upload(http, project_a, '营业执照.pdf', PDF + b'license').json()['document']
        assert license_doc['category'] == 'other' and license_doc['subject_id'] is None and license_doc['date_kind'] == 'none'
        catalog = http.get(f'/api/projects/{project_a}/material-catalog').json()['items'][0]
        assert catalog['category'] == 'other' and catalog['subject_name'] is None and catalog['date_kind'] == 'none'
        assert 'storage_key' not in catalog
        subject_a = http.post(f'/api/projects/{project_a}/subjects', json={'name': '  甲公司  ', 'role': 'target'})
        assert subject_a.status_code == 201, subject_a.text
        subject_a_id = subject_a.json()['id']
        assert subject_a.json()['name'] == '甲公司' and subject_a.json()['role'] == 'target'
        subject_c = http.post(f'/api/projects/{project_a}/subjects', json={'name': '子公司丙', 'role': 'subsidiary'}).json()['id']
        duplicate = http.post(f'/api/projects/{project_a}/subjects', json={'name': '甲公司'})
        assert duplicate.status_code == 409 and '同名' in duplicate.json()['detail'] and '乙公司' not in duplicate.text
        renamed = http.patch(f'/api/projects/{project_a}/subjects/{subject_a_id}', json={'name': '甲公司已改名'})
        assert renamed.status_code == 200 and renamed.json()['name'] == '甲公司已改名'
        assert http.patch(f'/api/projects/{project_a}/subjects/{subject_a_id}', json={'name': '甲公司'}).status_code == 200
        assert http.delete(f'/api/projects/{project_a}/subjects/{subject_a_id}').status_code == 405
        subject_b = http.post(f'/api/projects/{project_b}/subjects', json={'name': '乙公司', 'role': 'target'}).json()['id']
        assert {row['name'] for row in http.get(f'/api/projects/{project_a}/subjects').json()} == {'子公司丙', '甲公司'}
        assert {row['name'] for row in http.get(f'/api/projects/{project_b}/subjects').json()} == {'乙公司'}
        assert '甲公司' not in http.get(f'/api/projects/{project_b}/subjects').text

        def add(owner, name, body=None):
            payload = body if body is not None else PDF + name.encode()
            return upload(http, owner, name, payload).json()['document']['id']

        articles = add(project_a, '章程.pdf')
        audit = add(project_a, '审计.pdf')
        audit_old = add(project_a, '旧审计.pdf')
        other_subject = add(project_a, '丙审计.pdf')
        unfiled = add(project_a, '未归档.pdf')
        undated = add(project_a, '无日期章程.pdf')
        pages = add(project_a, '两页.pdf')
        narrow_a = add(project_a, '第一份.pdf')
        narrow_b = add(project_a, '第二份.pdf')
        narrow_c = add(project_a, '第三份.pdf')
        batch_a = add(project_a, '批量甲.pdf')
        batch_b = add(project_a, '批量乙.pdf')
        foreign = add(project_b, '乙章程.pdf')
        office = http.post(
            f'/api/projects/{project_a}/documents',
            files={'file': ('说明.docx', _docx('注册资本无页码单元。'), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
        )
        assert office.status_code == 201, office.text
        office_id = office.json()['document']['id']

        saved = http.patch(f'/api/projects/{project_a}/documents/{articles}/metadata', json=_body('organizational_docs', subject_a_id, 'point', '2023-06-01'))
        assert saved.status_code == 200, saved.text
        assert saved.json()['document']['category'] == 'organizational_docs'
        assert saved.json()['document']['subject_name'] == '甲公司'
        assert saved.json()['document']['point_date'] == '2023-06-01'
        assert http.patch(f'/api/projects/{project_a}/documents/{audit}/metadata', json=_body('finance', subject_a_id, 'period', start='2023-01-01', end='2023-06-30')).status_code == 200
        assert http.patch(f'/api/projects/{project_a}/documents/{audit_old}/metadata', json=_body('finance', subject_a_id, 'period', start='2022-01-01', end='2022-12-31')).status_code == 200
        assert http.patch(f'/api/projects/{project_a}/documents/{other_subject}/metadata', json=_body('finance', subject_c, 'period', start='2023-01-01', end='2023-12-31')).status_code == 200
        assert http.patch(f'/api/projects/{project_a}/documents/{undated}/metadata', json=_body('organizational_docs', subject_a_id)).status_code == 200
        assert http.patch(f'/api/projects/{project_b}/documents/{foreign}/metadata', json=_body('organizational_docs', subject_b, 'point', '2023-06-01')).status_code == 200
        for document_id, kind in ((batch_a, 'point'), (batch_b, 'period')):
            body = _body('major_contracts', subject_a_id, kind, '2020-01-02' if kind == 'point' else None, '2020-01-01' if kind == 'period' else None, '2020-01-31' if kind == 'period' else None)
            assert http.patch(f'/api/projects/{project_a}/documents/{document_id}/metadata', json=body).status_code == 200

        rejected = http.patch(f'/api/projects/{project_a}/documents/{unfiled}/metadata', json=_body('not_a_category'))
        assert rejected.status_code == 422 and '类别' in rejected.text
        assert app.state.database.document(project_a, unfiled)['category'] == 'other'
        assert http.patch(f'/api/projects/{project_a}/documents/{unfiled}/metadata', json=_body('organizational_docs', kind='period', start='2023-12-31', end='2023-01-01')).status_code == 422
        assert http.patch(f'/api/projects/{project_a}/documents/{unfiled}/metadata', json=_body('organizational_docs', kind='point', point='2023-06-01', start='2023-01-01', end='2023-12-31')).status_code == 422
        assert app.state.database.document(project_a, unfiled)['date_kind'] == 'none'

        stolen = http.patch(f'/api/projects/{project_b}/documents/{foreign}/metadata', json=_body('organizational_docs', subject_a_id))
        assert stolen.status_code == 404
        assert '甲公司' not in stolen.text and '乙公司' not in stolen.text
        assert str(app.state.database.document(project_b, foreign)['subject_id']) == subject_b
        mixed = http.post(f'/api/projects/{project_a}/documents/metadata', json={'document_ids': [batch_a, foreign], 'category': {'set': True, 'value': 'business'}})
        assert mixed.status_code == 404 and '甲公司' not in mixed.text
        assert app.state.database.document(project_a, batch_a)['category'] == 'major_contracts'
        batched = http.post(f'/api/projects/{project_a}/documents/metadata', json={'document_ids': [batch_a, batch_b], 'category': {'set': True, 'value': 'shareholders'}})
        assert batched.status_code == 200, batched.text
        for document_id in (batch_a, batch_b):
            row = app.state.database.document(project_a, document_id)
            assert row['category'] == 'shareholders'
            assert str(row['subject_id']) == subject_a_id
            assert row['date_kind'] in ('point', 'period')

    _index(app, service, project_a, articles, ['注册资本章程甲。'])
    _index(app, service, project_a, audit, ['注册资本审计甲。'])
    _index(app, service, project_a, audit_old, ['注册资本旧审计。'])
    _index(app, service, project_a, other_subject, ['注册资本丙审计。'])
    _index(app, service, project_a, unfiled, ['注册资本未归档。'])
    _index(app, service, project_a, undated, ['注册资本无日期。'])
    _index(app, service, project_a, pages, ['注册资本第一页。', '注册资本第二页。'])
    _index(app, service, project_a, narrow_a, ['注册资本只在第一份。'])
    # narrow_b 在材料范围内但没有可召回的切片，避免向量近邻把无关材料也带出来。
    _index(app, service, project_a, narrow_c, ['注册资本只在第三份。'])
    _index(app, service, project_b, foreign, ['注册资本只属于乙。'])
    service.index_office(project_a, office_id)
    before = _chunk_signature(app.state.database, articles)
    changed = None
    with client(app) as http:
        changed = http.patch(f'/api/projects/{project_a}/documents/{articles}/metadata', json=_body('business', subject_a_id, 'point', '2023-06-01'))
        assert changed.status_code == 200
    assert _chunk_signature(app.state.database, articles) == before
    # 改回章程，后面的类别过滤仍用这份材料。
    with client(app) as http:
        assert http.patch(f'/api/projects/{project_a}/documents/{articles}/metadata', json=_body('organizational_docs', subject_a_id, 'point', '2023-06-01')).status_code == 200
        listed = http.get(f'/api/projects/{project_a}/material-catalog', params={'categories': ['organizational_docs']}).json()
        assert {row['document_id'] for row in listed['items']} == {articles, undated}
        assert all(row['category'] == 'organizational_docs' for row in listed['items'])
        empty_filter = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'categories': []})
        assert empty_filter.status_code == 422
        bad_page = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'page_from': 5, 'page_to': 2})
        assert bad_page.status_code == 422
        both = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'document_id': articles, 'document_ids': [audit]})
        assert both.status_code == 422 and '不能同时传入' in both.text

        plain = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本'}).json()
        assert plain['status'] == 'candidates' and plain['results']
        assert 'excluded_uncategorized' not in plain
        narrowed = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'document_ids': [narrow_a, narrow_b]}).json()
        assert _ids(narrowed) == {narrow_a}
        assert narrow_c not in _ids(narrowed)
        paged = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'page_from': 2, 'page_to': 2}).json()
        assert paged['results'] and {row.get('page_number') for row in paged['results']} == {2}
        assert all(row.get('index_kind') != 'source_unit' for row in paged['results'])
        assert '无页码单元' not in json.dumps(paged, ensure_ascii=False) and '第一页' not in json.dumps(paged, ensure_ascii=False)
        by_category = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'categories': ['organizational_docs']}).json()
        assert _ids(by_category) <= {articles, undated} and articles in _ids(by_category)
        assert unfiled not in _ids(by_category) and by_category['excluded_uncategorized'] == 0
        rejected_uncategorized = http.post(f'/api/projects/{project_a}/search', json={
            'query': '注册资本', 'categories': ['organizational_docs', 'uncategorized']})
        assert rejected_uncategorized.status_code == 422 and '类别' in rejected_uncategorized.text
        including = http.post(f'/api/projects/{project_a}/search', json={
            'query': '注册资本', 'categories': ['organizational_docs', 'other'],
            'document_ids': [articles, undated, unfiled, audit]}).json()
        assert _ids(including) == {articles, undated, unfiled}
        by_subject = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'subject_ids': [subject_a_id]}).json()
        assert unfiled not in _ids(by_subject) and other_subject not in _ids(by_subject)
        assert by_subject['excluded_unsubjected'] > 0 and by_subject['results']
        by_date = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'period_start': '2023-01-01', 'period_end': '2023-12-31'}).json()
        assert {articles, audit, other_subject} <= _ids(by_date)
        assert audit_old not in _ids(by_date) and unfiled not in _ids(by_date)
        assert by_date['excluded_undated'] >= 1
        combined = http.post(f'/api/projects/{project_a}/search', json={
            'query': '注册资本', 'categories': ['finance'], 'subject_ids': [subject_a_id],
            'period_start': '2023-01-01', 'period_end': '2023-12-31'}).json()
        assert _ids(combined) == {audit}
        missing = http.post(f'/api/projects/{project_a}/search', json={'query': '注册资本', 'categories': ['ip']}).json()
        assert missing['status'] == 'no_matches' and missing['results'] == []
        assert '过滤后没有命中' in missing['message'] and '尚无' not in missing['message']
        assert missing['excluded_uncategorized'] == 0
        foreign_subject = http.post(f'/api/projects/{project_b}/search', json={'query': '注册资本', 'subject_ids': [subject_a_id]})
        assert foreign_subject.status_code >= 400
        assert '甲公司' not in foreign_subject.text and '注册资本章程甲' not in foreign_subject.text and '注册资本只属于乙' not in foreign_subject.text
        foreign_document = http.post(f'/api/projects/{project_b}/search', json={'query': '注册资本', 'document_ids': [articles]})
        assert foreign_document.status_code >= 400 and '注册资本章程甲' not in foreign_document.text
        own = http.post(f'/api/projects/{project_b}/search', json={'query': '注册资本'}).json()
        assert _ids(own) == {foreign}

    queries = []

    def capture(function):
        original = app.state.database.connect

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

        app.state.database.connect = wrapped
        try:
            return function()
        finally:
            app.state.database.connect = original

    scope = parse_filters(categories=['organizational_docs'], page_from=2, page_to=2)
    vector = valid_vector([1.0] + [0.0] * 1023)
    capture(lambda: ParentQueries(app.state.database).candidates(project_a, 'v' * 64, [1.0] + [0.0] * 1023, documents=[], scope=scope, limit=1))
    parent_sql = '\n'.join(queries)
    assert 'category' in parent_sql and 'page_number' in parent_sql and 'slide' not in parent_sql
    queries.clear()
    capture(lambda: OfficeIndex(service.settings, app.state.database, service.models).candidates(
        project_a, 'v' * 64, vector=vector, scope=scope, limit=1))
    office_sql = '\n'.join(queries)
    assert 'and false' in office_sql and 'slide' not in office_sql

    many = None
    with client(app) as http:
        many = upload(http, project_a, '多页.pdf', PDF + b'many').json()['document']['id']
    _index(app, service, project_a, many, ['注册资本多页之一。'])
    _, once = _single_document_queries(app.state.database, lambda: service.search(project_a, '注册资本', scope=parse_filters(document_ids=[many])))
    _index(app, service, project_a, many, [f'注册资本多页之{number}。' for number in range(1, 6)])
    found, repeated = _single_document_queries(app.state.database, lambda: service.search(project_a, '注册资本', scope=parse_filters(document_ids=[many])))
    assert len(found['results']) == 5
    assert once == repeated and repeated < 5

    tool_app = SimpleNamespace(state=SimpleNamespace(database=app.state.database, evidence=service))
    search_tool = next(tool for tool in TOOLS if tool['name'] == 'search_evidence')
    list_tool = next(tool for tool in TOOLS if tool['name'] == 'list_documents')
    assert search_tool['inputSchema']['additionalProperties'] is False
    assert search_tool['inputSchema']['required'] == ['project_id', 'query']
    assert 'document_id' in search_tool['inputSchema']['properties']
    assert 'document_ids' in search_tool['inputSchema']['properties']
    assert '过滤后无命中' in search_tool['description']
    assert list_tool['inputSchema']['required'] == ['project_id']

    def tool(name, arguments):
        try:
            result = _call_tool(tool_app, {'name': name, 'arguments': arguments})
        except ToolFailure as exc:
            return {'isError': True, 'content': [{'text': str(exc)}]}
        if result.get('isError'):
            return result
        return json.loads(result['content'][0]['text'])

    legacy = tool('search_evidence', {'project_id': project_b, 'query': '注册资本'})
    assert legacy['status'] == 'candidates' and {row['document_id'] for row in legacy['results']} == {foreign}
    single = tool('search_evidence', {'project_id': project_a, 'query': '注册资本', 'document_id': narrow_a})
    assert {row['document_id'] for row in single['results']} == {narrow_a}
    conflict = tool('search_evidence', {'project_id': project_a, 'query': '注册资本', 'document_id': narrow_a, 'document_ids': [narrow_c]})
    assert conflict['isError'] is True and '422' in conflict['content'][0]['text'] and '不能同时传入' in conflict['content'][0]['text']
    listed = tool('list_documents', {'project_id': project_a})
    row = next(item for item in listed['documents'] if item['id'] == articles)
    assert row['category'] == 'organizational_docs' and row['subject_name'] == '甲公司' and row['point_date'] == '2023-06-01'
    assert 'storage_key' not in json.dumps(listed)
    filtered = tool('list_documents', {'project_id': project_b, 'categories': ['organizational_docs']})
    assert {item['id'] for item in filtered['documents']} == {foreign}
    assert articles not in json.dumps(filtered)
    empty_project = tool('list_documents', {'project_id': str(uuid4())})
    assert empty_project['isError'] is True


def test_directory_category_saves_filters_and_returns_from_mcp(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True, limit=2_000_000)
    assert app.state.database.health()['status'] == 'ready'
    service = _service(app, tmp_path, database_url)
    with client(app) as http:
        project_id = project(http, '目录项目')
        attachment_id = upload(http, project_id, '附件.pdf', PDF + b'attachment').json()['document']['id']
        other_id = upload(http, project_id, '章程.pdf', PDF + b'articles').json()['document']['id']
        saved = http.patch(
            f'/api/projects/{project_id}/documents/{attachment_id}/metadata',
            json=_body('attachments'))
        assert saved.status_code == 200, saved.text
        assert saved.json()['document']['category'] == 'attachments'
        assert http.patch(
            f'/api/projects/{project_id}/documents/{other_id}/metadata',
            json=_body('organizational_docs')).status_code == 200
        catalog = http.get(
            f'/api/projects/{project_id}/material-catalog',
            params={'categories': ['attachments']}).json()
        assert {row['document_id'] for row in catalog['items']} == {attachment_id}
        assert catalog['items'][0]['category'] == 'attachments'
        assert catalog['category_counts']['attachments'] == 1
        assert catalog['category_counts']['organizational_docs'] == 1
        excluded = http.get(
            f'/api/projects/{project_id}/material-catalog',
            params={'categories': ['organizational_docs']}).json()
        assert {row['document_id'] for row in excluded['items']} == {other_id}
        filed = http.post(
            f'/api/projects/{project_id}/documents',
            files={'file': ('财务.pdf', PDF + b'finance-upload', 'application/pdf')},
            data={'category': 'finance'})
        assert filed.status_code == 201, filed.text
        finance_id = filed.json()['document']['id']
        assert filed.json()['document']['category'] == 'finance'
        again = http.post(
            f'/api/projects/{project_id}/documents',
            files={'file': ('财务副本.pdf', PDF + b'finance-upload', 'application/pdf')},
            data={'category': 'attachments'})
        assert again.status_code == 200
        assert again.json()['duplicate'] is True
        assert again.json()['document']['category'] == 'finance'
    with app.state.database.connect() as conn:
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conname = 'documents_category_known'"
        ).fetchone()['definition']
        assert 'attachments' in definition and 'organizational_docs' in definition and 'labor_hr' in definition
        assert 'interview_record' not in definition and 'business_license' not in definition and 'uncategorized' not in definition
    with pytest.raises(CheckViolation):
        with app.state.database.connect() as conn:
            conn.execute(
                'UPDATE dd_agent.documents SET category = %s WHERE id = %s',
                ('not_a_category', attachment_id))
    assert app.state.database.document(project_id, attachment_id)['category'] == 'attachments'

    _index(app, service, project_id, attachment_id, ['附件里的注册资本。'])
    _index(app, service, project_id, other_id, ['章程里的注册资本。'])
    with client(app) as http:
        found = http.post(
            f'/api/projects/{project_id}/search',
            json={'query': '注册资本', 'categories': ['attachments']}).json()
        assert _ids(found) == {attachment_id}
        missed = http.post(
            f'/api/projects/{project_id}/search',
            json={'query': '注册资本', 'categories': ['organizational_docs']}).json()
        assert attachment_id not in _ids(missed) and other_id in _ids(missed)

    tool_app = SimpleNamespace(state=SimpleNamespace(database=app.state.database, evidence=service))

    def tool(name, arguments):
        try:
            result = _call_tool(tool_app, {'name': name, 'arguments': arguments})
        except ToolFailure as exc:
            return {'isError': True, 'content': [{'text': str(exc)}]}
        if result.get('isError'):
            return result
        return json.loads(result['content'][0]['text'])

    listed = tool('list_documents', {'project_id': project_id, 'categories': ['attachments']})
    assert {item['id'] for item in listed['documents']} == {attachment_id}
    assert listed['documents'][0]['category'] == 'attachments'
    hidden = tool('list_documents', {'project_id': project_id, 'categories': ['organizational_docs']})
    assert attachment_id not in {item['id'] for item in hidden['documents']}
    searched = tool('search_evidence', {
        'project_id': project_id, 'query': '注册资本', 'categories': ['attachments']})
    assert searched['status'] == 'candidates'
    assert {row['document_id'] for row in searched['results']} == {attachment_id}
    not_attachment = tool('search_evidence', {
        'project_id': project_id, 'query': '注册资本', 'categories': ['organizational_docs']})
    assert attachment_id not in {row['document_id'] for row in not_attachment['results']}


MIGRATION_021_MAP = {**LEGACY_CATEGORY_MAP, 'uncategorized': 'uncategorized'}


def test_migration_021_remaps_legacy_categories(tmp_path, database_url):
    from uuid import uuid4 as new_uuid
    app = application(tmp_path, database_url, migrate=False, limit=2_000_000)
    db = app.state.database
    root = Path('src/dd_agent/migrations')
    early = sorted(path for path in root.glob('*.sql') if path.name <= '020_interview_record.sql')
    later = sorted(path for path in root.glob('*.sql') if path.name.startswith('021_'))
    project_id = new_uuid()
    samples = {old: new_uuid() for old in MIGRATION_021_MAP}
    leftover_id = new_uuid()
    with db.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(74332101)')
        for source in early:
            conn.execute(source.read_text(encoding='utf-8'))
        conn.execute('INSERT INTO dd_agent.projects (id, name) VALUES (%s, %s)', (project_id, '映射项目'))
        for old, document_id in samples.items():
            conn.execute(
                "INSERT INTO dd_agent.documents (id, project_id, sha256, byte_size, storage_key, status, reason, category) "
                "VALUES (%s, %s, %s, 1, %s, 'registered', '', %s)",
                (document_id, project_id, document_id.hex * 2, f'store/{document_id}', old))
        conn.execute('ALTER TABLE dd_agent.documents DROP CONSTRAINT IF EXISTS documents_category_known')
        conn.execute(
            "INSERT INTO dd_agent.documents (id, project_id, sha256, byte_size, storage_key, status, reason, category) "
            "VALUES (%s, %s, %s, 1, %s, 'registered', '', %s)",
            (leftover_id, project_id, leftover_id.hex * 2, f'store/{leftover_id}', 'totally_unknown'))
        for source in later:
            conn.execute(source.read_text(encoding='utf-8'))
    # 只跑到 021 时库版本仍是 21，健康检查已期望 22，因此应提示需要迁移。
    assert db.health()['status'] == 'needs_migration'
    with db.connect() as conn:
        version = conn.execute('SELECT max(version) AS version FROM dd_agent.schema_version').fetchone()['version']
        assert version == 21
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conname = 'documents_category_known'"
        ).fetchone()['definition']
        assert 'organizational_docs' in definition and 'uncategorized' in definition
        assert 'interview_record' not in definition
        for old, document_id in samples.items():
            row = conn.execute('SELECT category FROM dd_agent.documents WHERE id=%s', (document_id,)).fetchone()
            assert row['category'] == MIGRATION_021_MAP[old], (old, row['category'])
        leftover = conn.execute('SELECT category FROM dd_agent.documents WHERE id=%s', (leftover_id,)).fetchone()
        assert leftover['category'] == 'uncategorized'
        conn.execute(root.joinpath('021_dd_directories.sql').read_text(encoding='utf-8'))
        again = conn.execute('SELECT category FROM dd_agent.documents WHERE id=%s', (samples['articles'],)).fetchone()
        assert again['category'] == 'organizational_docs'


def test_migration_022_remaps_uncategorized_to_other(tmp_path, database_url):
    from uuid import uuid4 as new_uuid
    app = application(tmp_path, database_url, migrate=False, limit=2_000_000)
    db = app.state.database
    root = Path('src/dd_agent/migrations')
    early = sorted(path for path in root.glob('*.sql') if path.name <= '020_interview_record.sql')
    mid = root.joinpath('021_dd_directories.sql')
    late = root.joinpath('022_drop_uncategorized.sql')
    project_id = new_uuid()
    samples = {old: new_uuid() for old in MIGRATION_021_MAP}
    leftover_id = new_uuid()
    already_other_id = new_uuid()
    with db.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(74332102)')
        for source in early:
            conn.execute(source.read_text(encoding='utf-8'))
        conn.execute('INSERT INTO dd_agent.projects (id, name) VALUES (%s, %s)', (project_id, '去未分类项目'))
        for old, document_id in samples.items():
            conn.execute(
                "INSERT INTO dd_agent.documents (id, project_id, sha256, byte_size, storage_key, status, reason, category) "
                "VALUES (%s, %s, %s, 1, %s, 'registered', '', %s)",
                (document_id, project_id, document_id.hex * 2, f'store/{document_id}', old))
        conn.execute('ALTER TABLE dd_agent.documents DROP CONSTRAINT IF EXISTS documents_category_known')
        conn.execute(
            "INSERT INTO dd_agent.documents (id, project_id, sha256, byte_size, storage_key, status, reason, category) "
            "VALUES (%s, %s, %s, 1, %s, 'registered', '', %s)",
            (leftover_id, project_id, leftover_id.hex * 2, f'store/{leftover_id}', 'totally_unknown'))
        conn.execute(
            "INSERT INTO dd_agent.documents (id, project_id, sha256, byte_size, storage_key, status, reason, category) "
            "VALUES (%s, %s, %s, 1, %s, 'registered', '', %s)",
            (already_other_id, project_id, already_other_id.hex * 2, f'store/{already_other_id}', 'other'))
        # 先到 021：未分类与残留仍是 uncategorized，模拟本机已升到 21 的库。
        conn.execute(mid.read_text(encoding='utf-8'))
        after_021 = {
            row['id']: row['category']
            for row in conn.execute(
                'SELECT id, category FROM dd_agent.documents WHERE project_id=%s', (project_id,)
            ).fetchall()
        }
        assert after_021[samples['uncategorized']] == 'uncategorized'
        assert after_021[leftover_id] == 'uncategorized'
        assert after_021[samples['articles']] == 'organizational_docs'
        assert after_021[already_other_id] == 'other'
        # 再跑 022，覆盖「已应用 021」和「021 刚把残留写成 uncategorized」两种情况。
        conn.execute(late.read_text(encoding='utf-8'))
    assert db.health()['status'] == 'ready'
    with db.connect() as conn:
        version = conn.execute('SELECT max(version) AS version FROM dd_agent.schema_version').fetchone()['version']
        assert version == 22
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conname = 'documents_category_known'"
        ).fetchone()['definition']
        assert 'uncategorized' not in definition
        for code in CATEGORIES:
            assert code in definition
        default = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema='dd_agent' AND table_name='documents' AND column_name='category'"
        ).fetchone()['column_default']
        assert 'other' in default and 'uncategorized' not in default
        for old, document_id in samples.items():
            row = conn.execute('SELECT category FROM dd_agent.documents WHERE id=%s', (document_id,)).fetchone()
            assert row['category'] == LEGACY_CATEGORY_MAP[old], (old, row['category'])
        leftover = conn.execute('SELECT category FROM dd_agent.documents WHERE id=%s', (leftover_id,)).fetchone()
        assert leftover['category'] == 'other'
        already = conn.execute('SELECT category FROM dd_agent.documents WHERE id=%s', (already_other_id,)).fetchone()
        assert already['category'] == 'other'
        finance = conn.execute('SELECT category FROM dd_agent.documents WHERE id=%s', (samples['audit_report'],)).fetchone()
        assert finance['category'] == 'finance'
        # 重跑 022 保持幂等：有效目录不变，已是 other 的仍是 other。
        conn.execute(late.read_text(encoding='utf-8'))
        again = {
            row['id']: row['category']
            for row in conn.execute(
                'SELECT id, category FROM dd_agent.documents WHERE project_id=%s', (project_id,)
            ).fetchall()
        }
        assert again[samples['articles']] == 'organizational_docs'
        assert again[samples['uncategorized']] == 'other'
        assert again[leftover_id] == 'other'
        assert again[already_other_id] == 'other'
        assert again[samples['audit_report']] == 'finance'


def _chunk_signature(database, document_id):
    with database.connect() as conn:
        chunks = conn.execute(
            'SELECT count(*) AS n, coalesce(max(index_version), \'\') AS version FROM dd_agent.evidence_chunks WHERE document_id=%s',
            (document_id,)).fetchone()
        pages = conn.execute('SELECT count(*) AS n FROM dd_agent.indexed_pages WHERE document_id=%s', (document_id,)).fetchone()
    return chunks['n'], chunks['version'], pages['n']
