"""Actual producer/consumer loop with internal assembly injection; no activation flag."""
from types import SimpleNamespace

from dd_agent.parent_index import ParentIndex
from test_parent_index import Models,Budget
from test_assembly_ingestion import driver
from test_stage_scheduler import fixture,start,wait_for,cleanup


def enable(db,manager):
    pipeline=manager.factory()
    index=ParentIndex(manager.settings,database=db,models=Models(),budget=Budget())
    pipeline.assembly_ingestion=driver(SimpleNamespace(index=index,manager=manager))
    return pipeline,index


def test_actual_scheduler_publishes_both_index_families_and_drains_tail(tmp_path,database_url):
    db,manager,p,d,job,calls,_,_,evidence=fixture(tmp_path,database_url,total=13)
    pipeline,index=enable(db,manager)
    manager.drain()
    row=manager.latest(p,d)
    assert row['status']=='completed'
    assert row['progress']['assembly']['closed'] and row['progress']['assembly']['terminal']
    assert row['progress']['assembly']['covered_pages']==13
    assert row['progress']['processed_pages']==13 and len(calls['reading'])==13
    assert evidence.search(p,'注册资本')['coverage']['indexed_pages']==13
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_builds').fetchone()['n']>=2
        assert not conn.execute("SELECT 1 FROM dd_agent.stage_jobs WHERE job_id=%s AND run_epoch=%s AND state IN ('pending','running')",
            (job['id'],job['run_epoch'])).fetchone()


def test_embedding_pause_also_stops_new_assembly_windows(tmp_path,database_url):
    db,manager,p,d,_,calls,_,release,_=fixture(tmp_path,database_url,total=13)
    enable(db,manager)
    manager.control_stage(p,d,'embedding',pause=True)
    thread=start(manager)
    try:
        wait_for(lambda:len(calls['reading'])==13)
        assert calls['vision']==[]
        with db.connect() as conn:
            assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches WHERE document_id=%s',(d,)).fetchone()['n']==0
        manager.control_stage(p,d,'embedding',pause=False)
    finally:
        release.set();thread.join(30)
    assert not thread.is_alive()
    row=manager.latest(p,d)
    assert row['status']=='completed' and row['progress']['assembly']['terminal']


def test_pause_after_saved_window_restores_tail_without_repeating_reading(tmp_path,database_url):
    db,manager,p,d,_,calls,_,_,_=fixture(tmp_path,database_url,total=13)
    pipeline,index=enable(db,manager)
    actual=pipeline.assembly_ingestion.step
    stopped=[]
    def step(*a,**kwargs):
        original=kwargs['checkpoint']
        def checkpoint(stage,number):
            if stage=='assembly_saved' and not stopped:
                stopped.append(True);manager.pause(p,d)
            original(stage,number)
        return actual(*a,**dict(kwargs,checkpoint=checkpoint))
    pipeline.assembly_ingestion.step=step
    manager.drain()
    row=manager.latest(p,d)
    assert row['status']=='paused' and row['progress']['assembly']['saved_pages']==10
    before=list(calls['reading'])
    manager.resume(p,d,wake=False);manager.drain()
    row=manager.latest(p,d)
    assert row['status']=='completed' and row['progress']['assembly']['closed']
    assert len(calls['reading'])==len(set(calls['reading']))==13
    assert set(before).issubset(calls['reading'])


def test_stale_receipt_makes_actual_parent_partial(tmp_path,database_url):
    db,manager,p,d,_,_,_,_,_=fixture(tmp_path,database_url,total=2)
    pipeline,index=enable(db,manager)
    actual=pipeline.assembly_ingestion.step
    changed=[]
    def step(*a,**kwargs):
        original=kwargs['checkpoint']
        def checkpoint(stage,number):
            if stage=='assembly_saved' and not changed:
                changed.append(True)
                db.save_page(d,'test-reader-v1',dict(page_number=1,status='succeeded',method='text',text='新股东'))
            original(stage,number)
        return actual(*a,**dict(kwargs,checkpoint=checkpoint))
    pipeline.assembly_ingestion.step=step
    manager.drain()
    row=manager.latest(p,d)
    assert row['status']=='partial' and row['progress']['failed_assembly_count']>0
