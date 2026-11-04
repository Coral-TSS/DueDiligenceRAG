"""RG-05g1 有界页读取：目录 keyset、单页定位、历史分页与错误映射。"""
from uuid import uuid4

import pytest

from dd_agent.page_queries import PageReader
from dd_agent.performance import capture
from test_workspace import application, client, project, upload

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 32


def seed(app, document_id, count, version='scan-v1', image_page=None, prefix='第'):
    """一次 SQL 造出 count 页，避免逐页提交拖慢快照测试。"""
    with app.state.database.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.document_pages
            (document_id,page_number,pipeline_version,status,method,text,lines,image_key,error_code)
            SELECT %s,n,%s,'succeeded','ocr',%s||n||'页正文','[]'::jsonb,
                   CASE WHEN n=%s THEN 'pages/preview.png' ELSE '' END,''
            FROM generate_series(1,%s) AS n
            ON CONFLICT (document_id,page_number,pipeline_version) DO UPDATE
            SET text=EXCLUDED.text,image_key=EXCLUDED.image_key''',
            (document_id, version, prefix, image_page or 0, count))
    app.state.database.set_reading(document_id, count, version)


@pytest.fixture
def workspace(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    with client(app) as c:
        p = project(c)
        d = upload(c, p).json()['document']['id']
        yield app, c, p, d


def test_page_catalog_is_metadata_only_and_keyset_paged(workspace):
    app, c, p, d = workspace
    seed(app, d, 200)
    base = f'/api/projects/{p}/documents/{d}'
    first = c.get(f'{base}/page-catalog', params={'limit': 25})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body['page_count'] == 200 and body['total_pages'] == 200 and body['succeeded_pages'] == 200
    assert body['reading_version'] == 'scan-v1' and body['snapshot'] == 'not_frozen'
    assert body['as_of'] and body['content_revision'] and body['status_revision']
    assert len(body['items']) == 25 and body['next_cursor'] == 25
    assert [i['page_number'] for i in body['items']] == list(range(1, 26))
    # 目录不含正文、行框或内部存储位置。
    assert all(set(i) == {'page_number', 'status', 'method', 'error_code', 'verification',
                          'source_revision', 'has_image'} for i in body['items'])
    assert '第1页正文' not in first.text and 'pages/preview.png' not in first.text

    seen, cursor = [], body['next_cursor']
    for _ in range(2):
        page = c.get(f'{base}/page-catalog', params={'limit': 25, 'after': cursor}).json()
        seen.extend(i['page_number'] for i in page['items'])
        cursor = page['next_cursor']
    assert seen == list(range(26, 76))
    assert c.get(f'{base}/page-catalog').json()['items'][-1]['page_number'] == 50

    for params in ({'limit': 0}, {'limit': 101}, {'after': -1}):
        assert c.get(f'{base}/page-catalog', params=params).status_code == 422


def test_deep_link_reads_one_page_without_loading_the_document(workspace, tmp_path):
    app, c, p, d = workspace
    seed(app, d, 649)
    base = f'/api/projects/{p}/documents/{d}'
    response = c.get(f'{base}/pages/600')
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['text'] == '第600页正文' and page['page_number'] == 600
    assert page['total_length'] == len('第600页正文') and page['next_offset'] is None
    assert len(response.content) < 2000
    # 附近目录也不遍历前 599 页。
    near = c.get(f'{base}/page-catalog', params={'after': 599, 'limit': 10}).json()
    assert [i['page_number'] for i in near['items']] == list(range(600, 610))

    # 行数与 SQL 次数和全卷页数无关：20 页与 649 页完全相同。
    # 固定开销＝材料边界 2＋阅读版本 1＋单页 1＋连接归还时的 3 条会话清理语句。
    small = upload(c, p, '小卷.pdf', b'%PDF-1.4 small\n%%EOF').json()['document']['id']
    seed(app, small, 20)
    with capture(tmp_path / 'perf', 'page_reads_large') as large:
        PageReader(app.state.database, p, d).page(600)
    with capture(tmp_path / 'perf', 'page_reads_small') as tiny:
        PageReader(app.state.database, p, small).page(20)
    assert large.counters['sql.fetched_rows'] <= 6 and large.counters['sql.calls'] <= 8
    assert large.counters['sql.fetched_rows'] == tiny.counters['sql.fetched_rows']
    assert large.counters['sql.calls'] == tiny.counters['sql.calls']


def test_text_slice_and_offset_continuation_are_bound_to_the_source(workspace):
    app, c, p, d = workspace
    seed(app, d, 5)
    with app.state.database.connect() as conn:
        conn.execute("UPDATE dd_agent.document_pages SET text=repeat('证',250000) WHERE document_id=%s AND page_number=1", (d,))
    base = f'/api/projects/{p}/documents/{d}'
    first = c.get(f'{base}/pages/1').json()
    assert first['total_length'] == 250000 and len(first['text']) == 100000
    assert first['next_offset'] == 100000 and first['slice_limit'] == 100000

    second = c.get(f'{base}/pages/1', params={'offset': first['next_offset'],
                                              'source_revision': first['revision']}).json()
    assert len(second['text']) == 100000 and second['next_offset'] == 200000

    stale = c.get(f'{base}/pages/1', params={'offset': 100000, 'source_revision': 'not-the-revision'})
    assert stale.status_code == 409 and stale.json()['code'] == 'stale'
    beyond = c.get(f'{base}/pages/1', params={'offset': 250001, 'source_revision': first['revision']})
    assert beyond.status_code == 409 and beyond.json()['code'] == 'stale'
    assert c.get(f'{base}/pages/1', params={'offset': -1}).status_code == 422


def test_raw_and_effective_views_with_optional_line_boxes(workspace):
    app, c, p, d = workspace
    seed(app, d, 3)
    with app.state.database.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.machine_pages
            (id,document_id,page_number,base_revision,profile_version,result)
            SELECT %s,%s,1,md5(p.pipeline_version||':'||p.updated_at::text),'profile-v1',
                jsonb_build_object('text','增强后的第1页','verification','machine_checked')
            FROM dd_agent.document_pages p WHERE p.document_id=%s AND p.page_number=1''', (uuid4(), d, d))
        conn.execute("UPDATE dd_agent.document_pages SET lines=%s::jsonb WHERE document_id=%s AND page_number=1",
                     ('[{"text":"第1页正文","score":0.9,"box":[[0,0],[10,0],[10,10],[0,10]]}]', d))
    base = f'/api/projects/{p}/documents/{d}'
    raw = c.get(f'{base}/pages/1', params={'view': 'raw'}).json()
    effective = c.get(f'{base}/pages/1').json()
    assert raw['text'] == '第1页正文' and raw['verification'] == 'unverified' and 'lines' not in raw
    assert effective['text'] == '增强后的第1页' and effective['verification'] == 'machine_checked'
    assert effective['source_revision'] != raw['revision'] and raw['revision'] == raw['raw_revision']
    with_lines = c.get(f'{base}/pages/1', params={'view': 'raw', 'include_lines': 'true'}).json()
    assert with_lines['lines'][0]['box'] == [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert c.get(f'{base}/pages/1', params={'view': 'bogus'}).status_code == 422


def test_errors_for_unknown_unprocessed_and_cross_project(workspace):
    app, c, p, d = workspace
    seed(app, d, 10)
    with app.state.database.connect() as conn:
        conn.execute('DELETE FROM dd_agent.document_pages WHERE document_id=%s AND page_number>3', (d,))
    base = f'/api/projects/{p}/documents/{d}'
    out_of_range = c.get(f'{base}/pages/11')
    assert out_of_range.status_code == 404 and out_of_range.json()['code'] == 'not_found'
    pending = c.get(f'{base}/pages/5')
    assert pending.status_code == 409 and pending.json()['code'] == 'not_processed'
    assert c.get(f'{base}/pages/0').status_code == 422

    # 尚无阅读版本时，目录和单页都明确要求先处理，而不是返回空数组。
    other = upload(c, p, '未读材料.pdf', b'%PDF-1.4\n%%EOF').json()['document']['id']
    assert c.get(f'/api/projects/{p}/documents/{other}/page-catalog').json()['code'] == 'not_processed'
    assert c.get(f'/api/projects/{p}/documents/{other}/pages/1').status_code == 409

    # 跨项目只返回统一提示，不回显该材料或其项目。
    foreign_project = project(c, '另一个项目')
    foreign = upload(c, foreign_project).json()['document']['id']
    denied = c.get(f'/api/projects/{p}/documents/{foreign}/page-catalog')
    assert denied.status_code == 404 and denied.json()['detail'] == '材料不存在或不属于当前项目。'
    assert foreign not in denied.text and foreign_project not in denied.text
    assert c.get(f'/api/projects/{p}/documents/{uuid4()}/pages/1').status_code == 404


def test_page_history_is_bounded_and_marks_the_current_attempt(workspace):
    app, c, p, d = workspace
    seed(app, d, 3)
    with app.state.database.connect() as conn:
        for index in range(15):
            conn.execute('''INSERT INTO dd_agent.machine_pages
                (id,document_id,page_number,base_revision,profile_version,result,created_at)
                SELECT %s,%s,1,
                    CASE WHEN %s=0 THEN md5(p.pipeline_version||':'||p.updated_at::text) ELSE 'outdated-'||%s END,
                    'profile-'||%s,
                    jsonb_build_object('text',repeat('历',%s),'verification','machine_checked'),
                    clock_timestamp() - (%s||' minutes')::interval
                FROM dd_agent.document_pages p WHERE p.document_id=%s AND p.page_number=1''',
                (uuid4(), d, index, index, index, 600 if index < 2 else 20, index, d))
    base = f'/api/projects/{p}/documents/{d}'
    first = c.get(f'{base}/pages/1/history', params={'limit': 10}).json()
    assert len(first['items']) == 10 and first['next_cursor']
    assert sum(1 for i in first['items'] if i['is_current']) == 1
    assert first['items'][0]['verification'] == 'machine_checked'
    assert first['items'][0]['preview_truncated'] is True and first['items'][0]['text_length'] == 600
    assert len(first['items'][0]['text_preview']) == 500
    rest = c.get(f'{base}/pages/1/history', params={'limit': 10, 'after': first['next_cursor']}).json()
    assert len(rest['items']) == 5 and rest['next_cursor'] is None
    numbers = [i['created_at'] for i in first['items'] + rest['items']]
    assert numbers == sorted(numbers, reverse=True) and len(set(numbers)) == 15
    assert c.get(f'{base}/pages/1/history', params={'limit': 51}).status_code == 422
    assert c.get(f'{base}/pages/1/history', params={'after': 'not-a-cursor'}).status_code == 422
    assert c.get(f'{base}/pages/1/history', params={'after': '2026-01-01T00:00:00|' + str(uuid4())}).json()['items'] == []


def test_same_base_history_only_marks_the_effective_record_current(workspace):
    app, c, p, d = workspace
    seed(app, d, 1)
    with app.state.database.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.machine_pages
            (id,document_id,page_number,base_revision,profile_version,result)
            SELECT gen_random_uuid(),p.document_id,1,p.raw_revision,'retry-'||n,
                jsonb_build_object('text','增强'||n,'verification','machine_checked')
            FROM dd_agent.effective_document_pages p CROSS JOIN generate_series(1,3) n
            WHERE p.document_id=%s AND p.page_number=1''', (d,))
    url = f'/api/projects/{p}/documents/{d}/pages/1/history'
    first = c.get(url, params={'limit': 1}).json()
    second = c.get(url, params={'limit': 1, 'after': first['next_cursor']}).json()
    third = c.get(url, params={'limit': 1, 'after': second['next_cursor']}).json()
    records = first['items'] + second['items'] + third['items']
    assert sum(row['is_current'] for row in records) == 1
    assert first['items'][0]['is_current']
    assert all(row['base_matches_current'] for row in records)


def test_page_summary_projects_current_metadata_without_history_or_body(workspace):
    app, c, p, d = workspace
    seed(app, d, 1)
    with app.state.database.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.machine_pages
            (id,document_id,page_number,base_revision,profile_version,result)
            SELECT gen_random_uuid(),p.document_id,1,p.raw_revision,'retry-'||n,
                jsonb_build_object('text',repeat('历',20000),'outcome','enhanced',
                    'route',jsonb_build_object('reason','test'),'regions','[]'::jsonb)
            FROM dd_agent.effective_document_pages p CROSS JOIN generate_series(1,30) n
            WHERE p.document_id=%s AND p.page_number=1''', (d,))
    base = f'/api/projects/{p}/documents/{d}/pages/1'
    response = c.get(base + '/summary')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['current']['outcome'] == 'enhanced'
    assert set(data['current']) == {'outcome', 'route', 'regions'}
    assert 'history' not in data and '历历' not in response.text
    assert len(response.content) < 2000
    assert data['source_revision'] == c.get(base).json()['source_revision']
    assert c.get(base + '/summary', params={'source_revision': 'old'}).status_code == 409
    other = project(c, '另一个项目')
    assert c.get(f'/api/projects/{other}/documents/{d}/pages/1/summary').status_code == 404


def test_content_revision_ignores_progress_only_changes(workspace):
    app, c, p, d = workspace
    seed(app, d, 4)
    base = f'/api/projects/{p}/documents/{d}'
    before = c.get(f'{base}/page-catalog').json()
    # 新增一页失败记录：进度与状态变了，但没有任何新正文。
    with app.state.database.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.document_pages
            (document_id,page_number,pipeline_version,status,method,text,lines,image_key,error_code)
            VALUES (%s,5,%s,'failed','ocr','','[]'::jsonb,'','worker_failed')''', (d, 'scan-v1'))
    progress = c.get(f'{base}/page-catalog').json()
    assert progress['content_revision'] == before['content_revision']
    assert progress['status_revision'] != before['status_revision']
    assert progress['failed_pages'] == 1
    # 正文真正变化时两个修订都变。
    with app.state.database.connect() as conn:
        conn.execute('''UPDATE dd_agent.document_pages SET text='新的第1页',updated_at=clock_timestamp()
            WHERE document_id=%s AND page_number=1''', (d,))
    changed = c.get(f'{base}/page-catalog').json()
    assert changed['content_revision'] != before['content_revision']
    assert changed['status_revision'] != before['status_revision']


def test_image_lookup_targets_only_the_requested_page(workspace, tmp_path):
    app, c, p, d = workspace
    seed(app, d, 649, image_page=600)
    (tmp_path / 'pages').mkdir(exist_ok=True)
    (tmp_path / 'pages' / 'preview.png').write_bytes(PNG)
    base = f'/api/projects/{p}/documents/{d}'
    response = c.get(f'{base}/pages/600/image')
    assert response.status_code == 200 and response.content == PNG
    missing = c.get(f'{base}/pages/601/image')
    assert missing.status_code == 404 and '预览图' in missing.json()['detail']

    with capture(tmp_path / 'perf', 'page_reads_image') as trace:
        app.state.database.page_image_key(d, 600)
    assert trace.counters['sql.fetched_rows'] == 1
