"""Real, isolated PostgreSQL. Never use the application's database URL."""
import os
from pathlib import Path
import socket
import subprocess
import uuid

import psycopg
import pytest
from testing.support.database_lifecycle import IsolatedDatabase


_cluster_alive={}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item,call):
    result=yield
    report=result.get_result()
    reports=getattr(item,'_dda_reports',{})
    reports[report.when]=report;item._dda_reports=reports
    owned=getattr(item,'_dda_database',None)
    if report.when!='teardown' or owned is None:return
    database,cluster=owned
    # Run after *all* application/worker/pool fixture finalizers. Test failure
    # evidence wins over cleanup, including setup and teardown failures.
    outcome='failed' if any(r.failed for r in reports.values()) else (
        'skipped' if reports.get('call') and reports['call'].skipped else
        'passed' if reports.get('call') and reports['call'].passed else 'unknown')
    try:
        action=database.finish(outcome,cluster_running=_cluster_alive.get(cluster,False))
        if action=='retained_error':
            report.outcome='failed'
            report.longrepr='Isolated database cleanup failed; retained for diagnosis: '+database.name
    except Exception as exc:
        # A cleanup error must be visible, not an INTERNALERROR hiding the test.
        report.outcome='failed'
        report.longrepr='Isolated database cleanup failed ('+type(exc).__name__+'); database retained'


@pytest.fixture(scope="session")
def postgres():
    binaries = Path(os.environ.get("DDA_TEST_POSTGRES_BIN", r"D:\Apps\PostgreSQL\17\bin"))
    assert (binaries / "initdb.exe").exists(), "Set DDA_TEST_POSTGRES_BIN to PostgreSQL bin"
    run_root = Path(os.environ.get('DDA_TEST_RUN_DIR', Path(__file__).resolve().parents[2] / 'testing/runs/current'))
    root = run_root / 'postgres' / uuid.uuid4().hex
    root.mkdir(parents=True)
    data = root / "pgdata"
    def run(name, *args):
        # Windows server children inherit pipes; use a file so start can return.
        output = root / (name + ".log")
        with output.open("w", encoding="utf-8") as stream:
            # Shutdown can follow an in-progress checkpoint and another full sync.
            # Keep durability on; increase only this isolated-fixture wait budget.
            result = subprocess.run([str(binaries / name), *map(str, args)], stdout=stream, stderr=stream, timeout=330 if 'stop' in args else 150)
        assert result.returncode == 0, output.read_text(encoding="utf-8", errors="replace")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    run("initdb.exe", "-D", data, "-U", "postgres", "-A", "trust", "--encoding=UTF8", "--locale=C")
    run("pg_ctl.exe", "-D", data, "-l", root / "server.log", "-o", f"-h 127.0.0.1 -p {port}", "-w", "start")
    cluster=f"host=127.0.0.1 port={port} user=postgres"
    _cluster_alive[cluster]=True
    try:
        yield cluster
    finally:
        # Many isolated databases can need >60s to sync on this Windows disk.
        try:run("pg_ctl.exe", "-D", data, "-m", "fast", "-t", "300", "-w", "stop")
        finally:_cluster_alive[cluster]=False


@pytest.fixture
def database_url(postgres,request):
    # A fresh database per test also exercises migration from an empty database.
    run_root=Path(os.environ.get('DDA_TEST_RUN_DIR',Path(__file__).resolve().parents[1]/'runs/current'))
    database=IsolatedDatabase(postgres,run_root/'database-lifecycle.jsonl',node_id=request.node.nodeid)
    request.node._dda_database=(database,postgres)
    return database.url
