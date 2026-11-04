from dd_agent.assembly import VERSION
from dd_agent.parent_coverage import ParentCoverage
from test_mixed_search import fixture


def test_auto_discovery_limits_metadata_and_does_not_cross_project(tmp_path,database_url):
    x=fixture(tmp_path,database_url)
    with x.db.connect() as conn:
        conn.execute('''WITH added AS (
            INSERT INTO dd_agent.documents(id,project_id,sha256,byte_size,storage_key,status,reason)
            SELECT gen_random_uuid(),%s,lpad(n::text,64,'0'),1,'synthetic','registered',''
            FROM generate_series(1,21) AS n RETURNING id)
            INSERT INTO dd_agent.assembly_checkpoints(document_id,policy_version,generation,state)
            SELECT id,%s,1,'{"closed":true}'::jsonb FROM added''',(x.p,VERSION))
    coverage=ParentCoverage(x.db)
    result=coverage.discover(x.p)
    assert result['limited'] and len(result['documents'])==20
    other=x.db.create_project('空项目')['id']
    assert coverage.discover(other)==dict(documents=[],limited=False)
    assert all(x.db.document(x.p,d) is not None for d in result['documents'])
    # Coarse discovery never grants eligibility to forged/incomplete metadata.
    assert any(not coverage.inspect(x.p,d,x.index.version())['eligible'] for d in result['documents'])
