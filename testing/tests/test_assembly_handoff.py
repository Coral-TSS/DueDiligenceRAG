"""Atomic assembly outbox and bounded, restartable receipt-to-work handoff."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
import importlib
import os
import pytest

from dd_agent.assembly_store import AssemblyStore
from dd_agent.parent_index import ParentIndex
from dd_agent.parent_work import ParentWork
from dd_agent.pipeline import JobManager
from dd_agent.stage_publication import StageSuperseded
from test_parent_index import Models,Budget
from test_retrieval import setup


def api():
    try:return importlib.import_module('dd_agent.assembly_handoff')
    except ModuleNotFoundError:pytest.fail('Reliable assembly receipt handoff is missing')


def sample(tmp_path,database_url,*,advance=True,many=0,scale=1):
    module=api()
    app,p,d,evidence,_=setup(tmp_path,database_url)
    db=app.state.database;db.set_reading(d,2,'scan-v1')
    first='公司应提供材料，' if not many else '\n\n'.join(f'第{i}条 '+'甲'*40 for i in range(1,many+1))
    for n,text in [(1,first),(2,'并说明注册资本变化。')]:
        db.save_page(d,'scan-v1',dict(page_number=n,status='succeeded',method='text',text=text))
    index=ParentIndex(evidence.settings,database=db,models=Models(),budget=Budget(scale))
    manager=JobManager(evidence.settings,database=db);job=manager.enqueue(p,d,wake=False)
    x=SimpleNamespace(db=db,p=p,d=d,index=index,manager=manager,job=job,store=AssemblyStore(db))
    x.bridge=module.ReceiptHandoff(index,manager.stages)
    if advance:x.store.advance(p,d,x.store.snapshots(p,d,[1,2],ready_pages={1,2}),expected_generation=0,final=True)
    return x


def reconcile(x,**kwargs):return x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch'],**kwargs)


def scalar(x,table):
    with x.db.connect() as conn:return conn.execute(f'SELECT count(*) AS n FROM dd_agent.{table}').fetchone()['n']


def test_receipt_and_outbox_roll_back_together(tmp_path,database_url):
    import psycopg
    x=sample(tmp_path,database_url,advance=False)
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1],ready_pages={1}),expected_generation=0)
    with x.db.connect() as conn:conn.execute('ALTER TABLE dd_agent.assembly_checkpoints ADD CONSTRAINT reject_probe CHECK(generation<2)')
    with pytest.raises(psycopg.errors.CheckViolation):
        x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[2],ready_pages={2}),expected_generation=1,final=True)
    assert scalar(x,'assembly_receipt_outbox')==scalar(x,'assembly_batches')==1


def test_migration_backfills_old_receipts_once_and_reconcile_is_idempotent(tmp_path,database_url):
    x=sample(tmp_path,database_url,advance=False)
    with x.db.connect() as conn:conn.execute('DROP TRIGGER assembly_receipt_saved ON dd_agent.assembly_batches')
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1,2],ready_pages={1,2}),expected_generation=0,final=True)
    assert scalar(x,'assembly_receipt_outbox')==0
    x.db.migrate();x.db.migrate()
    assert scalar(x,'assembly_receipt_outbox')==1
    result=reconcile(x)
    assert result['registered']==1 and result['delivered']==1 and not result['has_more']
    assert reconcile(x)['registered']==0
    assert scalar(x,'assembly_handoff_parts')==scalar(x,'parent_index_work')==1


def test_unsealed_tail_has_no_false_embedding_and_next_window_is_delivered(tmp_path,database_url):
    x=sample(tmp_path,database_url,advance=False)
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1],ready_pages={1}),expected_generation=0)
    assert reconcile(x)['empty']==1
    assert x.index.budget.calls==x.index.models.calls==0 and scalar(x,'parent_index_work')==0
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[2],ready_pages={2}),expected_generation=1,final=True)
    assert reconcile(x)['registered']==1
    claim=x.manager.stages.claim(x.job['id'],'embedding')
    identity=ParentWork(x.index,x.manager.stages).run(claim['id'],claim['owner_token'])
    assert x.index.metadata(x.p,x.d,identity)['current']
    with x.db.connect() as conn:
        text=conn.execute('SELECT text FROM dd_agent.parent_index_parents WHERE build_id=%s',(identity,)).fetchone()['text']
    assert text=='公司应提供材料，并说明注册资本变化。'


def test_registration_and_cursor_ack_roll_back_as_one_transaction(tmp_path,database_url):
    import psycopg
    x=sample(tmp_path,database_url)
    with x.db.connect() as conn:conn.execute('ALTER TABLE dd_agent.assembly_handoff_parts ADD CONSTRAINT reject_probe CHECK(end_parent<1)')
    with pytest.raises(psycopg.errors.CheckViolation):reconcile(x)
    assert scalar(x,'parent_index_work')==scalar(x,'assembly_handoff_parts')==scalar(x,'assembly_handoffs')==0
    with x.db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM dd_agent.stage_jobs WHERE stage='embedding'").fetchone()['n']==0
        conn.execute('ALTER TABLE dd_agent.assembly_handoff_parts DROP CONSTRAINT reject_probe')
    assert reconcile(x)['registered']==1


def test_large_receipt_is_handed_off_in_bounded_parts_and_new_instance_continues(tmp_path,database_url):
    x=sample(tmp_path,database_url,many=45)
    first=reconcile(x,limit=1)
    assert first['registered']==1 and first['has_more'] and first['delivered']==0
    x.bridge=api().ReceiptHandoff(x.index,x.manager.stages)
    second=reconcile(x,limit=1);third=reconcile(x,limit=1)
    assert second['registered']==third['registered']==1
    assert third['delivered']==1 and not third['has_more']
    with x.db.connect() as conn:
        parts=conn.execute('SELECT start_parent,end_parent FROM dd_agent.assembly_handoff_parts ORDER BY start_parent').fetchall()
    assert parts==[dict(start_parent=0,end_parent=20),dict(start_parent=20,end_parent=40),dict(start_parent=40,end_parent=46)]


def test_new_epoch_reuses_old_part_without_tokenization_and_reuses_saved_vectors(tmp_path,database_url):
    x=sample(tmp_path,database_url);reconcile(x)
    old=x.manager.stages.claim(x.job['id'],'embedding')
    work=ParentWork(x.index,x.manager.stages)
    work.save_vectors(old['id'],old['owner_token'],x.index.embed(work.load(old['id'],old['owner_token'])))
    x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
    x.index=ParentIndex(x.index.settings,database=x.db,models=Models(),budget=Budget())
    x.bridge=api().ReceiptHandoff(x.index,x.manager.stages)
    assert reconcile(x)['registered']==1 and x.index.budget.calls==0
    new=x.manager.stages.claim(x.job['id'],'embedding')
    identity=ParentWork(x.index,x.manager.stages).run(new['id'],new['owner_token'])
    assert x.index.metadata(x.p,x.d,identity)['current'] and x.index.models.calls==0


@pytest.mark.parametrize('published',[False,True])
def test_reset_fences_old_origins_until_identical_work_is_explicitly_readopted(tmp_path,database_url,published):
    x=sample(tmp_path,database_url);reconcile(x)
    task=x.manager.stages.claim(x.job['id'],'embedding')
    work=ParentWork(x.index,x.manager.stages);computed=x.index.embed(work.load(task['id'],task['owner_token']))
    work.save_vectors(task['id'],task['owner_token'],computed)
    if published:x.index.publish(x.manager.stages,task['id'],task['owner_token'],computed)
    x.store.restart(x.p,x.d,expected_generation=1)
    if published:assert not x.index.metadata(x.p,x.d,computed['id'])['current']
    with pytest.raises(StageSuperseded):x.index.publish(x.manager.stages,task['id'],task['owner_token'],computed)
    if not published:
        with pytest.raises(StageSuperseded):work.load(task['id'],task['owner_token'])
    assert reconcile(x)['empty']==1
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1,2],ready_pages={1,2}),expected_generation=2,final=True)
    assert reconcile(x)['registered']==1
    if not published:work.run(task['id'],task['owner_token'])
    assert x.index.metadata(x.p,x.d,computed['id'])['current']


def test_reset_during_preparation_cannot_register_obsolete_work(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url);real=x.index.prepare
    def reset(*a,**k):
        result=real(*a,**k);x.store.restart(x.p,x.d,expected_generation=1);return result
    monkeypatch.setattr(x.index,'prepare',reset)
    result=reconcile(x,limit=1)
    assert result['superseded']==1 and result['registered']==0
    assert scalar(x,'parent_index_work')==0


def test_source_change_during_preparation_marks_stale_and_does_not_register(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url);real=x.index.prepare
    def change(*a,**k):
        result=real(*a,**k)
        x.db.save_page(x.d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='来源变化'))
        return result
    monkeypatch.setattr(x.index,'prepare',change)
    result=reconcile(x)
    assert result['stale']==1 and result['registered']==0 and not result['has_more']
    assert scalar(x,'parent_index_work')==0 and x.index.models.calls==0


def test_concurrent_handoff_creates_only_one_part_and_one_task(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url);real=x.index.prepare;barrier=Barrier(2)
    def overlap(*a,**k):
        result=real(*a,**k);barrier.wait(15);return result
    monkeypatch.setattr(x.index,'prepare',overlap)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:reconcile(x,limit=1),range(2)))
    assert sum(r['registered'] for r in results)==1
    assert scalar(x,'parent_index_work')==scalar(x,'assembly_handoff_parts')==1


def test_stale_epoch_and_invalid_limits_cannot_advance_handoff(tmp_path,database_url):
    x=sample(tmp_path,database_url)
    with pytest.raises(StageSuperseded):x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch']+1)
    for limit in (0,21,False):
        with pytest.raises(ValueError):reconcile(x,limit=limit)
    with pytest.raises(ValueError):x.bridge.reconcile(uuid4(),expected_epoch=1)
    assert scalar(x,'assembly_handoffs')==0


def test_receipt_preserves_all_input_dependencies_even_when_every_page_is_blank(tmp_path,database_url):
    app,p,d,_,_=setup(tmp_path,database_url);db=app.state.database
    for n in (1,2,3):db.save_page(d,'scan-v1',dict(page_number=n,status='succeeded',method='text',text=''))
    store=AssemblyStore(db)
    result=store.advance(p,d,store.snapshots(p,d,[1,2,3],ready_pages={1,2,3}),expected_generation=0,final=True)
    assert [dep['page_number'] for dep in result.get('input_dependencies',[])]==[1,2,3]


def test_adaptive_part_size_respects_actual_child_token_budget(tmp_path,database_url):
    x=sample(tmp_path,database_url,advance=False,scale=10)
    text='\n\n'.join(f'第{i}条 '+'甲'*2900 for i in range(1,21))
    x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text=text))
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1,2],ready_pages={1,2}),expected_generation=0,final=True)
    result=reconcile(x,limit=1)
    assert result['registered']==1 and result['has_more'] and x.index.models.calls==0
    with x.db.connect() as conn:
        part=conn.execute('SELECT end_parent,build_id FROM dd_agent.assembly_handoff_parts').fetchone()
        plan=conn.execute('SELECT plan FROM dd_agent.parent_index_work WHERE id=%s',(part['build_id'],)).fetchone()['plan']
    assert 0<part['end_parent']<20 and len(plan['children'])<=128
    assert all(c['embedding_tokens']<=4096 and c['reranker_tokens']<=2048 for c in plan['children'])


def test_changed_earlier_blank_page_is_not_silently_acked_as_empty(tmp_path,database_url):
    x=sample(tmp_path,database_url,advance=False)
    for n in (1,2):x.db.save_page(x.d,'scan-v1',dict(page_number=n,status='succeeded',method='text',text=''))
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1,2],ready_pages={1,2}),expected_generation=0,final=True)
    x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='新增正文'))
    result=reconcile(x)
    assert result['stale']==1 and result['empty']==result['registered']==0
    assert x.index.budget.calls==x.index.models.calls==0


def test_real_receipt_handoff_through_qwen_and_pgvector(tmp_path,database_url):
    if os.getenv('DDA_TEST_LOCAL_MODELS')!='1':pytest.skip('Enable real receipt handoff integration')
    from dd_agent.retrieval import LocalModels
    from dd_agent.token_budget import LocalTokenBudget
    from dd_agent.model_runtime import close_runtime
    x=sample(tmp_path,database_url)
    x.index.models=LocalModels(x.index.settings);x.index.budget=LocalTokenBudget(x.index.settings)
    try:
        assert reconcile(x)['registered']==1
        taken=x.manager.stages.claim(x.job['id'],'embedding')
        identity=ParentWork(x.index,x.manager.stages).run(taken['id'],taken['owner_token'])
        meta=x.index.metadata(x.p,x.d,identity)
        assert meta['current'] and meta['parents']==meta['children']==1
        assert not reconcile(x)['has_more']
    finally:close_runtime()
