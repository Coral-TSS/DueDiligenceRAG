"""Owned assembly publication with real SQL, including non-text boundary sources."""
from copy import deepcopy
import importlib
from uuid import uuid4

import pytest

from dd_agent.assembly_store import AssemblyStore,AssemblyStale,AssemblyConflict
from dd_agent.config import Settings
from dd_agent.pipeline import JobManager
from dd_agent.stage_publication import StageSuperseded,StagePaused
from dd_agent.control import ProcessingPaused
from test_assembly_store import sample


def setup(tmp_path,database_url):
    try:module=importlib.import_module('dd_agent.assembly_work')
    except ModuleNotFoundError:pytest.fail('Owned assembly publication is missing')
    db,p,d=sample(tmp_path,database_url)
    manager=JobManager(Settings(database_url=database_url,data_dir=tmp_path),database=db)
    job=manager.enqueue(p,d,wake=False)
    repo=AssemblyStore(db)
    worker=module.AssemblyWork(repo,manager.stages)
    return db,p,d,manager,job,repo,worker


def prepared(repo,p,d,numbers=(1,),*,generation=0,final=False,ready=None):
    pages=repo.snapshots(p,d,list(numbers),ready_pages=set(numbers) if ready is None else ready) if numbers else []
    return repo.prepare(p,d,pages,expected_generation=generation,final=final)


def claim(worker,manager,job,plan):
    task=worker.register(job['id'],plan,expected_epoch=job['run_epoch'])
    taken=manager.stages.claim(job['id'],'assembly',task_id=task['id'])
    assert taken is not None and taken['id']==task['id']
    return taken


def counts(db,d):
    with db.connect() as conn:
        return [conn.execute(f'SELECT count(*) AS n FROM dd_agent.{table} WHERE document_id=%s',(d,)).fetchone()['n']
                for table in ('assembly_checkpoints','assembly_batches','assembly_receipt_outbox')]


def test_preparation_is_pure_and_commit_uses_callers_transaction(tmp_path,database_url):
    db,p,d,_,_,repo,_=setup(tmp_path,database_url)
    plan=prepared(repo,p,d)
    assert counts(db,d)==[0,0,0]
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            repo.commit(plan,conn)
            raise RuntimeError('synthetic rollback')
    assert counts(db,d)==[0,0,0]
    with db.connect() as conn:first=repo.commit(plan,conn)
    assert first['generation']==1 and first['parents']==[]
    assert counts(db,d)==[1,1,1]


def test_owned_commit_and_stage_completion_are_atomic_and_continue_tail(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    first=prepared(repo,p,d)
    task=claim(worker,manager,job,first)
    worker.run(task['id'],task['owner_token'],first)
    second=prepared(repo,p,d,(2,),generation=1,final=True)
    task=claim(worker,manager,job,second)
    result=worker.run(task['id'],task['owner_token'],second)
    assert [p['text'] for p in result['parents']]==['公司应提供资料，并说明注册资本变化。']
    assert result['state']['closed'] and counts(db,d)==[1,2,2]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM dd_agent.stage_jobs WHERE job_id=%s AND stage='assembly' AND state='succeeded'",(job['id'],)).fetchone()['n']==2


def test_wrong_owner_and_old_epoch_cannot_advance_tail(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d);task=claim(worker,manager,job,plan)
    with pytest.raises(StageSuperseded):worker.run(task['id'],uuid4(),plan)
    manager.pause(p,d);resumed=manager.resume(p,d,wake=False)
    with pytest.raises(StageSuperseded):worker.run(task['id'],task['owner_token'],plan)
    with pytest.raises(StageSuperseded):worker.register(job['id'],plan,expected_epoch=job['run_epoch'])
    assert counts(db,d)==[0,0,0]
    fresh=prepared(repo,p,d);task=claim(worker,manager,resumed,fresh)
    assert worker.run(task['id'],task['owner_token'],fresh)['generation']==1


def test_source_change_after_claim_cannot_publish(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d);task=claim(worker,manager,job,plan)
    db.save_page(d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='变更来源'))
    with pytest.raises((StageSuperseded,AssemblyStale)):worker.run(task['id'],task['owner_token'],plan)
    assert counts(db,d)==[0,0,0]


@pytest.mark.parametrize('boundary',['failed','missing','pending','blank'])
def test_explicit_boundary_sources_can_be_owned_without_indexing_their_text(tmp_path,database_url,boundary):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    if boundary=='failed':db.save_page(d,'scan-v1',dict(page_number=2,status='failed',method='ocr',text='',error_code='synthetic'))
    if boundary=='missing':db.set_reading(d,3,'scan-v1')
    if boundary=='blank':db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='  '))
    numbers=(1,2,3) if boundary=='missing' else (1,2)
    ready={1,2} if boundary in ('missing','blank') else {1}
    plan=prepared(repo,p,d,numbers,ready=ready,final=True)
    task=claim(worker,manager,job,plan)
    result=worker.run(task['id'],task['owner_token'],plan)
    assert result['state']['closed'] and counts(db,d)==[1,1,1]
    assert result['input_dependencies'][-1]['readiness']==('ready' if boundary=='blank' else boundary)
    if boundary=='pending':assert all('注册资本变化' not in b['text'] for b in result['parents'])


def test_filled_missing_boundary_invalidates_claim(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    db.set_reading(d,3,'scan-v1')
    plan=prepared(repo,p,d,(1,2,3),ready={1,2},final=True)
    task=claim(worker,manager,job,plan)
    db.save_page(d,'scan-v1',dict(page_number=3,status='succeeded',method='text',text='新插入页'))
    with pytest.raises((StageSuperseded,AssemblyStale)):worker.run(task['id'],task['owner_token'],plan)
    assert counts(db,d)==[0,0,0]


def test_checkpoint_failure_also_rolls_back_stage_success(tmp_path,database_url):
    import psycopg
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d);task=claim(worker,manager,job,plan)
    with db.connect() as conn:conn.execute('ALTER TABLE dd_agent.assembly_checkpoints ADD CONSTRAINT reject_all CHECK(generation<1)')
    with pytest.raises(psycopg.errors.CheckViolation):worker.run(task['id'],task['owner_token'],plan)
    assert counts(db,d)==[0,0,0]
    with db.connect() as conn:
        assert conn.execute('SELECT state FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()['state']!='succeeded'


@pytest.mark.parametrize('global_pause',[True,False])
def test_pause_returns_claim_without_writing_or_spending_failure_budget(tmp_path,database_url,global_pause):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d);task=claim(worker,manager,job,plan)
    if global_pause:manager.pause(p,d)
    else:manager.control_stage(p,d,'embedding',pause=True)
    with pytest.raises(ProcessingPaused if global_pause else StagePaused):worker.run(task['id'],task['owner_token'],plan)
    assert counts(db,d)==[0,0,0]
    with db.connect() as conn:
        assert conn.execute('SELECT state,failures FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()==dict(state='pending',failures=0)


def test_foreign_plan_and_tampered_preparation_cannot_be_registered(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d)
    broken=deepcopy(plan);broken['result']['state']['next_page']=10
    with pytest.raises(ValueError):worker.register(job['id'],broken,expected_epoch=job['run_epoch'])
    other=deepcopy(plan);other['project_id']=str(uuid4())
    with pytest.raises(ValueError):worker.register(job['id'],other,expected_epoch=job['run_epoch'])
    assert counts(db,d)==[0,0,0]


@pytest.mark.parametrize('stage',['vision','embedding'])
def test_non_success_boundary_dependency_is_rejected_for_model_stages(tmp_path,database_url,stage):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    dep=dict(page_number=1,view='raw' if stage=='vision' else 'effective',revision='synthetic',readiness='pending',quality='unverified')
    with pytest.raises(ValueError):manager.stages.ensure(job['id'],stage,dict(pages=[1]),source_revision='x',policy_version='x',dependencies=[dep])
