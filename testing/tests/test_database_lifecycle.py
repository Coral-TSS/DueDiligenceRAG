"""The test reaper must preserve failures/busy clients and never target another DB."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import psycopg
import pytest


def create(postgres,tmp_path):
    try:module=importlib.import_module('testing.support.database_lifecycle')
    except ModuleNotFoundError:pytest.fail('Isolated database lifecycle management is missing')
    return module.IsolatedDatabase(postgres,tmp_path/'database-lifecycle.jsonl')


def exists(postgres,name):
    with psycopg.connect(postgres+' dbname=postgres',autocommit=True) as conn:
        return bool(conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone())


def test_committed_data_is_durable_until_successful_test_cleanup(postgres,tmp_path):
    db=create(postgres,tmp_path)
    with psycopg.connect(db.url) as conn:
        conn.execute('CREATE TABLE durable_marker(value integer)')
        conn.execute('INSERT INTO durable_marker VALUES(7)')
    with psycopg.connect(db.url) as conn:assert conn.execute('SELECT value FROM durable_marker').fetchone()==(7,)
    assert db.finish('passed')=='dropped' and not exists(postgres,db.name)
    assert db.finish('passed')=='dropped'
    rows=[json.loads(s) for s in (tmp_path/'database-lifecycle.jsonl').read_text().splitlines()]
    assert [r['action'] for r in rows]==['created','dropped']
    assert all('url' not in r and 'dsn' not in r for r in rows)


def test_failed_or_unreported_tests_keep_database_for_diagnosis(postgres,tmp_path):
    db=create(postgres,tmp_path)
    assert db.finish('failed')=='retained_failed' and exists(postgres,db.name)
    assert db.finish('unknown')=='retained_unknown' and exists(postgres,db.name)
    assert db.finish('passed')=='dropped'


def test_busy_connection_is_not_terminated_or_force_dropped(postgres,tmp_path):
    db=create(postgres,tmp_path)
    with psycopg.connect(db.url,autocommit=True) as live:
        assert db.finish('passed')=='retained_busy'
        assert live.execute('SELECT 1').fetchone()==(1,)
    assert db.finish('passed')=='dropped'


def test_changed_database_identity_cannot_drop_another_database(postgres,tmp_path):
    db=create(postgres,tmp_path);issued=db.name
    db.name='postgres'
    with pytest.raises(ValueError):db.finish('passed')
    assert exists(postgres,'postgres') and exists(postgres,issued)
    db.name=issued
    assert db.finish('passed')=='dropped'


def test_pytest_reaps_only_after_fixture_close_and_retains_failed_case(tmp_path):
    root=Path(__file__).resolve().parents[2];child=tmp_path/'child';suite=child/'suite';suite.mkdir(parents=True)
    (suite/'conftest.py').write_text('''from testing.tests.conftest import *
import pytest,psycopg
@pytest.fixture
def held_client(database_url):
    with psycopg.connect(database_url) as conn:
        yield conn
''',encoding='utf-8')
    (suite/'test_contract.py').write_text('''import json
from pathlib import Path
import psycopg
def remember(label,url):
    name=psycopg.conninfo.conninfo_to_dict(url)['dbname']
    Path(__file__).with_name(label+'.json').write_text(json.dumps(name))
def test_a_failure(database_url):
    remember('failed',database_url)
    with psycopg.connect(database_url) as conn:
        conn.execute('CREATE TABLE retained(value integer)')
        conn.execute('INSERT INTO retained VALUES(9)')
    assert False,'intentional failure retention fixture'
def test_b_success(database_url,held_client):
    remember('passed',database_url)
    assert held_client.execute('SELECT 1').fetchone()==(1,)
def test_c_after_teardown(postgres):
    failed=json.loads(Path(__file__).with_name('failed.json').read_text())
    passed=json.loads(Path(__file__).with_name('passed.json').read_text())
    with psycopg.connect(postgres+' dbname=postgres',autocommit=True) as conn:
        assert conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(failed,)).fetchone()
        assert not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(passed,)).fetchone()
    with psycopg.connect(postgres+' dbname='+failed) as conn:
        assert conn.execute('SELECT value FROM retained').fetchone()==(9,)
''',encoding='utf-8')
    env={**os.environ,'DDA_TEST_RUN_DIR':str(child),'PYTHONUTF8':'1',
        'PYTHONPATH':os.pathsep.join((str(root),str(root/'src'))),'PYTHONPYCACHEPREFIX':str(root/'testing/cache/bytecode'),
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}
    result=subprocess.run([sys.executable,'-m','pytest',str(suite),'-q','-o','cache_dir='+str(child/'cache'),
        '--basetemp='+str(child/'tmp'),'--junitxml='+str(child/'results.xml')],cwd=root,env=env,
        capture_output=True,text=True,encoding='utf-8',timeout=600)
    (child/'console.log').write_text(result.stdout+result.stderr,encoding='utf-8')
    assert result.returncode==1,result.stdout+result.stderr
    suites=ET.parse(child/'results.xml').getroot().iter('testsuite')
    assert sum(int(s.get('failures','0')) for s in suites)==1,result.stdout+result.stderr
