"""RG-05g2 前端切换后的运行期约束：旧无界接口收口、传输量对照、DOM 接线。"""
import re
from pathlib import Path

from test_page_reads import seed
from test_workspace import application, client, project, upload

STATIC = Path(__file__).resolve().parents[2] / 'src' / 'dd_agent' / 'static'


def test_legacy_page_list_refuses_beyond_the_safe_range(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    with client(app) as c:
        p = project(c)
        small = upload(c, p, '小卷.pdf').json()['document']['id']
        seed(app, small, 90)
        base = f'/api/projects/{p}/documents/{small}'
        # 安全范围内旧接口行为不变，旧前端仍可用。
        legacy = c.get(f'{base}/pages')
        assert legacy.status_code == 200 and len(legacy.json()) == 90

        big = upload(c, p, '大卷.pdf', b'%PDF-1.4 large\n%%EOF').json()['document']['id']
        seed(app, big, 649)
        refused = c.get(f'/api/projects/{p}/documents/{big}/pages')
        assert refused.status_code == 409 and refused.json()['code'] == 'bounded_read_required'
        # 明确指向有界入口，而不是静默返回残缺数组。
        assert 'page-catalog' in refused.json()['detail'] and '649' in refused.json()['detail']
        # 同一材料的有界入口照常可用。
        assert c.get(f'/api/projects/{p}/documents/{big}/page-catalog').status_code == 200
        assert c.get(f'/api/projects/{p}/documents/{big}/pages/649').json()['page_number'] == 649


def test_bounded_reads_transfer_far_less_than_the_legacy_list(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    with client(app) as c:
        p = project(c)
        d = upload(c, p).json()['document']['id']
        seed(app, d, 90)
        with app.state.database.connect() as conn:
            conn.execute("UPDATE dd_agent.document_pages SET text=repeat('证',2000) WHERE document_id=%s", (d,))
        base = f'/api/projects/{p}/documents/{d}'
        legacy = c.get(f'{base}/pages')
        catalog = c.get(f'{base}/page-catalog', params={'limit': 100})
        single = c.get(f'{base}/pages/45')
        assert legacy.status_code == catalog.status_code == single.status_code == 200
        # 一次取 90 页正文 vs 一份目录 vs 一页正文：目录不带正文，单页只带一页。
        assert '证证证' not in catalog.text
        assert len(catalog.content) * 10 < len(legacy.content)
        assert len(single.content) * 20 < len(legacy.content)
        # 进度探测只要一条（甚至为空）的目录，也能带回全卷两个修订。
        probe = c.get(f'{base}/page-catalog', params={'limit': 1, 'after': 90})
        assert probe.json()['items'] == [] and probe.json()['status_revision']


def test_frontend_scripts_use_only_bounded_endpoints():
    reader = (STATIC / 'reader.js').read_text(encoding='utf-8')
    app = (STATIC / 'app.js').read_text(encoding='utf-8')
    # 阅读器不再整卷拉取，工作区不再用无界材料列表。
    assert '`${base}/pages`' not in reader
    assert 'page-catalog' in reader and 'include_lines=true' in reader
    assert '`/api/projects/${current}/documents`' not in app
    assert 'material-catalog' in app
    # 单轮询链与隐藏退避必须留在阅读器里。
    assert 'clearTimeout' in reader and 'visibilitychange' in reader and 'AbortController' in reader


def test_frontend_element_ids_exist_in_markup():
    for script, markup in (('reader.js', 'reader.html'), ('app.js', 'index.html')):
        code = (STATIC / script).read_text(encoding='utf-8')
        html = (STATIC / markup).read_text(encoding='utf-8')
        used = set(re.findall(r'\$\("([^"]+)"\)', code))
        present = set(re.findall(r'id="([^"]+)"', html))
        assert used <= present, f'{script} 引用了不存在的元素：{sorted(used - present)}'
