"""Actual PostgreSQL: bounded loans, transaction boundaries and session-lock ownership."""
from concurrent.futures import ThreadPoolExecutor
import importlib
from contextlib import nullcontext
from time import perf_counter, sleep

import psycopg
import pytest


def connections():
    try:
        return importlib.import_module('dd_agent.db_connections')
    except ModuleNotFoundError:
        pytest.fail('RG-05h shared connection provider is not implemented')


@pytest.mark.parametrize('pooled', [False, True])
def test_search_model_calls_do_not_hold_idle_database_transactions(tmp_path, database_url, pooled):
    from test_retrieval import setup
    _, p, d, service, models = setup(tmp_path, database_url)
    service.index(p, d, [1, 2])
    original_embed, original_rerank = models.embed, models.rerank
    observed = []
    def inspect():
        with psycopg.connect(database_url, autocommit=True) as monitor:
            count = monitor.execute("""SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND pid<>pg_backend_pid()
                AND state='idle in transaction'""").fetchone()[0]
        observed.append(count)
    def embed(texts, query=False):
        inspect(); return original_embed(texts, query=query)
    def rerank(query, texts):
        inspect(); return original_rerank(query, texts)
    models.embed, models.rerank = embed, rerank
    provider = connections().ConnectionProvider(database_url) if pooled else None
    with provider if pooled else nullcontext():
        if pooled:
            from dd_agent.db import Database
            service.database = Database(database_url, provider=provider)
        assert service.search(p, '注册资本')['results'][0]['page_number'] == 1
    assert observed == [0, 0], 'Model inference must run after all business transactions close'


def test_pool_reuses_sessions_but_rolls_back_failures_and_clears_session_locks(database_url):
    m = connections()
    with m.ConnectionProvider(database_url, max_size=1) as provider:
        with provider.connection() as conn:
            pid = conn.info.backend_pid
            conn.execute('CREATE TABLE pool_probe(value integer)')
        with pytest.raises(ValueError):
            with provider.connection() as conn:
                conn.execute('INSERT INTO pool_probe VALUES (1)')
                conn.execute('SELECT pg_advisory_lock(91234001)')
                conn.execute("SET application_name='temporary-pool-setting'")
                raise ValueError('rollback this operation')
        with provider.connection() as conn:
            assert conn.info.backend_pid == pid
            assert conn.execute('SELECT count(*) FROM pool_probe').fetchone()['count'] == 0
            assert conn.execute("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'").fetchone()['count'] == 0
            assert conn.execute('SHOW application_name').fetchone()['application_name'] == 'dd-agent'
        assert provider.stats()['connections_num'] == 1
    with pytest.raises(m.DatabaseBusy):
        with provider.connection(): pass


def test_pool_has_bounded_wait_and_queue_and_recovers_after_saturation(database_url):
    m = connections()
    with m.ConnectionProvider(database_url, max_size=1, timeout=.2, max_waiting=1) as provider:
        with provider.connection():
            with ThreadPoolExecutor(max_workers=1) as workers:
                def blocked():
                    started = perf_counter()
                    with pytest.raises(m.DatabaseBusy):
                        with provider.connection(): pass
                    return perf_counter()-started
                pending = workers.submit(blocked)
                deadline = perf_counter()+2
                while provider.stats().get('requests_waiting', 0) != 1 and perf_counter() < deadline:
                    sleep(.005)
                assert provider.stats().get('requests_waiting', 0) == 1
                with pytest.raises(m.DatabaseBusy):
                    with provider.connection(): pass
                assert .1 <= pending.result(timeout=2) < 1.5
        # 官方池异步执行归还重置；有限等待回收，不把瞬时 busy 当永久不可用。
        deadline = perf_counter()+2
        while provider.stats().get('pool_available', 0) != 1 and perf_counter() < deadline:
            sleep(.005)
        assert provider.stats().get('pool_available', 0) == 1
        with provider.connection() as conn:
            assert conn.execute('SELECT 42 AS n').fetchone()['n'] == 42
        assert provider.stats()['pool_size'] <= 1


def test_queue_lease_is_not_a_pool_loan_and_survives_pool_resets(database_url):
    m = connections()
    from dd_agent.db import Database
    with m.ConnectionProvider(database_url, max_size=1) as provider:
        db = Database(database_url, provider=provider)
        with db.lease() as lease:
            lease.execute('SELECT pg_advisory_lock(91234002)'); lease.commit()
            with db.connect() as ordinary:
                assert ordinary.info.backend_pid != lease.info.backend_pid
            with db.connect() as ordinary:
                assert not ordinary.execute('SELECT pg_try_advisory_lock(91234002) AS acquired').fetchone()['acquired']
        with db.connect() as ordinary:
            assert ordinary.execute('SELECT pg_try_advisory_lock(91234002) AS acquired').fetchone()['acquired']


def test_web_services_share_one_provider_and_it_can_restart_cleanly(tmp_path, database_url):
    connections()
    from test_workspace import application, client
    app = application(tmp_path, database_url, migrate=True)
    db = app.state.database
    with client(app) as c:
        assert c.get('/api/health').json()['status'] == 'ready'
        pipeline = app.state.pipeline
        assert app.state.evidence.database is db
        assert app.state.review.database is db
        assert app.state.automatic.database is db
        assert pipeline.reader.database is pipeline.automatic.database is pipeline.evidence.database is db
        assert pipeline.automatic.review.database is db
        assert app.state.jobs.database is db
        assert db.provider.active
    assert not db.provider.active
    # Legacy direct use outside the app scope remains a short, unpooled connection.
    assert db.health()['status'] == 'ready'
    with client(app) as c:
        assert c.get('/api/health').json()['status'] == 'ready'


def test_closed_or_broken_loans_are_discarded_without_breaking_future_requests(database_url):
    m = connections()
    with m.ConnectionProvider(database_url, max_size=1) as provider:
        with provider.connection() as conn:
            old_pid = conn.info.backend_pid; conn.close()
        with provider.connection() as conn:
            assert conn.info.backend_pid != old_pid
            assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1


def test_provider_close_keeps_inflight_loan_alive_until_return(database_url):
    m = connections()
    provider = m.ConnectionProvider(database_url)
    provider.start()
    with provider.connection() as conn:
        provider.close()
        assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1
    with pytest.raises(m.DatabaseBusy):
        with provider.connection(): pass


def test_shutdown_preserves_inflight_background_checkpoint_after_pool_close(tmp_path, database_url):
    from threading import Event
    from test_workspace import application, client, project, upload
    app = application(tmp_path, database_url, migrate=True)
    entered, release = Event(), Event()
    class Work:
        def run(self, project_id, document_id, pages, checkpoint=None):
            entered.set()
            assert release.wait(timeout=10)
            checkpoint('embedding', 1)
    app.state.jobs.factory = Work
    try:
        with client(app) as c:
            p = project(c); d = upload(c, p).json()['document']['id']
            app.state.database.set_reading(d, 1, 'synthetic')
            app.state.jobs.enqueue(p, d)
            assert entered.wait(timeout=5)
        assert not app.state.database.provider.active
        release.set(); app.state.jobs._thread.join(timeout=5)
        result = app.state.jobs.latest(p, d)
        assert result['status'] == 'paused'
        assert result['progress']['recovery']['reason'] == 'service_stopped'
    finally:
        release.set(); app.state.jobs.close()


def test_saturated_pool_returns_retryable_api_status(tmp_path, database_url):
    from dd_agent.app import create_app
    from dd_agent.config import Settings
    from test_workspace import client
    settings = Settings(database_url=database_url, data_dir=tmp_path, db_pool_size=1, db_pool_timeout=.1)
    app = create_app(settings); app.state.database.migrate()
    with client(app) as c:
        with app.state.database.connect():
            result = c.get('/api/projects')
        assert result.status_code == 503
        assert '繁忙' in result.json()['detail']
        assert database_url not in result.text


def test_cli_scope_closes_all_of_its_connections(tmp_path, database_url):
    import os
    import subprocess
    import sys
    from dd_agent.db import Database
    Database(database_url).migrate()
    env = {**os.environ, 'DDA_DATABASE_URL': database_url, 'DDA_DATA_DIR': str(tmp_path),
           'PYTHONIOENCODING': 'utf-8'}
    result = subprocess.run([sys.executable, '-m', 'dd_agent', 'check'], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stdout
    with psycopg.connect(database_url) as monitor:
        assert monitor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND application_name='dd-agent'").fetchone()[0] == 0


def test_pooled_index_rejects_source_changed_during_embedding(tmp_path, database_url):
    from test_retrieval import setup
    from dd_agent.db import Database
    app, p, d, service, models = setup(tmp_path, database_url)
    original = models.embed
    with connections().ConnectionProvider(database_url, max_size=1) as provider:
        service.database = Database(database_url, provider=provider)
        def changed(texts, query=False):
            # Separate session can commit while inference is running: no page lock is held.
            app.state.database.save_page(d, 'scan-v1', dict(page_number=1, status='succeeded',
                method='ocr', text='注册资本已变更。'))
            return original(texts, query=query)
        models.embed = changed
        assert service.index(p, d, [1])['failed_pages'] == [1]
        with service.database.connect() as conn:
            assert conn.execute('SELECT count(*) AS n FROM dd_agent.indexed_pages').fetchone()['n'] == 0
        models.embed = original
        assert service.index(p, d, [1])['indexed_pages'] == [1]


def test_failed_pooled_publication_preserves_previous_complete_page(tmp_path, database_url):
    from test_retrieval import setup
    from dd_agent.db import Database
    app, p, d, service, _ = setup(tmp_path, database_url)
    with connections().ConnectionProvider(database_url, max_size=1) as provider:
        service.database = Database(database_url, provider=provider)
        service.index(p, d, [1])
        snapshot = app.state.database.evidence_pages(d)[0]
        with service.database.connect() as conn:
            before = conn.execute('SELECT id,text FROM dd_agent.evidence_chunks').fetchall()
            conn.execute("ALTER TABLE dd_agent.evidence_chunks ADD CONSTRAINT reject_probe CHECK(text <> 'reject')")
        with pytest.raises(psycopg.errors.CheckViolation):
            service.publish(d, snapshot, service.version(),
                [dict(start_offset=0, end_offset=6, text='reject')], [[1.] + [0.] * 1023])
        with service.database.connect() as conn:
            assert conn.execute('SELECT id,text FROM dd_agent.evidence_chunks').fetchall() == before
            assert conn.execute('SELECT count(*) AS n FROM dd_agent.indexed_pages').fetchone()['n'] == 1
