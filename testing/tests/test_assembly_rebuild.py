"""Durable local recomputation plans; the current receipt lineage never changes."""
import importlib
from uuid import uuid4

import pytest

from dd_agent.assembly import VERSION
from dd_agent.control import ProcessingPaused
from dd_agent.stage_publication import StageSuperseded,StagePaused
from test_assembly_ingestion import sample as source_sample,machine,VISION


def module():
    try:return importlib.import_module('dd_agent.assembly_rebuild')
    except ModuleNotFoundError:pytest.fail('Durable local rebuild preparation is missing')


def sample(tmp_path,database_url,total=30):
    api=module()
    x=source_sample(tmp_path,database_url,total=total)
    generation=0
    for start in range(1,total+1,10):
        numbers=list(range(start,min(total+1,start+10)))
        result=x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,numbers,ready_pages=set(numbers)),
            expected_generation=generation,final=numbers[-1]==total)
        generation=result['generation']
    x.rebuild=api.AssemblyRebuild(x.manager.stages)
    x.original=x.store.load(x.p,x.d)
    return x


def change(x,number,*,enhance=True):
    x.db.save_page(x.d,'scan-v1',dict(page_number=number,status='succeeded',method='text',text=f'并说明第{number}页修订资料。'))
    if enhance:machine(x,[number])


def start(x):
    return x.rebuild.start(x.job['id'],expected_epoch=x.job['run_epoch'],vision_policy=VISION)


def scan(x,plan,limit=4):
    return x.rebuild.scan(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'],limit=limit)


def prepare(x,plan,**kwargs):
    return x.rebuild.prepare_next(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'],**kwargs)


def manifest(x,plan,**kwargs):
    return x.rebuild.manifest(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'],**kwargs)


def test_scan_is_bounded_restartable_and_clean_plan_recomputes_nothing(tmp_path,database_url):
    x=sample(tmp_path,database_url)
    plan=start(x)
    first=scan(x,plan,limit=1)
    assert first['status']=='scanning' and first['receipts']==1 and first['source_pages']==10
    x.rebuild=module().AssemblyRebuild(x.manager.stages)
    assert scan(x,plan,limit=1)['receipts']==2
    final=scan(x,plan,limit=1)
    assert final['status']=='clean' and final['dirty_receipts']==0
    assert prepare(x,plan)['recomputed_receipts']==0
    assert x.store.load(x.p,x.d)==x.original
    assert x.index.models.calls==x.index.budget.calls==0


def test_middle_change_replaces_one_window_and_preserves_prefix_and_suffix(tmp_path,database_url):
    x=sample(tmp_path,database_url);change(x,12)
    plan=start(x);assert scan(x,plan)['dirty_receipts']==1
    result=prepare(x,plan)
    assert result['status']=='prepared' and result['recomputed_receipts']==1 and result['kept_receipts']==2
    items=manifest(x,plan)['items']
    assert [(r['receipt_generation'],r['action']) for r in items]==[(1,'keep'),(2,'replace'),(3,'keep')]
    assert x.store.load(x.p,x.d)==x.original
    with x.db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches WHERE document_id=%s',(x.d,)).fetchone()['n']==3
    assert x.index.models.calls==x.index.budget.calls==0


def test_changed_tail_propagates_until_next_stable_boundary(tmp_path,database_url):
    x=sample(tmp_path,database_url);change(x,10)
    plan=start(x);scan(x,plan)
    first=prepare(x,plan)
    assert first['status']=='preparing' and first['recomputed_receipts']==1
    final=prepare(x,plan)
    assert final['status']=='prepared' and final['recomputed_receipts']==2 and final['regions']==1
    assert [r['action'] for r in manifest(x,plan)['items']]==['replace','replace','keep']


def test_sparse_changes_skip_the_unchanged_middle_window(tmp_path,database_url):
    x=sample(tmp_path,database_url);change(x,2);change(x,22)
    plan=start(x);scan(x,plan)
    assert prepare(x,plan)['status']=='preparing'
    final=prepare(x,plan)
    assert final['status']=='prepared' and final['regions']==2 and final['recomputed_receipts']==2
    assert [r['action'] for r in manifest(x,plan)['items']]==['replace','keep','replace']


def test_last_page_change_finishes_at_document_end_without_false_convergence(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,20)
    plan=start(x);scan(x,plan)
    assert prepare(x,plan)['status']=='prepared'
    part=x.rebuild.replacement(x.job['id'],plan['id'],2,expected_epoch=x.job['run_epoch'])
    assert part['state']['closed'] and part['state']['last']['source_revision']!=x.original['state']['last']['source_revision']
    assert x.store.load(x.p,x.d)==x.original


def test_source_changes_again_invalidate_the_frozen_plan(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan);change(x,2)
    result=prepare(x,plan)
    assert result['status']=='superseded' and result['error_code']=='source_changed' and result['recomputed_receipts']==0


def test_baseline_reset_cannot_be_overwritten_by_old_plan(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan)
    current=x.store.restart(x.p,x.d,expected_generation=x.original['generation'])
    result=prepare(x,plan)
    assert result['status']=='superseded' and x.store.load(x.p,x.d)==current


def test_explicit_resume_keeps_prepared_parts_and_rejects_old_epoch(tmp_path,database_url):
    x=sample(tmp_path,database_url);change(x,2);change(x,22)
    plan=start(x);scan(x,plan);prepare(x,plan)
    epoch=x.job['run_epoch']
    x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
    fresh=module().AssemblyRebuild(x.manager.stages)
    restored=fresh.resume(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'])
    assert restored['recomputed_receipts']==1
    with pytest.raises(StageSuperseded):x.rebuild.prepare_next(x.job['id'],plan['id'],expected_epoch=epoch)
    x.rebuild=fresh
    assert prepare(x,plan)['status']=='prepared'


@pytest.mark.parametrize('global_pause',[True,False])
def test_pause_prevents_local_preparation_writes(tmp_path,database_url,global_pause):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan)
    if global_pause:x.manager.pause(x.p,x.d)
    else:x.manager.control_stage(x.p,x.d,'embedding',pause=True)
    with pytest.raises(ProcessingPaused if global_pause else StagePaused):prepare(x,plan)
    assert x.store.load(x.p,x.d)==x.original


def test_waiting_vision_does_not_use_changed_raw_text(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2,enhance=False)
    plan=start(x);scan(x,plan)
    result=prepare(x,plan)
    assert result['status']=='ready' and result['waiting']=='vision' and result['recomputed_receipts']==0
    assert x.index.models.calls==x.index.budget.calls==0


def test_source_change_during_pure_assembly_cannot_commit_part(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan)
    actual=module().assemble
    def changed(*a,**k):
        result=actual(*a,**k);change(x,2);return result
    monkeypatch.setattr(module(),'assemble',changed)
    result=prepare(x,plan)
    assert result['status']=='superseded' and result['recomputed_receipts']==0


def test_historical_missing_metadata_blocks_instead_of_guessing(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20)
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_batches SET result=result-'input_dependencies' WHERE document_id=%s AND generation=1",(x.d,))
    plan=start(x)
    result=scan(x,plan)
    assert result['status']=='blocked' and result['error_code']=='legacy_receipt_unverified'


def test_manifest_pages_and_wrong_job_are_bounded(tmp_path,database_url):
    x=sample(tmp_path,database_url);change(x,12)
    plan=start(x);scan(x,plan);prepare(x,plan)
    first=manifest(x,plan,limit=1)
    assert len(first['items'])==1 and first['has_more'] and first['next_cursor']==1
    assert manifest(x,plan,after_generation=first['next_cursor'],limit=1)['items'][0]['receipt_generation']==2
    with pytest.raises(ValueError):manifest(x,plan,limit=0)
    with pytest.raises(ValueError):x.rebuild.manifest(uuid4(),plan['id'],expected_epoch=x.job['run_epoch'])


def test_failed_save_rolls_back_and_exhaustion_blocks_instead_of_waiting_forever(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan)
    original=x.rebuild._save_part
    def broken(*args):
        original(*args)
        raise RuntimeError('synthetic commit failure')
    monkeypatch.setattr(x.rebuild,'_save_part',broken)
    for _ in range(4):
        result=prepare(x,plan)
        assert result['recomputed_receipts']==0
        assert x.store.load(x.p,x.d)==x.original
        with x.db.connect() as conn:
            conn.execute("UPDATE dd_agent.stage_jobs SET available_at=clock_timestamp() WHERE job_id=%s AND scope ? 'assembly_rebuild'",(x.job['id'],))
    assert result['status']=='blocked' and result['error_code']=='rebuild_save_failed'


def test_source_change_between_scan_batches_supersedes_plan(tmp_path,database_url):
    x=sample(tmp_path,database_url)
    plan=start(x);scan(x,plan,limit=1);change(x,2)
    result=scan(x,plan,limit=1)
    assert result['status']=='superseded' and result['receipts']==1


def test_pause_during_preparation_cannot_publish(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan)
    calls=0
    def pause(*args):
        nonlocal calls
        calls+=1
        if calls==2:x.manager.pause(x.p,x.d)
    with pytest.raises(ProcessingPaused):prepare(x,plan,checkpoint=pause)
    assert x.store.load(x.p,x.d)==x.original
    with x.db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_rebuild_parts WHERE plan_id=%s',(plan['id'],)).fetchone()['n']==0


def test_repeated_migration_preserves_prepared_results(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan);prepare(x,plan)
    before=manifest(x,plan)
    x.db.migrate()
    assert manifest(x,plan)==before


def test_corrupt_replacement_is_rejected(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);change(x,2)
    plan=start(x);scan(x,plan);prepare(x,plan)
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_rebuild_parts SET seal=repeat('0',64) WHERE plan_id=%s",(plan['id'],))
    with pytest.raises(ValueError):x.rebuild.replacement(x.job['id'],plan['id'],1,expected_epoch=x.job['run_epoch'])


@pytest.mark.parametrize('count,size,reason',[(1,150000,'assembly_input_too_large'),(4,60000,'character_budget')])
def test_text_over_snapshot_budget_blocks_cleanly(tmp_path,database_url,count,size,reason):
    x=sample(tmp_path,database_url,total=20)
    for number in range(2,2+count):
        x.db.save_page(x.d,'scan-v1',dict(page_number=number,status='succeeded',method='text',text='资'*size))
    machine(x,range(2,2+count));plan=start(x);scan(x,plan)
    result=prepare(x,plan)
    assert result['status']=='blocked' and result['error_code']==reason
    assert result['recomputed_receipts']==0 and x.store.load(x.p,x.d)==x.original


def test_malformed_dependency_is_blocked_without_guessing(tmp_path,database_url):
    from psycopg.types.json import Jsonb
    x=sample(tmp_path,database_url,total=20)
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_batches SET result=jsonb_set(result,'{input_dependencies}',%s) WHERE document_id=%s AND generation=1",(Jsonb(['corrupt']),x.d))
    plan=start(x);result=scan(x,plan)
    assert result['status']=='blocked' and result['error_code']=='invalid_history'
