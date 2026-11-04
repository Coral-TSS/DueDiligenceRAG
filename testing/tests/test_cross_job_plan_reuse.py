"""New jobs reuse immutable preparation, never another job's execution rights."""
from copy import deepcopy

import pytest
from psycopg.types.json import Jsonb

from dd_agent.assembly import digest
from dd_agent.assembly_handoff import ReceiptHandoff
from dd_agent.parent_index import ParentIndex
from dd_agent.parent_work import ParentWork
from dd_agent.stage_publication import StageSuperseded
from dd_agent.performance import capture
from test_assembly_handoff import sample,reconcile,scalar
from test_parent_index import Budget,Models


def next_job(x):
    # Only parent task completion is synthetic; receipt/work/publication use SQL.
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.processing_jobs SET status='completed' WHERE id=%s",(x.job['id'],))
    x.job=x.manager.enqueue(x.p,x.d,wake=False)
    x.index=ParentIndex(x.index.settings,database=x.db,models=Models(),budget=Budget())
    x.bridge=ReceiptHandoff(x.index,x.manager.stages)


@pytest.mark.parametrize('saved',[False,True])
def test_new_job_reuses_plan_and_checkpoint_but_requires_fresh_claim(tmp_path,database_url,saved):
    x=sample(tmp_path,database_url);reconcile(x)
    old=x.manager.stages.claim(x.job['id'],'embedding')
    work=ParentWork(x.index,x.manager.stages)
    if saved:work.save_vectors(old['id'],old['owner_token'],x.index.embed(work.load(old['id'],old['owner_token'])))
    next_job(x)
    assert reconcile(x)['registered']==1
    assert x.index.budget.calls==0,'New job must reuse the sealed tokenized plan'
    new=x.manager.stages.claim(x.job['id'],'embedding')
    assert new['id']!=old['id'] and new['owner_token']!=old['owner_token']
    with pytest.raises(StageSuperseded):ParentWork(x.index,x.manager.stages).load(old['id'],old['owner_token'])
    identity=ParentWork(x.index,x.manager.stages).run(new['id'],new['owner_token'])
    assert x.index.metadata(x.p,x.d,identity)['current']
    assert x.index.models.calls==(0 if saved else 1)
    assert scalar(x,'parent_index_work')==1 and scalar(x,'assembly_handoff_parts')==2


@pytest.mark.parametrize('damage',['seal','parents','children'])
def test_corrupt_saved_plan_is_reprepared_and_repaired_atomically(tmp_path,database_url,damage):
    x=sample(tmp_path,database_url);reconcile(x)
    with x.db.connect() as conn:
        row=conn.execute('SELECT id,plan FROM dd_agent.parent_index_work').fetchone()
        broken=deepcopy(row['plan'])
        broken['children' if damage=='children' else 'parents'][0]['text']='损坏的旧正文'
        if damage!='seal':broken['seal']=digest({k:v for k,v in broken.items() if k!='seal'})
        conn.execute('UPDATE dd_agent.parent_index_work SET plan=%s,plan_seal=%s WHERE id=%s',
            (Jsonb(broken),broken['seal'],row['id']))
    next_job(x)
    assert reconcile(x)['registered']==1
    assert x.index.budget.calls>0
    taken=x.manager.stages.claim(x.job['id'],'embedding')
    identity=ParentWork(x.index,x.manager.stages).run(taken['id'],taken['owner_token'])
    with x.db.connect() as conn:
        text=conn.execute('SELECT text FROM dd_agent.parent_index_parents WHERE build_id=%s',(identity,)).fetchone()['text']
    assert text=='公司应提供材料，并说明注册资本变化。'


@pytest.mark.parametrize('changed',['model','tokenizer','source','reset'])
def test_new_job_does_not_reuse_incompatible_or_retired_plan(tmp_path,database_url,changed):
    x=sample(tmp_path,database_url);reconcile(x);next_job(x)
    if changed=='model':x.index.models.version='different-embedding'
    if changed=='tokenizer':x.index.budget=Budget(2)
    if changed=='source':x.db.save_page(x.d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='注册资本已变化。'))
    if changed=='reset':x.store.restart(x.p,x.d,expected_generation=1)
    result=reconcile(x)
    if changed in ('model','tokenizer'):
        assert result['registered']==1 and x.index.budget.calls>0
        assert scalar(x,'parent_index_work')==2
    else:
        assert result['registered']==0
        assert result['stale']==1 if changed=='source' else result['empty']==1
        assert x.index.budget.calls==0


def test_reset_between_reuse_and_commit_cannot_adopt_old_plan(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url);reconcile(x);next_job(x)
    prepare=x.bridge._prepare
    def reset(*args):
        result=prepare(*args);x.store.restart(x.p,x.d,expected_generation=1);return result
    monkeypatch.setattr(x.bridge,'_prepare',reset)
    result=reconcile(x,limit=1)
    assert result['superseded']==1 and result['registered']==0
    assert x.index.budget.calls==0
    assert scalar(x,'assembly_handoff_parts')==1


def test_corrupt_plan_repair_rolls_back_with_handoff_and_stage(tmp_path,database_url):
    import psycopg
    x=sample(tmp_path,database_url);reconcile(x);next_job(x)
    with x.db.connect() as conn:
        original=conn.execute('''UPDATE dd_agent.parent_index_work SET
            plan=jsonb_set(plan,'{children,0,text}','"broken"'::jsonb) RETURNING plan''').fetchone()['plan']
        # Historical part must remain readable; reject only the next INSERT so
        # the failure happens after repair, inside the transaction under test.
        conn.execute('ALTER TABLE dd_agent.assembly_handoff_parts ADD CONSTRAINT reject_repair CHECK(end_parent<1) NOT VALID')
    with pytest.raises(psycopg.errors.CheckViolation):reconcile(x)
    with x.db.connect() as conn:
        assert conn.execute('SELECT plan FROM dd_agent.parent_index_work').fetchone()['plan']==original
        assert conn.execute("SELECT count(*) AS n FROM dd_agent.stage_jobs WHERE job_id=%s AND stage='embedding'",(x.job['id'],)).fetchone()['n']==0
        conn.execute('ALTER TABLE dd_agent.assembly_handoff_parts DROP CONSTRAINT reject_repair')
    assert reconcile(x)['registered']==1


def test_source_change_after_reuse_is_stale_not_a_cache_miss(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url);reconcile(x);next_job(x)
    prepare=x.bridge._prepare
    def change(*args):
        result=prepare(*args)
        x.db.save_page(x.d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='新的来源'))
        return result
    monkeypatch.setattr(x.bridge,'_prepare',change)
    result=reconcile(x)
    assert result['stale']==1 and result['registered']==0 and x.index.budget.calls==0
    assert scalar(x,'assembly_handoff_parts')==1


def test_multiple_compatible_partitions_are_reprepared_instead_of_guessed(tmp_path,database_url):
    x=sample(tmp_path,database_url,many=25);reconcile(x,limit=1);next_job(x)
    # Two valid historical jobs use different bounded partitions of one receipt.
    with x.db.connect() as conn:
        parent=x.bridge._parent(conn,x.job['id'],x.job['run_epoch'])
        row=x.bridge._next(conn,parent,x.index.version())
    parents,deps=x.bridge._receipt(row)
    chosen=parents[:1]
    plan=x.index.prepare(x.p,x.d,chosen,x.bridge._snapshots(parent,chosen))
    x.bridge._commit(x.job['id'],x.job['run_epoch'],row,x.index.version(),deps,plan=plan,end=1)
    next_job(x)
    with capture(tmp_path/'metrics','ambiguity') as trace:
        assert reconcile(x,limit=1)['registered']==1
    assert trace.counters['assembly.handoff.plan_ambiguous']==1
    assert x.index.budget.calls>0


@pytest.mark.parametrize('scale',[1,10])
def test_whitespace_and_token_subdivision_do_not_invalidate_compatible_plan(tmp_path,database_url,scale):
    x=sample(tmp_path,database_url,advance=False,scale=scale)
    text='\n\n'+'甲'*680+'\n\n'+'乙'*1900+'\n\n'
    x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text=text))
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1,2],ready_pages={1,2}),expected_generation=0,final=True)
    reconcile(x);next_job(x);x.index.budget=Budget(scale)
    assert reconcile(x)['registered']==1 and x.index.budget.calls==0
    claim=x.manager.stages.claim(x.job['id'],'embedding')
    ParentWork(x.index,x.manager.stages).run(claim['id'],claim['owner_token'])
