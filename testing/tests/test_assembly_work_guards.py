"""Assembly replay, reset, exact claims and short publication boundaries."""
import pytest

from dd_agent.stage_publication import StageSuperseded
from test_assembly_work import setup,prepared,claim,counts


def test_reset_after_existing_receipt_prevents_owned_old_replay(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d)
    repo.advance(p,d,repo.snapshots(p,d,[1],ready_pages={1}),expected_generation=0)
    task=claim(worker,manager,job,plan)
    repo.restart(p,d,expected_generation=1)
    with pytest.raises(StageSuperseded):worker.run(task['id'],task['owner_token'],plan)
    assert repo.load(p,d)==dict(generation=2,state=None)
    assert counts(db,d)==[1,2,2]


def test_exact_claim_never_takes_another_pending_window(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    first=prepared(repo,p,d)
    older=worker.register(job['id'],first,expected_epoch=job['run_epoch'])
    second=prepared(repo,p,d,(1,2),final=True)
    newer=worker.register(job['id'],second,expected_epoch=job['run_epoch'])
    taken=manager.stages.claim(job['id'],'assembly',task_id=newer['id'])
    assert taken['id']==newer['id']
    with db.connect() as conn:
        assert conn.execute('SELECT state FROM dd_agent.stage_jobs WHERE id=%s',(older['id'],)).fetchone()['state']=='pending'


def test_explicit_replay_keeps_one_receipt_and_tail_only_closes_at_physical_end(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d,(1,2))
    for _ in range(2):
        task=claim(worker,manager,job,plan)
        assert worker.run(task['id'],task['owner_token'],plan)['generation']==1
    assert counts(db,d)==[1,1,1]
    close=prepared(repo,p,d,(),generation=1,final=True)
    task=claim(worker,manager,job,close)
    result=worker.run(task['id'],task['owner_token'],close)
    assert result['state']['closed'] and result['state']['tail'] is None
    assert counts(db,d)==[1,2,2]


def test_resume_during_checkpoint_cannot_commit_old_preparation(tmp_path,database_url):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d);task=claim(worker,manager,job,plan)
    def restart(*a):
        manager.pause(p,d);manager.resume(p,d,wake=False)
    with pytest.raises(StageSuperseded):worker.run(task['id'],task['owner_token'],plan,checkpoint=restart)
    assert counts(db,d)==[0,0,0]


def test_large_preparation_validation_never_holds_parent_or_reading_lock(tmp_path,database_url,monkeypatch):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d);task=claim(worker,manager,job,plan)
    actual=repo._validated
    observed=[]
    def validate(value):
        with db.connect() as conn:
            conn.execute('SELECT id FROM dd_agent.processing_jobs WHERE id=%s FOR UPDATE NOWAIT',(job['id'],))
            conn.execute('SELECT document_id FROM dd_agent.document_readings WHERE document_id=%s FOR UPDATE NOWAIT',(d,))
        observed.append(True)
        return actual(value)
    monkeypatch.setattr(repo,'_validated',validate)
    worker.run(task['id'],task['owner_token'],plan)
    assert observed==[True] and counts(db,d)==[1,1,1]


def test_failure_completing_stage_rolls_back_tail_receipt_and_outbox(tmp_path,database_url,monkeypatch):
    db,p,d,manager,job,repo,worker=setup(tmp_path,database_url)
    plan=prepared(repo,p,d);task=claim(worker,manager,job,plan)
    actual=manager.stages._end_attempt
    def fail_success(conn,identity,outcome,code=''):
        if outcome=='succeeded':raise RuntimeError('synthetic stage completion failure')
        return actual(conn,identity,outcome,code)
    monkeypatch.setattr(manager.stages,'_end_attempt',fail_success)
    with pytest.raises(RuntimeError):worker.run(task['id'],task['owner_token'],plan)
    assert counts(db,d)==[0,0,0]
    with db.connect() as conn:
        assert conn.execute('SELECT state,failures FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()==dict(state='pending',failures=1)
