"""Durable parent work packets and vectors; real DB, no business documents."""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import pytest

from test_parent_index import sample,Models,Budget
from dd_agent.assembly_store import AssemblyStore,AssemblyStale
from dd_agent.control import ProcessingPaused,ProcessingStopped,ProcessingDeferred
from dd_agent.parent_index import ParentIndex
from dd_agent.stage_publication import StageSuperseded,StagePaused


def work(service,stages):
    try:return importlib.import_module('dd_agent.parent_work').ParentWork(service,stages)
    except ModuleNotFoundError:pytest.fail('Durable parent work packets are missing')


def fresh(service,stages):
    other=ParentIndex(service.settings,database=service.database,models=Models(),budget=Budget())
    return other,work(other,stages)


def test_manifest_and_stage_registration_roll_back_together(tmp_path,database_url,monkeypatch):
    db,p,d,service,old,manager,claim,models,_=sample(tmp_path,database_url)
    work(service,manager.stages)
    models.version='next-version'
    plan=service.prepare(p,d,old['parents'],AssemblyStore(db).snapshots(p,d,[1,2],ready_pages={1,2}))
    def broken(*a,**k):raise RuntimeError('injected registration failure')
    monkeypatch.setattr(manager.stages,'ensure',broken)
    with pytest.raises(RuntimeError):service.register(manager.stages,claim['job_id'],plan)
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_work WHERE id=%s',(plan['id'],)).fetchone()['n']==0
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_work WHERE id=%s',(old['id'],)).fetchone()['n']==1


def test_new_instance_recovers_saved_vectors_without_tokenizing_or_embedding(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,budget=sample(tmp_path,database_url)
    current=work(service,manager.stages)
    computed=service.embed(plan)
    current.save_vectors(claim['id'],claim['owner_token'],computed)
    db.migrate()  # Re-running upgrades must retain both the packet and saved vectors.
    other,recovered=fresh(service,manager.stages)
    assert recovered.load(claim['id'],claim['owner_token'])==computed
    identity=recovered.run(claim['id'],claim['owner_token'])
    assert other.models.calls==0 and other.budget.calls==0
    assert service.metadata(p,d,identity)['current'] and models.calls==1


@pytest.mark.parametrize('pause_error',[ProcessingPaused,ProcessingStopped,StagePaused])
def test_pause_after_vector_checkpoint_then_resume_reuses_vectors(tmp_path,database_url,pause_error):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    current=work(service,manager.stages)
    def pause(stage,number):
        if models.calls:raise pause_error()
    with pytest.raises(pause_error):current.run(claim['id'],claim['owner_token'],checkpoint=pause)
    with db.connect() as conn:
        row=conn.execute('SELECT state,failures FROM dd_agent.stage_jobs WHERE id=%s',(claim['id'],)).fetchone()
        assert row==dict(state='pending',failures=0)
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_builds').fetchone()['n']==0
    manager.pause(p,d);resumed=manager.resume(p,d,wake=False)
    other,recovered=fresh(service,manager.stages)
    restored=recovered.requeue(resumed['id'],plan['id'],expected_epoch=resumed['run_epoch'])
    taken=manager.stages.claim(resumed['id'],'embedding')
    assert taken['id']==restored['id'] and taken['owner_token']!=claim['owner_token']
    identity=recovered.run(taken['id'],taken['owner_token'])
    assert service.metadata(p,d,identity)['current']
    assert other.models.calls==other.budget.calls==0
    with pytest.raises(StageSuperseded):current.load(claim['id'],claim['owner_token'])


def test_restored_packet_can_be_consumed_in_a_new_python_process(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    work(service,manager.stages).save_vectors(claim['id'],claim['owner_token'],service.embed(plan))
    root=Path(__file__).resolve().parents[2]
    code='''import json,os
from dd_agent.config import Settings
from dd_agent.db import Database
from dd_agent.stage_jobs import StageJobs
from dd_agent.parent_index import ParentIndex
from dd_agent.parent_work import ParentWork
from test_parent_index import Models,Budget
settings=Settings(database_url=os.environ['TEST_DB'],data_dir=os.environ['TEST_DATA'])
db=Database(settings.database_url)
service=ParentIndex(settings,database=db,models=Models(),budget=Budget())
identity=ParentWork(service,StageJobs(db)).run(os.environ['TEST_TASK'],os.environ['TEST_OWNER'])
print(json.dumps(dict(id=identity,embedding_calls=service.models.calls,tokenizer_calls=service.budget.calls)))
'''
    env={**os.environ,'PYTHONPATH':os.pathsep.join([str(root/'src'),str(root/'testing/tests')]),
         'TEST_DB':database_url,'TEST_DATA':str(tmp_path/'child'),'TEST_TASK':str(claim['id']),'TEST_OWNER':str(claim['owner_token'])}
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==dict(id=plan['id'],embedding_calls=0,tokenizer_calls=0)
    assert service.metadata(p,d,plan['id'])['current']


def test_wrong_owner_old_epoch_and_stale_source_cannot_load_or_requeue(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    current=work(service,manager.stages)
    with pytest.raises(StageSuperseded):current.load(claim['id'],uuid4())
    with pytest.raises(StageSuperseded):current.requeue(claim['job_id'],plan['id'],expected_epoch=claim['run_epoch']+1)
    db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='来源已更新'))
    with pytest.raises((StageSuperseded,AssemblyStale)):current.load(claim['id'],claim['owner_token'])
    with pytest.raises((StageSuperseded,AssemblyStale)):current.requeue(claim['job_id'],plan['id'],expected_epoch=claim['run_epoch'])


def test_conflicting_or_corrupted_packets_are_never_silently_replaced(tmp_path,database_url):
    from dd_agent.parent_index import _seal
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    current=work(service,manager.stages)
    conflict=deepcopy(plan);conflict.pop('seal');conflict['children'][0]['embedding_tokens']+=1;_seal(conflict)
    with pytest.raises(ValueError):service.register(manager.stages,claim['job_id'],conflict)
    with db.connect() as conn:
        conn.execute("UPDATE dd_agent.parent_index_work SET plan=jsonb_set(plan,'{children,0,text}',to_jsonb('corrupted'::text)) WHERE id=%s",(plan['id'],))
    with pytest.raises(ValueError):current.load(claim['id'],claim['owner_token'])


def test_vector_checkpoint_is_atomic_and_checked_on_restore(tmp_path,database_url):
    import psycopg
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    current=work(service,manager.stages);computed=service.embed(plan)
    with db.connect() as conn:conn.execute('ALTER TABLE dd_agent.parent_index_work ADD CONSTRAINT reject_probe CHECK(vectors IS NULL)')
    with pytest.raises(psycopg.errors.CheckViolation):current.save_vectors(claim['id'],claim['owner_token'],computed)
    assert current.load(claim['id'],claim['owner_token'])['vectors'] is None
    with db.connect() as conn:conn.execute('ALTER TABLE dd_agent.parent_index_work DROP CONSTRAINT reject_probe')
    current.save_vectors(claim['id'],claim['owner_token'],computed)
    current.save_vectors(claim['id'],claim['owner_token'],computed)
    with db.connect() as conn:conn.execute("UPDATE dd_agent.parent_index_work SET vector_seal=repeat('0',64) WHERE id=%s",(plan['id'],))
    with pytest.raises(ValueError):current.load(claim['id'],claim['owner_token'])


def test_busy_work_is_returned_without_charging_failure_budget(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    current=work(service,manager.stages)
    def busy(*a,**k):raise ProcessingDeferred()
    models.embed=busy
    with pytest.raises(ProcessingDeferred):current.run(claim['id'],claim['owner_token'])
    with db.connect() as conn:
        assert conn.execute('SELECT state,failures,error_code FROM dd_agent.stage_jobs WHERE id=%s',(claim['id'],)).fetchone()==dict(state='pending',failures=0,error_code='model_busy')


def test_negative_zero_vector_round_trip_does_not_break_checkpoint_integrity(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    current=work(service,manager.stages);real=models.embed
    def signed_zero(*a,**k):
        vectors=real(*a,**k)
        for vector in vectors:vector[2]=-0.0
        return vectors
    models.embed=signed_zero
    computed=service.embed(plan)
    current.save_vectors(claim['id'],claim['owner_token'],computed)
    restored=current.load(claim['id'],claim['owner_token'])
    assert restored['vectors']==computed['vectors']
    assert current.run(claim['id'],claim['owner_token'])==plan['id']


def test_source_change_during_restore_compute_or_save_never_reuses_stale_vectors(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    current=work(service,manager.stages);computed=service.embed(plan)
    db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='计算后来源改变'))
    with pytest.raises((StageSuperseded,AssemblyStale)):current.save_vectors(claim['id'],claim['owner_token'],computed)
    with db.connect() as conn:
        assert conn.execute('SELECT vectors FROM dd_agent.parent_index_work WHERE id=%s',(plan['id'],)).fetchone()['vectors'] is None
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_builds').fetchone()['n']==0


def test_model_change_after_checkpoint_is_refused(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    current=work(service,manager.stages)
    current.save_vectors(claim['id'],claim['owner_token'],service.embed(plan))
    models.version='new-model'
    with pytest.raises(ValueError):current.load(claim['id'],claim['owner_token'])


def test_saved_vectors_cannot_be_overwritten_by_another_computation(tmp_path,database_url):
    from dd_agent.parent_index import _seal
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    current=work(service,manager.stages);original=service.embed(plan)
    current.save_vectors(claim['id'],claim['owner_token'],original)
    changed=deepcopy(original);changed.pop('seal');changed['vectors'][0][2]=.5;_seal(changed)
    with pytest.raises(ValueError):current.save_vectors(claim['id'],claim['owner_token'],changed)
    with pytest.raises(ValueError):service.publish(manager.stages,claim['id'],claim['owner_token'],changed)
    assert current.load(claim['id'],claim['owner_token'])==original


def test_real_saved_vectors_restore_without_model_requests(tmp_path,database_url):
    if os.getenv('DDA_TEST_LOCAL_MODELS')!='1':pytest.skip('Enable real vector checkpoint integration')
    from dd_agent.retrieval import LocalModels
    from dd_agent.token_budget import LocalTokenBudget
    from dd_agent.performance import capture
    from dd_agent.model_runtime import close_runtime
    db,p,d,service,old,manager,_,_,_=sample(tmp_path,database_url)
    service.models=LocalModels(service.settings);service.budget=LocalTokenBudget(service.settings)
    try:
        plan=service.prepare(p,d,old['parents'],AssemblyStore(db).snapshots(p,d,[1,2],ready_pages={1,2}))
        task=service.register(manager.stages,manager.latest(p,d)['id'],plan)
        taken=manager.stages.claim(task['job_id'],'embedding')
        current=work(service,manager.stages)
        current.save_vectors(taken['id'],taken['owner_token'],service.embed(plan))
        fresh_service=ParentIndex(service.settings,database=db)
        with capture(tmp_path/'restored-trace','test',sample_memory=False):
            identity=work(fresh_service,manager.stages).run(taken['id'],taken['owner_token'])
        trace=json.loads(next((tmp_path/'restored-trace').glob('*.json')).read_text(encoding='utf-8'))
        assert identity==plan['id'] and service.metadata(p,d,identity)['current']
        assert trace['counters'].get('runtime.requests',0)==0
        assert trace['counters'].get('worker.tokenizer_worker.starts',0)==0
    finally:close_runtime()
