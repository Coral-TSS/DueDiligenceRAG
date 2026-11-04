"""Persistent one-step repair orchestration against isolated PostgreSQL."""
import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from dd_agent.control import ProcessingPaused,ProcessingStopped
from test_assembly_rebuild import sample,change
from test_assembly_ingestion import VISION,machine,drain
from test_stage_scheduler import cleanup


def service(x):
    try:m=importlib.import_module('dd_agent.assembly_repair')
    except ModuleNotFoundError:pytest.fail('Persistent automatic repair is missing')
    return m.AssemblyRepair(x.manager.stages)


def step(x,api=None,**kwargs):
    return (api or service(x)).step(x.job['id'],expected_epoch=x.job['run_epoch'],vision_policy=VISION,reading_done=True,**kwargs)


def finish(x,api=None):
    api=api or service(x)
    for _ in range(40):
        r=step(x,api)
        if r['terminal']:return r
    pytest.fail('Repair did not stop within bounded steps')


def test_automatic_repair_runs_all_phases_and_restarts_without_models(tmp_path,database_url):
    x=sample(tmp_path,database_url);change(x,12)
    phases=[]
    for _ in range(30):
        r=step(x);phases.append(r['phase'])
        if r['terminal']:break
    assert r['phase']=='done' and r['failed']==0 and r['attempts']==1
    assert {'probing','scanning','preparing','validating','adopting','done'}.issubset(phases)
    assert x.store.load(x.p,x.d)['generation']==4
    assert x.index.models.calls==x.index.budget.calls==0


def test_reading_and_unresolved_vision_wait_without_spending_plan_budget(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2,enhance=False);api=service(x)
    r=api.step(x.job['id'],expected_epoch=x.job['run_epoch'],vision_policy=VISION,reading_done=False)
    assert r['waiting']=='reading' and r['attempts']==0
    for _ in range(3):r=step(x,api)
    assert r['waiting']=='vision' and r['attempts']==0 and not r['terminal']
    machine(x,[2]);assert finish(x)['phase']=='done'


def test_pause_resume_preserves_plan_and_rejects_old_epoch(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2);api=service(x)
    for _ in range(15):
        r=step(x,api)
        if r['phase']=='validating':break
    assert r['plan_id']
    old=x.job['run_epoch'];x.manager.pause(x.p,x.d)
    with pytest.raises(ProcessingPaused):step(x,api)
    assert api.status(x.job['id'],expected_epoch=old)['plan_id']==r['plan_id']
    x.job=x.manager.resume(x.p,x.d,wake=False)
    with pytest.raises(ProcessingStopped):api.step(x.job['id'],expected_epoch=old,vision_policy=VISION,reading_done=True)
    final=finish(x)
    assert final['phase']=='done' and final['plan_id']==r['plan_id'] and final['attempts']==1


def test_source_churn_exhausts_durable_budget_even_after_resume(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=20);change(x,2);api=service(x)
    actual=api.rebuild.scan
    def churn(*args,**kwargs):
        r=actual(*args,**kwargs);change(x,2);return r
    monkeypatch.setattr(api.rebuild,'scan',churn)
    final=finish(x,api)
    assert final['phase']=='blocked' and final['error_code']=='repair_replan_exhausted' and final['attempts']==3
    x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
    assert step(x)['phase']=='blocked'
    assert x.store.load(x.p,x.d)==x.original


def test_concurrent_step_does_not_duplicate_start(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2);api=service(x)
    assert step(x,api)['checked_pages']==20
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _:step(x),range(2)))
    with x.db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_rebuild_plans WHERE job_id=%s',(x.job['id'],)).fetchone()['n']==1
    assert finish(x)['phase']=='done'


def test_unexpected_failures_stop_after_three_without_resetting_on_new_instance(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=20);change(x,2);api=service(x)
    step(x,api);step(x,api)
    def broken(*a,**k):raise RuntimeError('synthetic step failure')
    monkeypatch.setattr(api.rebuild,'scan',broken)
    for _ in range(3):r=step(x,api)
    assert r['phase']=='blocked' and r['error_code']=='repair_step_exhausted'
    assert step(x)['phase']=='blocked' and x.store.load(x.p,x.d)==x.original


def test_reset_blocks_old_repair_without_overwriting_new_baseline(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    step(x);step(x)
    cp=x.store.restart(x.p,x.d,expected_generation=x.original['generation'])
    r=step(x)
    assert r['phase']=='blocked' and r['error_code']=='repair_baseline_changed'
    assert x.store.load(x.p,x.d)==cp


def test_new_source_after_done_opens_new_cycle_and_migration_preserves_progress(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    first=finish(x);assert first['phase']=='done'
    x.db.migrate();assert step(x)['plan_id']==first['plan_id']
    change(x,12);second=finish(x)
    assert second['phase']=='done' and second['plan_id']!=first['plan_id'] and second['attempts']==1


def test_internal_ingestion_repairs_then_drains_current_work(tmp_path,database_url):
    try:m=importlib.import_module('dd_agent.repair_ingestion')
    except ModuleNotFoundError:pytest.fail('Repair ingestion integration is missing')
    x=sample(tmp_path,database_url,total=20);change(x,2)
    driver=m.RepairingAssemblyIngestion(x.index,x.manager.stages)
    for _ in range(40):
        r=driver.step(x.job['id'],expected_epoch=x.job['run_epoch'],vision_policy=VISION,reading_done=True,flush=True)
        if r['terminal']:break
        drain(x)
    assert r['terminal'] and r['failed']==0 and r['covered_pages']==20
    assert x.store.load(x.p,x.d)['generation']==3


def test_done_without_changes_does_not_loop_on_stale_ledger(tmp_path,database_url):
    from dd_agent.repair_ingestion import RepairingAssemblyIngestion
    x=sample(tmp_path,database_url,total=20)
    x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch'],limit=20);drain(x)
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_handoffs SET state='stale' WHERE document_id=%s",(x.d,))
    driver=RepairingAssemblyIngestion(x.index,x.manager.stages)
    for _ in range(25):
        result=driver.step(x.job['id'],expected_epoch=x.job['run_epoch'],vision_policy=VISION,reading_done=True,flush=True)
        if result['terminal']:break
    assert result['terminal'] and result['failed']==1 and result['waiting']=='repair_no_progress'


def test_crash_after_commit_recovers_ack_only_after_explicit_resume(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=20);change(x,2);api=service(x)
    for _ in range(15):
        if step(x,api)['phase']=='adopting':break
    class Crash(BaseException):pass
    saved={}
    def crash(*a,**k):saved['args']=a;raise Crash()
    monkeypatch.setattr(api,'_finish',crash)
    with pytest.raises(Crash):step(x,api)
    assert x.store.load(x.p,x.d)['generation']==3
    assert step(x)['waiting']=='owner'
    x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
    recovered=step(x)
    assert recovered['phase']=='done' and recovered['generation']==3
    assert x.store.load(x.p,x.d)['generation']==3


def test_old_inflight_probe_cannot_overwrite_new_epoch(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2);api=service(x)
    entered=Event();release=Event()
    def checkpoint(*args):entered.set();assert release.wait(15)
    with ThreadPoolExecutor(1) as pool:
        old=pool.submit(step,x,api,checkpoint=checkpoint)
        assert entered.wait(15)
        x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
        current=step(x)
        release.set()
        with pytest.raises(ProcessingStopped):old.result(15)
    assert current['checked_pages']==20 and current['attempts']==0
    assert finish(x)['phase']=='done'


def test_actual_scheduler_repairs_changed_saved_receipt_before_completion(tmp_path,database_url):
    from dd_agent.repair_ingestion import RepairingAssemblyIngestion
    from dd_agent.parent_index import ParentIndex
    from test_parent_index import Models,Budget
    from test_stage_scheduler import fixture,start
    db,manager,p,d,job,calls,_,release,evidence=fixture(tmp_path,database_url,total=2)
    pipeline=manager.factory();index=ParentIndex(manager.settings,database=db,models=Models(),budget=Budget())
    engine=RepairingAssemblyIngestion(index,manager.stages);pipeline.assembly_ingestion=engine
    actual=engine.step;changed=[]
    def dispatch(*args,**kwargs):
        control=kwargs['checkpoint']
        def checkpoint(stage,number):
            if stage=='assembly_saved' and not changed:
                changed.append(True)
                db.save_page(d,'test-reader-v1',dict(page_number=1,status='succeeded',method='text',text='新股东资料。'))
            control(stage,number)
        return actual(*args,**dict(kwargs,checkpoint=checkpoint))
    engine.step=dispatch
    thread=start(manager)
    try:thread.join(60)
    finally:
        release.set()
        if thread.is_alive():manager._stop.set();thread.join(20)
    assert not thread.is_alive()
    result=manager.latest(p,d)
    assert result['status']=='completed' and result['progress']['assembly']['covered_pages']==2
    assert len(calls['reading'])==len(set(calls['reading']))==2
    with db.connect() as conn:
        assert conn.execute('SELECT phase FROM dd_agent.assembly_repair_runs WHERE job_id=%s',(job['id'],)).fetchone()['phase']=='done'


def test_cooperative_pause_before_start_does_not_spend_plan_budget(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2);step(x)
    def pause(*args):x.manager.pause(x.p,x.d);raise ProcessingPaused()
    for _ in range(3):
        with pytest.raises(ProcessingPaused):step(x,checkpoint=pause)
        x.job=x.manager.resume(x.p,x.d,wake=False)
    result=finish(x)
    assert result['phase']=='done' and result['attempts']==1


def test_waiting_poll_defers_repeated_readiness_work_but_still_checks_pause(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=20);change(x,2,enhance=False);api=service(x)
    module=importlib.import_module('dd_agent.assembly_repair');clock=[100.]
    monkeypatch.setattr(module,'monotonic',lambda:clock[0],raising=False)
    assert step(x,api)['waiting']=='vision'
    calls=[];actual=module.resolved_prefix
    def probe(*a,**k):calls.append(True);return actual(*a,**k)
    monkeypatch.setattr(module,'resolved_prefix',probe)
    for _ in range(3):assert step(x,api)['waiting']=='vision'
    assert calls==[]
    x.manager.pause(x.p,x.d)
    with pytest.raises(ProcessingPaused):step(x,api)
    x.job=x.manager.resume(x.p,x.d,wake=False);machine(x,[2]);clock[0]+=1
    assert finish(x,api)['phase']=='done' and calls
