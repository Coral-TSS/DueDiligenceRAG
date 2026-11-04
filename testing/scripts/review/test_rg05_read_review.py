"""Review-only reproductions of observed defects, outside the normal acceptance suite.

Assertions deliberately describe the current incorrect behavior. Remove or invert them
when fixing the defects; do not treat these as desired product acceptance criteria.
"""
import subprocess
from pathlib import Path

from conftest import postgres, database_url
from test_page_reads import seed
from test_workspace import application, client, project, upload


def test_reader_javascript_observed_defects():
    root = Path(__file__).resolve().parents[3]
    node = root / '.local/harness/runtime/node-v22.19.0-win-x64/node.exe'
    result = subprocess.run([str(node), str(Path(__file__).with_name('rg05-reader-review.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    print(result.stdout)


def test_same_base_history_has_multiple_current_records(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    with client(app) as c:
        p = project(c); d = upload(c, p).json()['document']['id']
        seed(app, d, 1)
        with app.state.database.connect() as conn:
            conn.execute('''INSERT INTO dd_agent.machine_pages
                (id,document_id,page_number,base_revision,profile_version,result)
                SELECT gen_random_uuid(),p.document_id,1,p.raw_revision,'review-probe-'||n,
                    jsonb_build_object('text','增强版本'||n,'verification','machine_checked')
                FROM dd_agent.effective_document_pages p CROSS JOIN generate_series(1,3) n
                WHERE p.document_id=%s AND p.page_number=1''', (d,))
        base = f'/api/projects/{p}/documents/{d}'
        rows = c.get(f'{base}/pages/1/history').json()['items']
        current = sum(row['is_current'] for row in rows)
        # Only one latest machine result is actually selected by the effective view.
        assert current == 3
        print('history current flags:', current, '/ records:', len(rows))


def test_reader_extras_still_download_entire_history(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    with client(app) as c:
        p = project(c); d = upload(c, p).json()['document']['id']
        seed(app, d, 1)
        with app.state.database.connect() as conn:
            conn.execute('''INSERT INTO dd_agent.machine_pages
                (id,document_id,page_number,base_revision,profile_version,result)
                SELECT gen_random_uuid(),p.document_id,1,p.raw_revision,'review-probe-'||n,
                    jsonb_build_object('text',repeat('历',20000),'verification','machine_checked')
                FROM dd_agent.effective_document_pages p CROSS JOIN generate_series(1,30) n
                WHERE p.document_id=%s AND p.page_number=1''', (d,))
        base = f'/api/projects/{p}/documents/{d}'
        legacy = c.get(f'{base}/pages/1/automatic')
        bounded = c.get(f'{base}/pages/1/history')
        assert len(legacy.json()['history']) == 30
        assert len(bounded.json()['items']) == 10
        assert len(legacy.content) > 1_000_000
        print('automatic response bytes:', len(legacy.content),
              ' / bounded history bytes:', len(bounded.content))
