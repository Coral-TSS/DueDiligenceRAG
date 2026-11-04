"""Real PostgreSQL checkpoints; never use the business connection."""
import importlib
import pytest
from test_retrieval import setup


def store(db):
    try:return importlib.import_module('dd_agent.assembly_store').AssemblyStore(db)
    except ModuleNotFoundError:pytest.fail('RG-06a persistent assembly checkpoints are missing')


def sample(tmp_path,database_url):
    app,p,d,_,_=setup(tmp_path,database_url)
    db=app.state.database
    db.set_reading(d,2,'scan-v1')
    for n,text in [(1,'公司应提供资料，'),(2,'并说明注册资本变化。')]:
        db.save_page(d,'scan-v1',dict(page_number=n,status='succeeded',method='text',text=text))
    return db,p,d


def test_new_repository_restores_tail_and_replay_does_not_duplicate_batch(tmp_path,database_url):
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    pages=repo.snapshots(p,d,[1],ready_pages={1})
    first=repo.advance(p,d,pages,expected_generation=0)
    assert first['generation']==1 and first['parents']==[]
    assert store(db).load(p,d)==dict(generation=1,state=first['state'])
    assert repo.advance(p,d,pages,expected_generation=0)==first
    second=store(db).advance(p,d,repo.snapshots(p,d,[2],ready_pages={2}),expected_generation=1,final=True)
    assert [v['text'] for v in second['parents']]==['公司应提供资料，并说明注册资本变化。']
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches WHERE document_id=%s',(d,)).fetchone()['n']==2
    assert db.health()['status']=='ready'
    db.migrate()  # Upgrade remains safely repeatable, including existing checkpoints.
    assert repo.load(p,d)['generation']==2


def test_old_source_and_stale_checkpoint_cannot_publish_or_replay(tmp_path,database_url):
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    pages=repo.snapshots(p,d,[1],ready_pages={1})
    repo.advance(p,d,pages,expected_generation=0)
    db.save_page(d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='新文字，'))
    from dd_agent.assembly_store import AssemblyStale
    with pytest.raises(AssemblyStale):repo.advance(p,d,pages,expected_generation=0)
    with pytest.raises(AssemblyStale):repo.advance(p,d,repo.snapshots(p,d,[2],ready_pages={2}),expected_generation=1,final=True)
    assert repo.load(p,d)['generation']==1


def test_two_writers_only_publish_one_generation(tmp_path,database_url):
    from concurrent.futures import ThreadPoolExecutor
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    pages=repo.snapshots(p,d,[1,2],ready_pages={1,2})
    def write(final):
        try:return store(db).advance(p,d,pages,expected_generation=0,final=final)
        except Exception as exc:return type(exc).__name__
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(write,[False,True]))
    assert sum(isinstance(v,dict) for v in results)==1
    assert 'AssemblyConflict' in results
    assert repo.load(p,d)['generation']==1


def test_snapshot_reader_requires_explicit_readiness_and_bounds_scope(tmp_path,database_url):
    from uuid import uuid4
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    db.set_reading(d,3,'scan-v1')
    snapshots=repo.snapshots(p,d,[1,2,3],ready_pages={1})
    assert [v.readiness for v in snapshots]==['ready','pending','missing']
    assert snapshots[1].text==snapshots[2].text==''
    with pytest.raises(ValueError):repo.snapshots(str(uuid4()),d,[1],ready_pages={1})
    with pytest.raises(ValueError):repo.load(str(uuid4()),d)
    with pytest.raises(ValueError):repo.snapshots(p,d,list(range(1,22)),ready_pages=set())
    with pytest.raises(ValueError):repo.snapshots(p,d,[1,3],ready_pages={1,3})


def test_filled_gap_invalidates_checkpoint_and_explicit_restart_keeps_history(tmp_path,database_url):
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    db.set_reading(d,3,'scan-v1')
    repo.advance(p,d,repo.snapshots(p,d,[1,2,3],ready_pages={1,2}),expected_generation=0)
    db.save_page(d,'scan-v1',dict(page_number=3,status='succeeded',method='text',text='补齐原文'))
    from dd_agent.assembly_store import AssemblyStale,AssemblyConflict
    with pytest.raises(AssemblyStale):repo.advance(p,d,[],expected_generation=1,final=True)
    with pytest.raises(AssemblyConflict):repo.restart(p,d,expected_generation=0)
    assert repo.restart(p,d,expected_generation=1)==dict(generation=2,state=None)
    assert repo.load(p,d)==dict(generation=2,state=None)
    result=repo.advance(p,d,repo.snapshots(p,d,[1,2,3],ready_pages={1,2,3}),expected_generation=2,final=True)
    assert result['generation']==3 and any('补齐原文'==v['text'] for v in result['parents'])
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches WHERE document_id=%s',(d,)).fetchone()['n']==3


def test_missing_page_insert_waits_for_assembly_commit(tmp_path,database_url,monkeypatch):
    import psycopg
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    db.set_reading(d,3,'scan-v1')
    save=repo._save
    def check_writer(conn,*args):
        with db.connect() as other:
            other.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                db.save_page(d,'scan-v1',dict(page_number=3,status='succeeded',method='text',text='后来插入的页'),conn=other)
            other.rollback()
        save(conn,*args)
    monkeypatch.setattr(repo,'_save',check_writer)
    repo.advance(p,d,repo.snapshots(p,d,[1,2,3],ready_pages={1,2}),expected_generation=0)


def test_source_changed_during_pure_assembly_and_spoofed_text_are_rejected(tmp_path,database_url,monkeypatch):
    from dataclasses import replace
    from dd_agent import assembly_store
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    pages=repo.snapshots(p,d,[1],ready_pages={1})
    with pytest.raises(assembly_store.AssemblyStale):
        repo.advance(p,d,[replace(pages[0],text='伪造正文')],expected_generation=0)
    real=assembly_store.assemble
    def changed(*a,**k):
        result=real(*a,**k)
        db.save_page(d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='计算期间变更'))
        return result
    monkeypatch.setattr(assembly_store,'assemble',changed)
    with pytest.raises(assembly_store.AssemblyStale):repo.advance(p,d,pages,expected_generation=0)
    assert repo.load(p,d)==dict(generation=0,state=None)


def test_checkpoint_failure_rolls_back_batch_receipt(tmp_path,database_url):
    import psycopg
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    repo.advance(p,d,repo.snapshots(p,d,[1],ready_pages={1}),expected_generation=0)
    with db.connect() as conn:
        conn.execute("ALTER TABLE dd_agent.assembly_checkpoints ADD CONSTRAINT reject_probe CHECK(generation<2)")
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.advance(p,d,repo.snapshots(p,d,[2],ready_pages={2}),expected_generation=1,final=True)
    assert repo.load(p,d)['generation']==1
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches WHERE document_id=%s',(d,)).fetchone()['n']==1


def test_material_cannot_close_before_physical_end_or_replay_after_page_count_grows(tmp_path,database_url):
    db,p,d=sample(tmp_path,database_url);repo=store(db)
    db.set_reading(d,3,'scan-v1')
    from dd_agent.assembly_store import AssemblyStale
    with pytest.raises(AssemblyStale):
        repo.advance(p,d,repo.snapshots(p,d,[1],ready_pages={1}),expected_generation=0,final=True)
    assert repo.load(p,d)['generation']==0
    pages=repo.snapshots(p,d,[1,2,3],ready_pages={1,2})
    repo.advance(p,d,pages,expected_generation=0,final=True)
    db.set_reading(d,4,'scan-v1')
    with pytest.raises(AssemblyStale):repo.advance(p,d,pages,expected_generation=0,final=True)
