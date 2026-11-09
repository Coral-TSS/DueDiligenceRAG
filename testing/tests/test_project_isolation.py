"""项目 A/B 同时有数据时，检索、补读、缓存、MCP 和任务不能串项目。

合成夹具只替换模型推理。断点是：漏掉 project 条件后，对方正文或材料标识出现在结果里。
"""
import pytest

from dd_agent.config import Settings
from dd_agent.mcp_server import create_mcp_app
from dd_agent.automatic import AutomaticError
from dd_agent.pipeline import JobManager
from test_mcp_server import KEY, _Session, _initialize, _payload, _running
from test_parent_index import Budget
from test_retrieval import TinyModels
from test_workspace import PDF, application, client, project, upload


MARKER_A = '甲项独有注册资本甲甲。'
MARKER_B = '乙项独有注册资本乙乙。'


def _models():
    models = TinyModels()
    models.cache_profile = lambda kind: models.version + '-' + kind
    return models


def workspace(tmp_path, database_url):
    app = application(tmp_path, database_url, migrate=True)
    models = _models()
    with client(app) as http:
        project_a = project(http, '项目甲')
        project_b = project(http, '项目乙')
    return app, models, project_a, project_b


def add_document(app, project_id, name, body, page_text):
    with client(app) as http:
        stored = upload(http, project_id, name, body)
        assert stored.status_code == 201, stored.text
        document_id = stored.json()['document']['id']
    database = app.state.database
    database.set_reading(document_id, 1, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(
        page_number=1, status='succeeded', method='text', text=page_text))
    return document_id


def index_page(app, tmp_path, database_url, models, project_id, document_id):
    from dd_agent.retrieval import EvidenceService
    service = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models, database=app.state.database)
    assert service.index(project_id, document_id, [1])['indexed_pages'] == [1]
    return service


def search_ids(service, project_id, query):
    result = service.search(project_id, query)
    return {str(hit['document_id']) for hit in result['results']}, result


def test_same_filename_with_different_bytes_stays_inside_each_project(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    document_a = add_document(app, project_a, '合同.pdf', PDF + '\n甲'.encode(), MARKER_A)
    document_b = add_document(app, project_b, '合同.pdf', PDF + '\n乙'.encode(), MARKER_B)
    service = index_page(app, tmp_path, database_url, models, project_a, document_a)
    index_page(app, tmp_path, database_url, models, project_b, document_b)
    with client(app) as http:
        listed = http.get(f'/api/projects/{project_a}/material-catalog').json()['items']
        assert [item['document_id'] for item in listed] == [document_a]
        assert listed[0]['filenames'] == ['合同.pdf']
        denied = http.get(f'/api/projects/{project_b}/documents/{document_a}/file')
        assert denied.status_code == 404
        assert MARKER_A not in denied.text and 'storage_key' not in denied.text
    found, result_a = search_ids(service, project_a, '甲项独有')
    assert found == {document_a}
    assert MARKER_B not in result_a['results'][0]['text']
    found_b, result_b = search_ids(service, project_b, '甲项独有')
    assert document_a not in found_b
    assert all(MARKER_A not in hit['text'] for hit in result_b['results'])


def test_identical_bytes_keep_separate_rows_files_and_vectors(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    shared = '相同正文注册资本丙丙。'
    document_a = add_document(app, project_a, '副本.pdf', PDF, shared)
    document_b = add_document(app, project_b, '副本.pdf', PDF, shared)
    assert document_a != document_b
    bins = list(tmp_path.rglob('*.bin'))
    assert len(bins) == 2
    assert {path.parent.name for path in bins} == {project_a, project_b}
    service = index_page(app, tmp_path, database_url, models, project_a, document_a)
    index_page(app, tmp_path, database_url, models, project_b, document_b)
    with app.state.database.connect() as conn:
        rows = conn.execute(
            'SELECT document_id::text AS document_id, count(*) AS n FROM dd_agent.evidence_chunks GROUP BY document_id'
        ).fetchall()
    counts = {row['document_id']: row['n'] for row in rows}
    assert counts[document_a] >= 1 and counts[document_b] >= 1
    found, result = search_ids(service, project_a, '丙丙')
    assert found == {document_a}
    assert shared in result['results'][0]['text']
    found_b, _ = search_ids(service, project_b, '丙丙')
    assert found_b == {document_b}


def test_vector_keyword_and_hybrid_return_only_the_queried_project(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    document_a = add_document(app, project_a, '甲.pdf', PDF + b'\nA', MARKER_A)
    document_b = add_document(app, project_b, '乙.pdf', PDF + b'\nB', MARKER_B)
    index_page(app, tmp_path, database_url, models, project_a, document_a)
    index_page(app, tmp_path, database_url, models, project_b, document_b)
    from dd_agent.retrieval import EvidenceService
    for mode in ('vector', 'keyword', 'hybrid'):
        service = EvidenceService(
            Settings(database_url=database_url, data_dir=tmp_path,
                     search_index_mode='page', retrieval_mode=mode),
            models=models, database=app.state.database)
        found, result = search_ids(service, project_a, '注册资本')
        assert found == {document_a}, mode
        assert MARKER_B not in result['results'][0]['text']
        texts = [row['text'] for row in result['results']]
        assert all(MARKER_B not in text for text in texts)


def test_mixed_and_parent_search_omit_the_other_projects_text(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    document_a = add_document(app, project_a, '甲.pdf', PDF + b'\nA', MARKER_A + '甲' * 80)
    document_b = add_document(app, project_b, '乙.pdf', PDF + b'\nB', MARKER_B + '乙' * 80)
    # 第二页让父块可以封口。两页都含各自标记。
    for project_id, document_id, marker in (
            (project_a, document_a, MARKER_A), (project_b, document_b, MARKER_B)):
        app.state.database.set_reading(document_id, 2, 'scan-v1')
        app.state.database.save_page(document_id, 'scan-v1', dict(
            page_number=2, status='succeeded', method='text', text=marker + '续页说明股东。'))
    index_page(app, tmp_path, database_url, models, project_a, document_a)
    # index_page only sends page 1; index page 2 as well.
    from dd_agent.retrieval import EvidenceService
    page_service = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models, database=app.state.database)
    assert page_service.index(project_a, document_a, [2])['failed_pages'] == []
    assert page_service.index(project_b, document_b, [1, 2])['failed_pages'] == []
    _publish_parent(app.state.database, page_service.settings, project_a, document_a, models)
    _publish_parent(app.state.database, page_service.settings, project_b, document_b, models)
    mixed = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='mixed'),
        models=models, database=app.state.database)
    found, result = search_ids(mixed, project_a, '甲项独有')
    assert found == {document_a}
    assert all(MARKER_B not in hit.get('text', '') for hit in result['results'])
    from dd_agent.parent_search import ParentSearch
    from dd_agent.parent_index import ParentIndex
    parent = ParentSearch(mixed, ParentIndex(mixed.settings, database=app.state.database, models=models, budget=Budget()))
    parent_ids = {str(hit['document_id']) for hit in parent.search(project_b, '注册资本')['results']}
    assert document_a not in parent_ids
    joined = ' '.join(hit.get('context', {}).get('text', '') for hit in parent.search(project_b, '注册资本')['results'])
    assert MARKER_A not in joined


def _publish_parent(database, settings, project_id, document_id, models):
    from dd_agent.assembly import assemble
    from dd_agent.assembly_store import AssemblyStore
    from dd_agent.parent_index import ParentIndex
    snapshots = AssemblyStore(database).snapshots(project_id, document_id, [1, 2], ready_pages={1, 2})
    parents = assemble(snapshots, final=True)['parents']
    assert parents
    index = ParentIndex(settings, database=database, models=models, budget=Budget())
    plan = index.prepare(project_id, document_id, parents, snapshots)
    manager = JobManager(settings, database=database)
    job = manager.enqueue(project_id, document_id, wake=False)
    task = index.register(manager.stages, job['id'], plan)
    claim = manager.stages.claim(job['id'], 'embedding', task_id=task['id'])
    index.publish(manager.stages, claim['id'], claim['owner_token'], index.embed(plan))
    return plan


def test_foreign_page_parent_and_catalog_reads_hide_the_body(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    document_a = add_document(app, project_a, '甲.pdf', PDF + b'\nA', MARKER_A + '甲' * 40)
    app.state.database.set_reading(document_a, 2, 'scan-v1')
    app.state.database.save_page(document_a, 'scan-v1', dict(
        page_number=2, status='succeeded', method='text', text='甲项续页。'))
    from dd_agent.retrieval import EvidenceService
    service = EvidenceService(
        Settings(database_url=database_url, data_dir=tmp_path, search_index_mode='page'),
        models=models, database=app.state.database)
    service.index(project_a, document_a, [1, 2])
    plan = _publish_parent(app.state.database, service.settings, project_a, document_a, models)
    with client(app) as http:
        page = http.get(f'/api/projects/{project_b}/documents/{document_a}/pages/1')
        assert page.status_code == 404
        assert MARKER_A not in page.text
        image = http.get(f'/api/projects/{project_b}/documents/{document_a}/pages/1/image')
        assert image.status_code == 404 and MARKER_A not in image.text
        catalog = http.get(f'/api/projects/{project_b}/material-catalog', params={'after': document_a})
        assert catalog.status_code == 404 and document_a not in catalog.text
        parent = http.get(
            f'/api/projects/{project_b}/documents/{document_a}/parent-builds/{plan["id"]}/parents/{plan["parents"][0]["id"]}',
            params={'index_version': _parent_version(service)})
        assert parent.status_code == 404
        assert MARKER_A not in parent.text
    from dd_agent.parent_references import ParentReferenceError, ParentReferences
    with pytest.raises(ParentReferenceError) as caught:
        ParentReferences(app.state.database).read(
            project_b, document_a, plan['id'], plan['parents'][0]['id'], _parent_version(service))
    assert MARKER_A not in str(caught.value)


def _parent_version(service):
    from dd_agent.parent_index import ParentIndex
    return ParentIndex(service.settings, database=service.database, models=service.models, budget=Budget()).version()


def test_cached_query_does_not_return_or_merge_the_other_projects_chunks(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    shared = '缓存边界注册资本丁丁。'
    document_a = add_document(app, project_a, '甲.pdf', PDF + b'\nA', shared + '甲侧')
    document_b = add_document(app, project_b, '乙.pdf', PDF + b'\nB', shared + '乙侧')
    service = index_page(app, tmp_path, database_url, models, project_a, document_a)
    index_page(app, tmp_path, database_url, models, project_b, document_b)
    calls = {'n': 0}
    original = models.embed
    def counting(texts, query=False):
        calls['n'] += 1
        return original(texts, query=query)
    models.embed = counting
    assert search_ids(service, project_a, '丁丁')[0] == {document_a}
    warmed = calls['n']
    assert search_ids(service, project_a, '丁丁')[0] == {document_a}
    assert calls['n'] == warmed
    found, result = search_ids(service, project_b, '丁丁')
    assert found == {document_b}
    assert '乙侧' in result['results'][0]['text'] and '甲侧' not in result['results'][0]['text']
    with app.state.database.connect() as conn:
        rows = conn.execute(
            'SELECT document_id::text AS document_id, count(*) AS n FROM dd_agent.evidence_chunks GROUP BY document_id'
        ).fetchall()
    counts = {row['document_id']: row['n'] for row in rows}
    assert counts[document_a] >= 1 and counts[document_b] >= 1


def test_mcp_without_project_or_with_foreign_document_returns_no_evidence(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    document_a = add_document(app, project_a, '甲.pdf', PDF + b'\nA', MARKER_A)
    document_b = add_document(app, project_b, '乙.pdf', PDF + b'\nB', MARKER_B)
    service = index_page(app, tmp_path, database_url, models, project_a, document_a)
    index_page(app, tmp_path, database_url, models, project_b, document_b)
    mcp = create_mcp_app(Settings(database_url=database_url, data_dir=tmp_path, mcp_api_key=KEY))
    mcp.state.evidence = service
    mcp.state.database = app.state.database
    with _running(mcp) as base:
        session = _Session(base, {'Authorization': f'Bearer {KEY}'})
        try:
            _initialize(session)
            missing = session.call('tools/call', {'name': 'search_evidence', 'arguments': {'query': '注册资本'}}, 2)
            assert missing['result']['isError'] is True
            assert MARKER_A not in missing['result']['content'][0]['text']
            foreign = session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': project_a, 'query': '注册资本', 'document_id': document_b}}, 3)
            assert foreign['result']['isError'] is True
            assert MARKER_B not in foreign['result']['content'][0]['text']
            own = _payload(session.call('tools/call', {'name': 'search_evidence', 'arguments': {
                'project_id': project_b, 'query': '乙项独有'}}, 4))
            assert {row['document_id'] for row in own['results']} == {document_b}
        finally:
            session.close()


def test_queued_job_is_invisible_and_not_controllable_from_the_other_project(tmp_path, database_url):
    app, models, project_a, project_b = workspace(tmp_path, database_url)
    document_a = add_document(app, project_a, '甲.pdf', PDF + b'\nA', MARKER_A)
    document_b = add_document(app, project_b, '乙.pdf', PDF + b'\nB', MARKER_B)
    manager = JobManager(Settings(database_url=database_url, data_dir=tmp_path), database=app.state.database)
    job = manager.enqueue(project_a, document_a, wake=False)
    app.state.jobs.wake = lambda: None
    with client(app) as http:
        denied = http.post(f'/api/projects/{project_b}/documents/{document_a}/processing/pause')
        assert denied.status_code == 404 and MARKER_A not in denied.text
        assert http.post(f'/api/projects/{project_b}/documents/{document_a}/processing/resume').status_code == 404
        assert http.post(
            f'/api/projects/{project_b}/documents/{document_a}/processing/stages/reading/pause').status_code == 404
        own = http.get(f'/api/projects/{project_b}/documents/{document_b}/processing')
        assert own.status_code == 200
        assert own.json() in (None, {}) or own.json().get('id') != str(job['id'])
    assert manager.latest(project_a, document_a)['status'] == 'queued'
    with pytest.raises(AutomaticError):
        manager.latest(project_b, document_a)
