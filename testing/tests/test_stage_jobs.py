"""RG-05c: real transactions and source fencing; no model workers involved."""
from concurrent.futures import ThreadPoolExecutor
import importlib
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from dd_agent.pipeline import JobManager
from test_retrieval import setup


def repository(database):
    try:
        return importlib.import_module('dd_agent.stage_jobs').StageJobs(database)
    except ModuleNotFoundError:
        pytest.fail('RG-05c persistent stage repository is missing')


@pytest.fixture
def work(tmp_path, database_url):
    app, p, d, evidence, _ = setup(tmp_path, database_url)
    manager = JobManager(evidence.settings, database=app.state.database)
    job = manager.enqueue(p, d, wake=False)
    return app.state.database, manager, p, d, job


def ensure(repo, job, number=1, policy='vision-v1', **kwargs):
    with repo.database.connect() as conn:
        page = conn.execute('SELECT raw_revision FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=%s',
            (job['document_id'],number)).fetchone()
    return repo.ensure(job['id'], 'vision', {'pages': [number]}, source_revision='source-test',
                       policy_version=policy, dependencies=[dict(page_number=number,view='raw',revision=page['raw_revision'])], **kwargs)


def test_enqueue_creates_persistent_preflight_and_source_events(work):
    db, manager, p, d, job = work
    repo = repository(db)
    with db.connect() as conn:
        rows = conn.execute('SELECT * FROM dd_agent.stage_jobs WHERE job_id=%s', (job['id'],)).fetchall()
        assert len(rows) == 1 and rows[0]['stage'] == 'preflight'
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.page_outbox WHERE document_id=%s', (d,)).fetchone()['n'] == 2
    result = repo.reconcile(job['id'], {'vision': 'vision-v1'}, limit=1)
    assert result['scanned'] == 1 and result['created'] == 1
    # Another repository/process can continue after the writer disappeared.
    assert repository(db).reconcile(job['id'], {'vision': 'vision-v1'})['created'] == 1
    assert repo.reconcile(job['id'], {'vision': 'vision-v1'})['created'] == 0


def test_page_and_outbox_share_commit_and_rollback(work):
    db, _, _, d, _ = work
    repository(db)
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            db.save_page(d, 'scan-v1', dict(page_number=3, status='succeeded', method='ocr', text='must roll back'), conn=conn)
            raise RuntimeError('rollback')
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.document_pages WHERE document_id=%s AND page_number=3', (d,)).fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.page_outbox WHERE document_id=%s AND page_number=3', (d,)).fetchone()['n'] == 0
        raw = conn.execute('SELECT raw_revision FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=1', (d,)).fetchone()['raw_revision']
        conn.execute('INSERT INTO dd_agent.machine_pages(id,document_id,page_number,base_revision,profile_version,result) VALUES (%s,%s,1,%s,%s,%s)',
            (uuid4(), d, raw, 'vision-v1', Jsonb(dict(text='机器文字', verification='machine_checked'))))
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM dd_agent.page_outbox WHERE document_id=%s AND stage='embedding'", (d,)).fetchone()['n'] == 1


def test_unique_claims_dependency_waiting_and_idempotent_ensure(work):
    db, _, _, _, job = work
    repo = repository(db)
    a = ensure(repo, job); b = ensure(repo, job, 2)
    assert ensure(repo, job)['id'] == a['id']
    dependent = repo.ensure(job['id'], 'embedding', {'pages': [1]}, source_revision='s',
        policy_version='embedding-v1', depends_on=[a['id']], dependencies=[dict(page_number=1,view='effective',
            revision=db.page_row(job['document_id'],1)['source_revision'])])
    assert repo.claim(job['id'], stage='embedding') is None
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: repository(db).claim(job['id'], stage='vision'), range(2)))
    assert {r['id'] for r in claims} == {a['id'], b['id']}
    assert repo.claim(job['id'], stage='vision') is None
    owner = next(r for r in claims if r['id'] == a['id'])
    assert repo.complete(owner['id'], owner['owner_token'], result_ref='page-1')
    assert repo.claim(job['id'], stage='embedding')['id'] == dependent['id']


def test_retry_is_bounded_and_old_owner_cannot_publish(work):
    db, _, _, _, job = work
    repo = repository(db); task = ensure(repo, job)
    first = repo.claim(job['id'], stage='vision')
    assert repo.fail(first['id'], first['owner_token'], category='transient', error_code='worker_failed', retry_after=0)
    second = repo.claim(job['id'], stage='vision')
    assert second['owner_token'] != first['owner_token'] and second['attempts'] == 2
    assert not repo.complete(task['id'], first['owner_token'], publish=lambda _: pytest.fail('old owner published'))
    assert repo.fail(second['id'], second['owner_token'], category='transient', error_code='worker_failed', retry_after=0)
    third = repo.claim(job['id'], stage='vision')
    assert repo.fail(third['id'], third['owner_token'], category='transient', error_code='worker_failed', retry_after=0)
    assert repo.claim(job['id'], stage='vision') is None
    with db.connect() as conn:
        assert conn.execute('SELECT state FROM dd_agent.stage_jobs WHERE id=%s', (task['id'],)).fetchone()['state'] == 'failed'
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.stage_attempts WHERE stage_job_id=%s', (task['id'],)).fetchone()['n'] == 3


def test_changed_source_fences_publication_and_creates_new_work(work):
    db, _, _, d, job = work
    repo = repository(db)
    repo.reconcile(job['id'], {'vision': 'vision-v1'})
    task = repo.claim(job['id'], stage='vision')
    n = task['scope']['pages'][0]
    db.save_page(d, 'scan-v1', dict(page_number=n, status='succeeded', method='ocr', text='new revision'))
    assert not repo.complete(task['id'], task['owner_token'], publish=lambda _: pytest.fail('stale source published'))
    assert repo.reconcile(job['id'], {'vision': 'vision-v1'})['created'] == 1


def test_resume_advances_epoch_fences_old_owner_and_reuses_success(work):
    db, manager, p, d, job = work
    repo = repository(db)
    done = ensure(repo, job)
    claim = repo.claim(job['id'], stage='vision')
    assert repo.complete(done['id'], claim['owner_token'], result_ref='saved-page')
    pending = ensure(repo, job, 2); old = repo.claim(job['id'], stage='vision')
    manager.pause(p, d)
    assert repo.claim(job['id']) is None
    resumed = manager.resume(p, d, wake=False)
    assert resumed['run_epoch'] == job['run_epoch'] + 1
    assert not repo.complete(pending['id'], old['owner_token'], publish=lambda _: pytest.fail('old epoch published'))
    reused = ensure(repo, resumed)
    assert reused['id'] == done['id'] and reused['reused']
    new = ensure(repo, resumed, 2)
    assert new['id'] != pending['id'] and new['run_epoch'] == resumed['run_epoch']


def test_publication_failure_rolls_back_and_can_be_retried(work):
    db, _, _, _, job = work
    repo = repository(db); task = ensure(repo, job)
    claim = repo.claim(job['id'], stage='vision')
    def broken(conn):
        conn.execute('CREATE TABLE dd_agent.stage_atomic_probe(n integer)')
        raise RuntimeError('failed publication')
    with pytest.raises(RuntimeError): repo.complete(task['id'], claim['owner_token'], publish=broken)
    with db.connect() as conn:
        assert conn.execute("SELECT to_regclass('dd_agent.stage_atomic_probe') AS t").fetchone()['t'] is None
    assert repo.complete(task['id'], claim['owner_token'])


def test_missing_policy_does_not_advance_outbox_cursor(work):
    db, _, _, _, job = work
    repo = repository(db)
    assert repo.reconcile(job['id'], {})['waiting_for_policy']
    assert repo.reconcile(job['id'], {'vision': 'v1'})['created'] == 2


def test_outbox_does_not_skip_lower_id_that_commits_later(work):
    db, _, _, d, job = work
    repo = repository(db)
    repo.reconcile(job['id'], {'vision': 'v1'})
    with db.connect() as slow:
        db.save_page(d, 'scan-v1', dict(page_number=1, status='succeeded', method='ocr', text='slow commit'), conn=slow)
        db.save_page(d, 'scan-v1', dict(page_number=2, status='succeeded', method='ocr', text='fast commit'))
        assert repo.reconcile(job['id'], {'vision': 'v1'})['created'] == 1
    # Identity allocation order is not transaction commit order.
    assert repo.reconcile(job['id'], {'vision': 'v1'})['created'] == 1


def test_page_stages_require_complete_source_dependencies(work):
    db, _, _, _, job = work
    repo = repository(db)
    with pytest.raises(ValueError):
        repo.ensure(job['id'], 'vision', {'pages':[1]}, source_revision='s', policy_version='v')


def test_predecessor_policy_change_replaces_waiting_task(work):
    db, _, _, d, job = work
    repo = repository(db)
    with db.connect() as conn:
        page = conn.execute('SELECT raw_revision,source_revision FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=1', (d,)).fetchone()
    deps = [dict(page_number=1,view='effective',revision=page['source_revision'])]
    a = ensure(repo, job)
    first = repo.ensure(job['id'],'embedding',{'pages':[1]},source_revision='same',policy_version='embed',dependencies=deps,depends_on=[a['id']])
    b = ensure(repo, job, policy='new-vision')
    second = repo.ensure(job['id'],'embedding',{'pages':[1]},source_revision='same',policy_version='embed',dependencies=deps,depends_on=[b['id']])
    assert second['id'] != first['id']


def test_migration_rebuilds_missing_legacy_events_idempotently(work):
    db, _, _, d, _ = work
    repository(db)
    with db.connect() as conn:
        conn.execute('DELETE FROM dd_agent.page_outbox WHERE document_id=%s', (d,))
    db.migrate(); db.migrate()
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.page_outbox WHERE document_id=%s', (d,)).fetchone()['n'] == 2


def test_two_saves_in_one_transaction_have_distinct_source_revisions(work):
    db, _, _, d, _ = work
    repository(db)
    with db.connect() as conn:
        revisions=[]
        for text in ('first', 'second'):
            db.save_page(d,'scan-v1',dict(page_number=1,status='succeeded',method='ocr',text=text),conn=conn)
            revisions.append(conn.execute('SELECT raw_revision FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=1',(d,)).fetchone()['raw_revision'])
        assert revisions[0] != revisions[1]


@pytest.mark.parametrize('operation',['save','checkpoint'])
def test_old_parent_epoch_cannot_overwrite_new_run(work, operation):
    from dd_agent.control import ProcessingStopped
    db, manager, p, d, old = work
    repository(db)
    manager.pause(p,d); manager.resume(p,d,wake=False)
    if operation=='save':
        manager._save(old,'failed',{'stale':True})
        latest=manager.latest(p,d)
        assert latest['status']=='queued' and 'stale' not in latest['progress']
    else:
        with pytest.raises(ProcessingStopped): manager._checkpoint(old,{},'reading',1)


def test_enqueue_locks_existing_parent_before_seeding(work, monkeypatch):
    import psycopg
    db, manager, p, d, job = work
    original = manager.stages.seed
    def inspect(job_id, *, conn=None):
        # Completion must not slip between selecting an active parent and seeding its stage.
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with db.connect() as competitor:
                competitor.execute("SET LOCAL lock_timeout='50ms'")
                competitor.execute("UPDATE dd_agent.processing_jobs SET status='completed' WHERE id=%s",(job_id,))
        return original(job_id,conn=conn)
    monkeypatch.setattr(manager.stages,'seed',inspect)
    assert manager.enqueue(p,d,wake=False)['id']==job['id']
