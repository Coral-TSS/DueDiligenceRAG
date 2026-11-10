"""旧版 DOC／XLS／PPT 经 soffice 转成现代格式后再解析。假进程覆盖失败路径；有真实 LibreOffice 时再跑一轮。"""
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import pytest

from dd_agent.config import Settings
from dd_agent.legacy_office import convert_for_parse, soffice_status
from dd_agent.office import classify_saved_file, parse_office
from dd_agent.retrieval import EvidenceService
from dd_agent.storage import Storage
from legacy_ole import mini_word, presentation, word_document, word_with_macro, workbook
from test_retrieval import TinyModels
from test_workspace import client, project


_FAKE = r'''#!__PY__
import json, os, subprocess, sys, time
from pathlib import Path

MODE = "__MODE__"

def outdir():
    args = sys.argv
    for index, arg in enumerate(args):
        if arg == "--outdir" and index + 1 < len(args):
            return Path(args[index + 1])
    raise SystemExit(2)

def log(payload):
    path = Path(__file__).with_suffix(".log")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

def main():
    if "--version" in sys.argv:
        print("LibreOffice 24.2.5.2 420(Build:2)")
        return 0
    source = Path(sys.argv[-1])
    overwritten = False
    try:
        with source.open("ab") as handle:
            handle.write(b"OVERWRITE")
        overwritten = True
    except OSError:
        overwritten = False
    install = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("-env:UserInstallation="))
    log({"argv": sys.argv, "input": str(source.resolve()), "overwrite": overwritten, "profile": install})
    if MODE == "fail":
        return 3
    if MODE == "timeout":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        Path(__file__).with_suffix(".pids").write_text(f"{os.getpid()}\n{child.pid}\n", encoding="utf-8")
        time.sleep(120)
        return 0
    profile = Path(unquote(urlparse(install).path))
    text = (profile / "user" / "registrymodifications.xcu").read_text(encoding="utf-8")
    if "MacroSecurity" not in text or "<value>3</value>" not in text:
        return 4
    if MODE == "sleep":
        started = time.time()
        time.sleep(0.5)
        Path(__file__).with_name(f"{os.getpid()}.stamp").write_text(f"{started}\n{time.time()}\n", encoding="utf-8")
    destination = outdir() / ("source.docx" if source.suffix.lower() == ".doc" else "source.xlsx" if source.suffix.lower() == ".xls" else "source.pptx")
    if source.suffix.lower() == ".doc":
        from docx import Document
        document = Document()
        document.add_paragraph("甲公司章程约定注册资本。")
        document.save(destination)
    elif source.suffix.lower() == ".xls":
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = "利润表"
        sheet["A1"] = "营业收入资本"
        book.save(destination)
    else:
        from pptx import Presentation
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        shape = slide.shapes.add_textbox(0, 0, 100, 100)
        shape.text_frame.text = "并购方案资本"
        deck.save(destination)
    return 0

from urllib.parse import unquote, urlparse
raise SystemExit(main())
'''


def _fake(directory, mode):
    path = Path(directory) / f'soffice-{mode}'
    path.write_text(_FAKE.replace('__PY__', sys.executable).replace('__MODE__', mode), encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _logs(binary):
    path = Path(binary).with_suffix('.log')
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _settings(tmp_path, binary=None, **extra):
    values = dict(data_dir=tmp_path / 'data', soffice_timeout_seconds=extra.pop('soffice_timeout_seconds', 30), soffice_concurrency=1)
    if binary is not None:
        values['soffice_path'] = Path(binary)
    values.update(extra)
    return Settings(**values)


def _app(tmp_path, database_url, **extra):
    from dd_agent.app import create_app
    settings = Settings(
        database_url=database_url, data_dir=tmp_path, max_upload_bytes=2_000_000,
        search_index_mode='page', **extra)
    app = create_app(settings)
    app.state.database.migrate()
    return app


def _service(app, tmp_path, database_url, models=None, **extra):
    return EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page', **extra),
        models=models or TinyModels(), database=app.state.database)


def _write(tmp_path, name, body):
    path = tmp_path / name
    path.write_bytes(body)
    return path


def test_structure_rejects_macro_password_and_corrupt_without_launching_soffice(tmp_path):
    binary = _fake(tmp_path, 'fail')
    settings = _settings(tmp_path, binary)
    samples = {
        '宏.doc': (word_with_macro(), '宏'),
        '密码.doc': (word_document(encrypted=True), '密码'),
        '迷你密码.doc': (mini_word(encrypted=True), '密码'),
        '密码.xls': (workbook(encrypted=True), '密码'),
        '密码.ppt': (presentation(encrypted=True), '密码'),
        '损坏.doc': (word_document()[:700], '不是有效'),
    }
    for name, (body, marker) in samples.items():
        path = _write(tmp_path, name, body)
        status, reason = classify_saved_file(name, path, lambda: soffice_status(settings))
        assert status != 'registered', name
        assert marker in reason
        prepared = convert_for_parse(settings, path, path.suffix.lower())
        assert prepared['ok'] is False
        assert marker in prepared['reason']
    assert _logs(binary) == []


def test_missing_and_configured_soffice_are_explicit(tmp_path, monkeypatch):
    path = _write(tmp_path, '章程.doc', word_document())
    missing = classify_saved_file('章程.doc', path, lambda: soffice_status(Settings()))
    assert missing[0] == 'unsupported'
    assert '未找到 LibreOffice 的 soffice' in missing[1]
    assert '未解析正文' in missing[1]
    configured = classify_saved_file(
        '章程.doc', path, lambda: soffice_status(Settings(soffice_path=tmp_path / 'absent-soffice')))
    assert configured[0] == 'unsupported'
    assert 'DDA_SOFFICE_PATH' in configured[1]
    binary = tmp_path / 'soffice.com'
    binary.write_bytes(b'')
    monkeypatch.setattr('dd_agent.legacy_office._windows_candidates', lambda: [binary])
    monkeypatch.setattr('dd_agent.legacy_office.os.name', 'nt')
    found, state = soffice_status(Settings())
    assert found == binary and state == 'ok'
    absent, absent_state = soffice_status(Settings(soffice_path=tmp_path / 'nope'))
    assert absent is None and absent_state == 'configured_missing'
    with pytest.raises(ValueError):
        Settings(soffice_concurrency=0)
    with pytest.raises(ValueError):
        Settings(soffice_concurrency=5)
    with pytest.raises(ValueError):
        Settings(soffice_timeout_seconds=0)


def test_empty_doc_and_docm_are_not_sent_to_soffice(tmp_path):
    store = Storage(tmp_path, 1000)
    empty = store.save('p', '空.doc', BytesIO(b''))
    assert empty['status'] == 'invalid' and '为空' in empty['reason']
    macro = store.save('p', '宏.docm', BytesIO(b'not-a-document'))
    assert macro['status'] == 'unsupported'
    assert 'LibreOffice' not in macro['reason']


def test_from_env_reads_soffice_settings(monkeypatch):
    monkeypatch.setenv('DDA_SOFFICE_PATH', '/tmp/soffice-example')
    monkeypatch.setenv('DDA_SOFFICE_TIMEOUT_SECONDS', '15')
    monkeypatch.setenv('DDA_SOFFICE_CONCURRENCY', '2')
    settings = Settings.from_env()
    assert settings.soffice_path == Path('/tmp/soffice-example')
    assert settings.soffice_timeout_seconds == 15
    assert settings.soffice_concurrency == 2


def test_fake_soffice_converts_without_touching_the_original_or_retrying(tmp_path):
    binary = _fake(tmp_path, 'success')
    settings = _settings(tmp_path, binary)
    samples = {
        '.doc': word_document(),
        '.xls': workbook(),
        '.ppt': presentation(),
    }
    profiles = []
    for ext, body in samples.items():
        path = _write(tmp_path, f'原件{ext}', body)
        before = path.stat()
        started = time.monotonic()
        prepared = convert_for_parse(settings, path, ext)
        assert time.monotonic() - started < 20
        assert prepared['ok'] is True, prepared
        assert path.read_bytes() == body
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        assert path.stat().st_mode == before.st_mode
        units = parse_office('converted' + prepared['target_extension'], prepared['blob'])
        assert units and units[0]['text']
        assert prepared['conversion']['converter'] == 'LibreOffice 24.2.5.2'
        assert prepared['conversion']['source_extension'] == ext
        assert '中间文件已删除' in prepared['reason']
    calls = [item for item in _logs(binary) if '--convert-to' in item['argv']]
    assert len(calls) == 3
    for item in calls:
        assert 'macro:///' not in ' '.join(item['argv'])
        assert '--headless' in item['argv']
        assert item['overwrite'] is False
        assert Path(item['input']) != Path(tmp_path / f"原件{Path(item['input']).suffix}")
        assert not Path(item['input']).exists()
        profile = item['profile']
        assert 'dda-legacy-' in profile and profile not in profiles
        profiles.append(profile)
        assert str(tmp_path / 'data') not in ' '.join(item['argv'])


def test_timeout_kills_the_process_tree_once(tmp_path):
    binary = _fake(tmp_path, 'timeout')
    settings = _settings(tmp_path, binary, soffice_timeout_seconds=2)
    path = _write(tmp_path, '挂起.doc', word_document())
    body = path.read_bytes()
    started = time.monotonic()
    prepared = convert_for_parse(settings, path, '.doc')
    elapsed = time.monotonic() - started
    assert prepared['ok'] is False
    assert prepared['error_code'] == 'legacy_timeout'
    assert '超时' in prepared['reason'] and '不会自动重试' in prepared['reason']
    assert elapsed < 15
    assert path.read_bytes() == body
    calls = [item for item in _logs(binary) if '--convert-to' in item['argv']]
    assert len(calls) == 1
    pids = [int(line) for line in Path(binary).with_suffix('.pids').read_text(encoding='utf-8').split() if line.strip()]
    assert len(pids) == 2
    for pid in pids:
        gone = False
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.05)
        assert gone, pid


def test_failed_conversion_is_not_retried(tmp_path):
    binary = _fake(tmp_path, 'fail')
    settings = _settings(tmp_path, binary, soffice_timeout_seconds=5)
    path = _write(tmp_path, '失败.doc', word_document())
    before = path.read_bytes()
    prepared = convert_for_parse(settings, path, '.doc')
    assert prepared['ok'] is False
    assert prepared['error_code'] == 'legacy_failed'
    assert '转换失败' in prepared['reason'] and '不会自动重试' in prepared['reason']
    assert path.read_bytes() == before
    assert len([item for item in _logs(binary) if '--convert-to' in item['argv']]) == 1


def test_conversions_do_not_overlap_when_concurrency_is_one(tmp_path):
    binary = _fake(tmp_path, 'sleep')
    settings = _settings(tmp_path, binary, soffice_timeout_seconds=10)
    paths = [_write(tmp_path, f'{index}.doc', word_document()) for index in (1, 2)]
    from threading import Thread
    results = []

    def run(path):
        results.append(convert_for_parse(settings, path, '.doc'))

    threads = [Thread(target=run, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert all(item['ok'] for item in results)
    stamps = []
    for stamp in Path(tmp_path).glob('*.stamp'):
        start, end = stamp.read_text(encoding='utf-8').split()
        stamps.append((float(start), float(end)))
    assert len(stamps) == 2
    stamps.sort()
    assert stamps[0][1] <= stamps[1][0] + 0.05


def test_legacy_upload_without_soffice_does_not_fail_a_modern_file(tmp_path, database_url):
    app = _app(tmp_path, database_url)
    with client(app) as http:
        project_id = project(http)
        legacy = http.post(f'/api/projects/{project_id}/documents', files={'file': ('章程.doc', word_document(), 'application/octet-stream')})
        modern = http.post(f'/api/projects/{project_id}/documents', files={'file': ('章程.docx', _docx(), 'application/octet-stream')})
    assert legacy.status_code == 201
    assert legacy.json()['document']['status'] == 'unsupported'
    assert '未找到 LibreOffice 的 soffice' in legacy.json()['document']['reason']
    assert modern.status_code == 201
    assert modern.json()['document']['status'] == 'registered'
    service = _service(app, tmp_path, database_url)
    service.index_office(project_id, modern.json()['document']['id'])
    assert any(hit['text'] == '甲公司章程约定注册资本。' for hit in service.search(project_id, '章程约定')['results'])


def test_converted_doc_is_cited_from_the_original_and_stays_inside_the_project(tmp_path, database_url):
    binary = _fake(tmp_path, 'success')
    app = _app(tmp_path, database_url, soffice_path=binary, soffice_timeout_seconds=30)
    app.state.pipeline.evidence.models = TinyModels()
    app.state.jobs.wake = lambda: None
    body = word_document()
    with client(app) as http:
        project_id = project(http)
        uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': ('章程.doc', body, 'application/octet-stream')})
        assert uploaded.status_code == 201, uploaded.text
        document = uploaded.json()['document']
        assert document['status'] == 'registered'
        document_id = document['id']
        other = project(http, '另一个项目')
        http.post(f'/api/projects/{other}/documents', files={'file': ('别的.pdf', b'%PDF-1.4\n%%EOF', 'application/pdf')})
    stored = app.state.database.document(project_id, document_id)
    original = Storage(tmp_path, 2_000_000).path(stored['storage_key'])
    before = original.stat()
    job = app.state.jobs.enqueue(project_id, document_id, wake=False)
    app.state.jobs._run_job(job)
    assert original.read_bytes() == body
    assert original.stat().st_mtime_ns == before.st_mtime_ns
    assert original.stat().st_mode == before.st_mode
    assert original.read_bytes().startswith(bytes.fromhex('D0CF11E0'))
    assert not list(Path(tmp_path).rglob('*.docx'))
    finished = app.state.jobs.latest(project_id, document_id)
    assert finished['status'] == 'completed'
    assert finished['progress']['conversion']['source_extension'] == '.doc'
    assert finished['progress']['conversion']['converter'] == 'LibreOffice 24.2.5.2'
    assert '章程.doc' not in json.dumps(finished['progress'])
    assert str(original) not in json.dumps(finished['progress'])
    saved = app.state.database.document(project_id, document_id)
    assert saved['status'] == 'registered'
    assert 'LibreOffice 24.2.5.2' in saved['reason']
    assert '.doc' in saved['reason']
    service = _service(app, tmp_path, database_url, soffice_path=binary)
    hit = next(item for item in service.search(project_id, '章程约定')['results'] if item['text'] == '甲公司章程约定注册资本。')
    assert hit['filenames'] == ['章程.doc']
    assert hit['locator'] == {'kind': 'docx_paragraph', 'paragraph': 1}
    assert 'PDF 第' not in hit['locator_label']
    assert '.docx' not in hit['source_url']
    with client(app) as http:
        denied = http.get(hit['source_url'].replace(f'/api/projects/{project_id}/', f'/api/projects/{other}/'))
        assert denied.status_code == 404
        assert '甲公司章程' not in denied.text
        assert service.search(other, '章程约定')['results'] == []
    with app.state.database.connect() as conn:
        foreign = conn.execute('SELECT reason FROM dd_agent.documents WHERE project_id=%s', (other,)).fetchall()
        assert foreign and all('LibreOffice' not in row['reason'] and '甲公司' not in row['reason'] for row in foreign)
        leaked = conn.execute('''SELECT count(*) AS n FROM dd_agent.source_unit_chunks c
            JOIN dd_agent.documents d ON d.id=c.document_id
            WHERE d.project_id=%s''', (other,)).fetchone()['n']
        assert leaked == 0
    # 之后找不到 soffice 时，撤下这一份已经入库的单元，不影响上面的隔离断言。
    withdrawn = _service(app, tmp_path, database_url, soffice_path=tmp_path / 'gone')
    outcome = withdrawn.index_office(project_id, document_id)
    assert outcome['unsupported'] is True
    assert outcome['error_code'] == 'soffice_missing'
    assert service.search(project_id, '章程约定')['results'] == []
    assert app.state.database.document(project_id, document_id)['status'] == 'unsupported'


@pytest.mark.parametrize(('name', 'body', 'query', 'text', 'locator'), [
    ('利润表.xls', workbook(), '营业收入', '营业收入资本', {'kind': 'xlsx_cell', 'sheet': '利润表', 'row': 1, 'column': 'A'}),
    ('方案.ppt', presentation(), '并购方案', '并购方案资本', {'kind': 'pptx_paragraph', 'slide': 1, 'shape': 1, 'paragraph': 1}),
])
def test_xls_and_ppt_reuse_modern_locators_and_keep_the_original_name(tmp_path, database_url, name, body, query, text, locator):
    binary = _fake(tmp_path, 'success')
    app = _app(tmp_path, database_url, soffice_path=binary, soffice_timeout_seconds=30)
    with client(app) as http:
        project_id = project(http)
        uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': (name, body, 'application/octet-stream')})
        document_id = uploaded.json()['document']['id']
    stored = Storage(tmp_path, 2_000_000).path(app.state.database.document(project_id, document_id)['storage_key'])
    service = _service(app, tmp_path, database_url, soffice_path=binary)
    service.index_office(project_id, document_id)
    assert stored.read_bytes() == body
    hit = next(item for item in service.search(project_id, query)['results'] if item['text'] == text)
    assert hit['filenames'] == [name]
    assert hit['locator'] == locator
    assert 'PDF 第' not in hit['locator_label']
    assert app.state.database.document(project_id, document_id)['status'] == 'registered'


def test_failed_legacy_job_does_not_sink_the_rest_of_the_batch(tmp_path, database_url):
    binary = _fake(tmp_path, 'fail')
    app = _app(tmp_path, database_url, soffice_path=binary, soffice_timeout_seconds=5)
    app.state.pipeline.evidence.models = TinyModels()
    app.state.jobs.wake = lambda: None
    with client(app) as http:
        project_id = project(http)
        legacy = http.post(f'/api/projects/{project_id}/documents', files={'file': ('坏.doc', word_document(), 'application/octet-stream')})
        modern = http.post(f'/api/projects/{project_id}/documents', files={'file': ('章程.docx', _docx(), 'application/octet-stream')})
        legacy_id = legacy.json()['document']['id']
        modern_id = modern.json()['document']['id']
        app.state.jobs.enqueue(project_id, legacy_id, wake=False)
        app.state.jobs.enqueue(project_id, modern_id, wake=False)
        app.state.jobs.drain()
    assert app.state.database.document(project_id, legacy_id)['status'] == 'unsupported'
    assert '转换失败' in app.state.database.document(project_id, legacy_id)['reason']
    with app.state.database.connect() as conn:
        legacy_job = conn.execute('SELECT status, error_code FROM dd_agent.processing_jobs WHERE document_id=%s', (legacy_id,)).fetchone()
        modern_job = conn.execute('SELECT status FROM dd_agent.processing_jobs WHERE document_id=%s', (modern_id,)).fetchone()
    assert legacy_job['status'] == 'failed' and legacy_job['error_code'] == 'legacy_failed'
    assert modern_job['status'] == 'completed'
    assert len([item for item in _logs(binary) if '--convert-to' in item['argv']]) == 1
    found = _service(app, tmp_path, database_url, soffice_path=binary).search(project_id, '章程约定')['results']
    assert any(hit['text'] == '甲公司章程约定注册资本。' for hit in found)
    assert all(str(hit['document_id']) != legacy_id for hit in found)


def test_reupload_after_soffice_appears_can_be_processed(tmp_path, database_url):
    body = word_document()
    first = _app(tmp_path, database_url)
    with client(first) as http:
        project_id = project(http)
        uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': ('章程.doc', body, 'application/octet-stream')})
        assert uploaded.json()['document']['status'] == 'unsupported'
        document_id = uploaded.json()['document']['id']
    binary = _fake(tmp_path, 'success')
    second = _app(tmp_path, database_url, soffice_path=binary, soffice_timeout_seconds=30)
    with client(second) as http:
        again = http.post(f'/api/projects/{project_id}/documents', files={'file': ('章程.doc', body, 'application/octet-stream')})
        assert again.json()['duplicate'] is True
        assert again.json()['document']['id'] == document_id
        assert again.json()['document']['status'] == 'registered'
    service = _service(second, tmp_path, database_url, soffice_path=binary)
    service.index_office(project_id, document_id)
    assert any(hit['filenames'] == ['章程.doc'] for hit in service.search(project_id, '章程约定')['results'])


def _docx():
    from docx import Document
    document = Document()
    document.add_paragraph('甲公司章程约定注册资本。')
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _real_soffice():
    candidates = [os.environ.get('DDA_SOFFICE_PATH', ''), shutil.which('soffice') or '', '/usr/bin/soffice', '/usr/lib/libreoffice/program/soffice']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _real_legacy_bytes(binary, extension, writer):
    with tempfile.TemporaryDirectory(prefix='dda-legacy-fixture-') as folder:
        folder = Path(folder)
        modern = folder / f'source{extension[0]}'
        target = extension[1]
        writer(modern)
        env = dict(os.environ)
        env['SAL_USE_VCLPLUGIN'] = 'svp'
        subprocess.run(
            [str(binary), '--headless', '--norestore', '--nofirststartwizard', '--convert-to', target,
             '--outdir', str(folder), str(modern)],
            check=False, timeout=180, capture_output=True, env=env)
        produced = list(folder.glob(f'source.{target}'))
        if len(produced) != 1:
            pytest.fail(f'已安装的 LibreOffice 不能把样例转成 {target}，无法做真实往返。')
        return produced[0].read_bytes()


def test_real_libreoffice_round_trip_keeps_the_original(tmp_path, database_url):
    binary = _real_soffice()
    if binary is None:
        pytest.skip('云端未安装 LibreOffice，跳过真实 soffice 转换。成功、超时、失败、密码或宏、找不到程序已由假进程覆盖。')
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation

    def write_doc(path):
        document = Document()
        document.add_paragraph('真实转换甲公司注册资本。')
        document.save(path)

    def write_xls(path):
        book = Workbook()
        book.active['A1'] = '真实转换营业收入'
        book.save(path)

    def write_ppt(path):
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        shape = slide.shapes.add_textbox(0, 0, 100, 100)
        shape.text_frame.text = '真实转换并购方案'
        deck.save(path)

    cases = [
        ('真实.doc', ('.docx', 'doc'), write_doc, '注册资本', '真实转换甲公司注册资本。'),
        ('真实.xls', ('.xlsx', 'xls'), write_xls, '营业收入', '真实转换营业收入'),
        ('真实.ppt', ('.pptx', 'ppt'), write_ppt, '并购方案', '真实转换并购方案'),
    ]
    app = _app(tmp_path, database_url, soffice_path=binary, soffice_timeout_seconds=180)
    service = _service(app, tmp_path, database_url, soffice_path=binary, soffice_timeout_seconds=180)
    with client(app) as http:
        project_id = project(http)
        for name, extensions, writer, query, text in cases:
            body = _real_legacy_bytes(binary, extensions, writer)
            uploaded = http.post(f'/api/projects/{project_id}/documents', files={'file': (name, body, 'application/octet-stream')})
            assert uploaded.status_code == 201, uploaded.text
            document_id = uploaded.json()['document']['id']
            stored = Storage(tmp_path, 2_000_000).path(app.state.database.document(project_id, document_id)['storage_key'])
            service.index_office(project_id, document_id)
            assert stored.read_bytes() == body
            hit = next(item for item in service.search(project_id, query)['results'] if text in item['text'])
            assert hit['filenames'] == [name]
            assert 'PDF 第' not in hit['locator_label']
            reason = app.state.database.document(project_id, document_id)['reason']
            assert 'LibreOffice' in reason and name.split('.')[-1] in reason
    assert not list(Path(tmp_path).rglob('*.docx'))
    assert not list(Path(tmp_path).rglob('*.xlsx'))
    assert not list(Path(tmp_path).rglob('*.pptx'))
