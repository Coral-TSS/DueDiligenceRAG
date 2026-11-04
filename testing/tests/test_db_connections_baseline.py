"""Real local SQL benchmark, synthetic vectors; latency is evidence, not a flaky pass threshold."""
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter

from dd_agent.db import Database
from dd_agent.db_connections import ConnectionProvider
from dd_agent.performance import capture
from dd_agent.retrieval import CURRENT
from test_retrieval import setup


def test_connections_and_writes_baseline(tmp_path, database_url):
    app, p, d, service, _ = setup(tmp_path/'data', database_url)
    service.index(p, d, [1, 2])
    output = Path(os.environ.get('DDA_TEST_RUN_DIR', tmp_path))/'db-connections-baseline'
    output.mkdir(parents=True, exist_ok=True)
    result = {'ordinary_calls_per_case': 40, 'write_rows_per_trial': 128, 'cases': {}}
    for label, provider in [('short', None), ('pooled', ConnectionProvider(database_url, max_size=2))]:
        if provider: provider.start()
        db = Database(database_url, provider=provider)
        try:
            times=[]
            with capture(output/label, 'db_baseline'):
                for _ in range(3):
                    start=perf_counter()
                    for _ in range(40): assert db.project_exists(p)
                    times.append(perf_counter()-start)
            result['cases'][label]={'seconds':times,'median_seconds':median(times),
                                     'pool_stats':provider.stats() if provider else None}
            if provider: assert provider.stats()['connections_num'] <= 2
        finally:
            if provider: provider.close()
    db = Database(database_url)
    with db.connect() as conn:
        conn.execute('CREATE TABLE dd_agent.write_probe_h(id integer PRIMARY KEY,body text,embedding public.vector(1024))')
    vector='['+','.join(['1']+['0']*1023)+']'
    params=[(n,'合成材料片段。'*70,vector) for n in range(128)]
    statement='INSERT INTO dd_agent.write_probe_h VALUES (%s,%s,%s::public.vector)'
    result['writes']={}
    for label in ('per_row','executemany'):
        elapsed=[]
        for _ in range(3):
            with db.connect() as conn: conn.execute('TRUNCATE dd_agent.write_probe_h')
            start=perf_counter()
            with db.connect() as conn:
                if label=='per_row':
                    for row in params: conn.execute(statement,row)
                else: conn.cursor().executemany(statement,params)
            elapsed.append(perf_counter()-start)
            with db.connect() as conn:
                assert conn.execute('SELECT count(*) AS n FROM dd_agent.write_probe_h').fetchone()['n']==128
        result['writes'][label]={'seconds':elapsed,'median_seconds':median(elapsed)}
    version=service.version()
    with db.connect() as conn:
        plan=conn.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT i.page_number '+CURRENT+
                          ' AND i.document_id=%s',(p,version,d)).fetchone()['QUERY PLAN']
    (output/'current-index-plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    (output/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('Connection baseline:',output)
