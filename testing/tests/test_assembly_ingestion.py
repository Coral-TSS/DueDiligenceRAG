"""Bounded automatic windows, readiness and receipt/work drain against real SQL."""
import importlib
import os
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from dd_agent.parent_work import ParentWork
from dd_agent.control import ProcessingPaused
from dd_agent.stage_publication import StagePaused
from test_assembly_handoff import sample as base

VISION='synthetic-vision-v1'


def driver(x,**kwargs):
    try:module=importlib.import_module('dd_agent.assembly_ingestion')
    except ModuleNotFoundError:pytest.fail('Automatic assembly window coordinator is missing')
    return module.AssemblyIngestion(x.index,x.manager.stages,**kwargs)


def sample(tmp_path,database_url,total=13,*,machines=True,blank=False,**kwargs):
    x=base(tmp_path,database_url,advance=False)
    x.db.set_reading(x.d,total,'scan-v1')
    for n in range(1,total+1):
        text='  ' if blank else '公司应提供材料，' if n==1 else '并说明注册资本变化。' if n==2 else f'第{n}页股东资料。'
        x.db.save_page(x.d,'scan-v1',dict(page_number=n,status='succeeded',method='text',text=text))
    if machines:machine(x,range(1,total+1))
    x.driver=driver(x,**kwargs)
    return x


def machine(x,numbers):
    with x.db.connect() as conn:
        for n in numbers:
            row=x.db.page_row(x.d,n)
            conn.execute('''INSERT INTO dd_agent.machine_pages(id,document_id,page_number,base_revision,profile_version,result)
                VALUES(%s,%s,%s,%s,%s,%s)''',
                (uuid4(),x.d,n,row['raw_revision'],VISION,Jsonb(dict(text=row['text'],verification='unverified'))))


def step(x,*,reading_done=True,flush=True,**kwargs):
    return x.driver.step(x.job['id'],expected_epoch=x.job['run_epoch'],vision_policy=VISION,
        reading_done=reading_done,flush=flush,**kwargs)


def drain(x):
    worker=ParentWork(x.index,x.manager.stages)
    for _ in range(20):
        task=x.manager.stages.claim(x.job['id'],'embedding')
        if task is None:return
        worker.run(task['id'],task['owner_token'])
    pytest.fail('Unbounded work queue')


def test_ten_page_window_final_tail_and_receipts_must_all_drain(tmp_path,database_url):
    x=sample(tmp_path,database_url)
    first=step(x)
    assert first['saved_pages']==10 and not first['closed'] and not first['terminal']
    assert first['active_work']>0 and not first['handoff_pending']
    drain(x)
    second=step(x)
    assert second['saved_pages']==13 and second['closed'] and not second['terminal']
    drain(x)
    final=step(x)
    assert final['terminal'] and final['failed']==0 and final['covered_pages']==13
    assert x.store.load(x.p,x.d)['state']['tail'] is None


def test_running_vision_is_a_barrier_even_if_later_pages_are_ready(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2,machines=False)
    machine(x,[2])
    result=step(x)
    assert result['saved_pages']==0 and not result['terminal'] and result['waiting']=='vision'
    assert x.store.load(x.p,x.d)['generation']==0 and x.index.models.calls==0
    machine(x,[1])
    assert step(x)['saved_pages']==2


def test_partial_window_requires_flush_and_can_close_without_rereading(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=3,machines=False)
    machine(x,[1,2])
    assert step(x,reading_done=False,flush=False)['waiting']=='window'
    assert x.store.load(x.p,x.d)['generation']==0
    assert step(x,reading_done=False,flush=True)['saved_pages']==2
    machine(x,[3])
    assert step(x,reading_done=False)['saved_pages']==3
    drain(x)
    result=step(x,reading_done=True)
    assert result['closed'] and result['saved_pages']==3


def test_work_backpressure_stops_new_windows_until_previous_packet_finishes(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=25,max_pending=1)
    assert step(x)['saved_pages']==10
    generation=x.store.load(x.p,x.d)['generation']
    result=step(x)
    assert result['waiting']=='backpressure' and result['active_work']==1
    assert x.store.load(x.p,x.d)['generation']==generation
    drain(x)
    assert step(x)['saved_pages']==20


@pytest.mark.parametrize('kind',['failed','missing','vision_failed'])
def test_terminal_gaps_are_boundaries_and_report_partial(tmp_path,database_url,kind):
    x=sample(tmp_path,database_url,total=3,machines=False)
    if kind=='missing':
        with x.db.connect() as conn:conn.execute('DELETE FROM dd_agent.document_pages WHERE document_id=%s AND page_number=2',(x.d,))
    elif kind=='failed':x.db.save_page(x.d,'scan-v1',dict(page_number=2,status='failed',method='ocr',text='',error_code='synthetic'))
    else:
        row=x.db.page_row(x.d,2)
        task=x.manager.stages.ensure(x.job['id'],'vision',dict(pages=[2]),source_revision=row['raw_revision'],policy_version=VISION,
            dependencies=[dict(page_number=2,view='raw',revision=row['raw_revision'])])
        taken=x.manager.stages.claim(x.job['id'],'vision',task_id=task['id'])
        x.manager.stages.fail(taken['id'],taken['owner_token'],category='permanent',error_code='synthetic')
    machine(x,[1,3])
    assert step(x)['closed']
    drain(x)
    result=step(x)
    assert result['terminal'] and result['failed']>0 and result['gap_pages']==1
    with x.db.connect() as conn:
        texts=conn.execute('SELECT text FROM dd_agent.parent_index_children').fetchall()
    assert all('注册资本变化' not in row['text'] for row in texts)


def test_blank_document_needs_no_tokenizer_or_embedding(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2,blank=True)
    result=step(x)
    assert result['terminal'] and result['closed'] and result['covered_pages']==2
    assert x.index.models.calls==0 and x.index.budget.calls==0


def test_resume_restores_receipts_and_vectors_without_retokenizing(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=13,machines=False)
    machine(x,range(1,11))
    step(x,reading_done=False)
    task=x.manager.stages.claim(x.job['id'],'embedding')
    work=ParentWork(x.index,x.manager.stages)
    work.save_vectors(task['id'],task['owner_token'],x.index.embed(work.load(task['id'],task['owner_token'])))
    calls=x.index.budget.calls;embeds=x.index.models.calls
    x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
    x.driver=driver(x)
    result=step(x,reading_done=False,flush=False)
    assert result['saved_pages']==10 and not result['closed']
    drain(x)
    assert x.index.budget.calls==calls and x.index.models.calls==embeds


def test_changed_earlier_source_blocks_closed_document_without_restart(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    step(x);drain(x)
    assert step(x)['terminal']
    generation=x.store.load(x.p,x.d)['generation']
    x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='更新后的公司资料'))
    result=step(x)
    assert result['terminal'] and result['failed']>0 and result['waiting']=='stale_sources'
    assert x.store.load(x.p,x.d)['generation']==generation


def test_closed_historical_partial_range_cannot_claim_whole_document_coverage(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[2],ready_pages={2}),expected_generation=0,final=True)
    step(x);drain(x)
    result=step(x)
    assert result['terminal'] and result['failed']>0 and result['waiting']=='incomplete_coverage'


@pytest.mark.parametrize('global_pause',[True,False])
def test_paused_driver_cannot_advance_or_register_more_work(tmp_path,database_url,global_pause):
    x=sample(tmp_path,database_url,total=2)
    if global_pause:x.manager.pause(x.p,x.d)
    else:x.manager.control_stage(x.p,x.d,'embedding',pause=True)
    with pytest.raises(ProcessingPaused if global_pause else StagePaused):step(x)
    assert x.store.load(x.p,x.d)['generation']==0


def test_real_automatic_window_through_qwen_and_pgvector(tmp_path,database_url):
    if os.getenv('DDA_TEST_LOCAL_MODELS')!='1':pytest.skip('Enable real automatic window integration')
    from dd_agent.retrieval import LocalModels
    from dd_agent.token_budget import LocalTokenBudget
    from dd_agent.model_runtime import close_runtime
    x=sample(tmp_path,database_url,total=2)
    x.index.models=LocalModels(x.index.settings);x.index.budget=LocalTokenBudget(x.index.settings)
    x.driver=driver(x)
    try:
        assert step(x)['closed']
        drain(x)
        result=step(x)
        assert result['terminal'] and result['failed']==0 and result['covered_pages']==2
        with x.db.connect() as conn:
            row=conn.execute('SELECT id FROM dd_agent.parent_index_builds WHERE document_id=%s',(x.d,)).fetchone()
        metadata=x.index.metadata(x.p,x.d,row['id'])
        assert metadata['current'] and metadata['parents']==metadata['children']==1
    finally:close_runtime()
