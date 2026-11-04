"""Atomic lineage adoption on real SQL; no customer documents or real models."""
import importlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from dd_agent.assembly import VERSION
from dd_agent.control import ProcessingPaused
from dd_agent.stage_publication import StageSuperseded
from test_assembly_rebuild import sample,change,start,scan,prepare
from test_assembly_ingestion import drain,VISION


def service(x):
    try:api=importlib.import_module('dd_agent.assembly_adoption')
    except ModuleNotFoundError:pytest.fail('Atomic lineage adoption is missing')
    return api.AssemblyAdoption(x.manager.stages)


def ready(x,numbers=(2,)):
    for n in numbers:change(x,n)
    plan=start(x)
    for _ in range(10):
        result=scan(x,plan,limit=1)
        if result['status']!='scanning':break
    for _ in range(10):
        result=prepare(x,plan)
        if result['status'] in ('prepared','clean'):break
    assert result['status'] in ('prepared','clean')
    return plan


def validate(x,plan,api=None):
    api=api or service(x)
    for _ in range(10):
        result=api.validate_next(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'])
        if result['status']!='validating':return result
    pytest.fail('Unbounded adoption validation')


def commit(x,plan,api=None,**kwargs):
    return (api or service(x)).commit(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'],**kwargs)


def live(x):
    with x.db.connect() as conn:
        return conn.execute('''SELECT position,receipt_generation FROM dd_agent.assembly_live_receipts
            WHERE document_id=%s AND assembly_policy=%s AND NOT is_reset ORDER BY position''',(x.d,VERSION)).fetchall()


def test_atomic_adoption_preserves_keep_receipts_and_is_idempotent(tmp_path,database_url):
    x=sample(tmp_path,database_url);plan=ready(x,(12,));validate(x,plan)
    result=commit(x,plan)
    assert result['status']=='adopted' and result['generation']==4
    assert [(r['position'],r['receipt_generation']) for r in live(x)]==[(1,1),(2,4),(3,3)]
    assert commit(x,plan)==result
    assert x.store.load(x.p,x.d)['generation']==4
    with x.db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches WHERE document_id=%s',(x.d,)).fetchone()['n']==4
    assert x.index.models.calls==x.index.budget.calls==0


def test_unvalidated_plan_cannot_switch_lineage(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x)
    assert commit(x,plan)['status']=='validating'
    assert x.store.load(x.p,x.d)==x.original


def test_validation_resumes_one_part_at_a_time_and_epoch_is_fenced(tmp_path,database_url):
    x=sample(tmp_path,database_url);plan=ready(x,(2,22));api=service(x)
    first=api.validate_next(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'])
    assert first['status']=='validating' and first['validated']==1
    old=x.job['run_epoch'];x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
    x.rebuild.resume(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'])
    with pytest.raises(StageSuperseded):api.commit(x.job['id'],plan['id'],expected_epoch=old)
    assert validate(x,plan)['validated']==2
    assert commit(x,plan)['status']=='adopted'


def test_source_change_after_validation_cannot_adopt(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x);validate(x,plan);change(x,2)
    assert commit(x,plan)['status']=='superseded'
    assert x.store.load(x.p,x.d)==x.original


def test_pause_after_claim_prevents_adoption(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x);validate(x,plan)
    with pytest.raises(ProcessingPaused):commit(x,plan,checkpoint=lambda *args:x.manager.pause(x.p,x.d))
    assert x.store.load(x.p,x.d)==x.original


def test_failure_after_receipt_insert_rolls_back_everything_and_can_retry(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=20);plan=ready(x);api=service(x);validate(x,plan,api)
    actual=api._switch
    def broken(*args):
        actual(*args)
        raise RuntimeError('synthetic publication failure')
    monkeypatch.setattr(api,'_switch',broken)
    with pytest.raises(RuntimeError):commit(x,plan,api)
    assert x.store.load(x.p,x.d)==x.original
    assert [r['receipt_generation'] for r in live(x)]==[1,2]
    with x.db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches WHERE document_id=%s',(x.d,)).fetchone()['n']==2
        conn.execute("UPDATE dd_agent.stage_jobs SET available_at=clock_timestamp() WHERE job_id=%s",(x.job['id'],))
    monkeypatch.setattr(api,'_switch',actual)
    assert commit(x,plan,api)['status']=='adopted'


def test_validated_payload_tamper_cannot_publish(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x);validate(x,plan)
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_rebuild_parts SET result=jsonb_set(result,'{parents}','[]'::jsonb) WHERE plan_id=%s",(plan['id'],))
    result=commit(x,plan)
    assert result['status']=='blocked' and result['error_code']=='payload_changed'
    assert x.store.load(x.p,x.d)==x.original


def test_next_rebuild_uses_logical_order_after_middle_receipt_replacement(tmp_path,database_url):
    x=sample(tmp_path,database_url);first=ready(x,(12,));validate(x,first);commit(x,first)
    second=ready(x,(22,));validate(x,second);commit(x,second)
    assert [(r['position'],r['receipt_generation']) for r in live(x)]==[(1,1),(2,4),(3,5)]
    clean=ready(x,());assert clean['id']!=first['id']
    assert scan(x,clean)['status']=='clean'


def test_only_replacement_receipt_is_handed_off_and_old_origins_are_retired(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20)
    for _ in range(10):
        r=x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch'])
        drain(x)
        if not r['has_more']:break
    with x.db.connect() as conn:
        origins=conn.execute('SELECT receipt_generation,build_id FROM dd_agent.assembly_handoff_parts WHERE document_id=%s',(x.d,)).fetchall()
    plan=ready(x);validate(x,plan);commit(x,plan)
    with x.db.connect() as conn:
        for row in origins:
            assert conn.execute('SELECT dd_agent.parent_build_origin_current(%s) AS valid',(row['build_id'],)).fetchone()['valid']==(row['receipt_generation']==2)
    calls=x.index.budget.calls
    result=x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch'])
    assert result['delivered']==1 and result['stale']==0 and x.index.budget.calls>calls
    drain(x)
    assert not x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch'])['has_more']
    assert x.driver._coverage(x.driver._context(x.job['id'],x.job['run_epoch']))['stale'] is False


def test_competing_adoptions_reset_and_repeated_migration_are_safe(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);first=ready(x);second=ready(x,());validate(x,first);validate(x,second)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:commit(x,first),range(2)))
    assert any(r['status']=='adopted' for r in results)
    assert commit(x,second)['status']=='superseded'
    before=live(x);x.db.migrate();assert live(x)==before
    cp=x.store.load(x.p,x.d);x.store.restart(x.p,x.d,expected_generation=cp['generation'])
    assert live(x)==[]


def test_payload_change_after_claim_is_reported_immediately(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x);validate(x,plan)
    def tamper(*args):
        with x.db.connect() as conn:
            conn.execute("UPDATE dd_agent.assembly_rebuild_parts SET result=jsonb_set(result,'{parents}','[]'::jsonb) WHERE plan_id=%s",(plan['id'],))
    result=commit(x,plan,checkpoint=tamper)
    assert result['status']=='blocked' and result['error_code']=='payload_changed'
    assert x.store.load(x.p,x.d)==x.original


def test_retired_pending_build_can_be_reattached_without_reviving_old_owner(tmp_path,database_url):
    from test_assembly_ingestion import machine
    from dd_agent.parent_work import ParentWork
    x=sample(tmp_path,database_url,total=20)
    cp=x.store.restart(x.p,x.d,expected_generation=x.original['generation'])
    x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',
        text='\n\n'.join(f'第{i}条合成资料。' for i in range(50))))
    machine(x,[1]);generation=cp['generation']
    for numbers in (list(range(1,11)),list(range(11,21))):
        r=x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,numbers,ready_pages=set(numbers)),expected_generation=generation,final=numbers[-1]==20)
        generation=r['generation']
    x.original=x.store.load(x.p,x.d)
    x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch'],limit=20)
    old=x.manager.stages.claim(x.job['id'],'embedding');assert old
    with x.db.connect() as conn:
        old_ids={r['build_id'] for r in conn.execute('SELECT build_id FROM dd_agent.assembly_handoff_parts WHERE document_id=%s',(x.d,)).fetchall()}
    plan=ready(x,(10,));validate(x,plan);commit(x,plan)
    with pytest.raises(StageSuperseded):ParentWork(x.index,x.manager.stages).run(old['id'],old['owner_token'])
    x.bridge.reconcile(x.job['id'],expected_epoch=x.job['run_epoch'],limit=20)
    with x.db.connect() as conn:
        rows=conn.execute('''SELECT p.build_id,s.state FROM dd_agent.assembly_handoff_parts p JOIN dd_agent.stage_jobs s ON s.id=p.stage_job_id
            WHERE p.document_id=%s AND dd_agent.assembly_receipt_current(p.document_id,p.assembly_policy,p.receipt_generation)''',(x.d,)).fetchall()
    reused=old_ids&{r['build_id'] for r in rows}
    assert reused
    assert all(r['state'] in ('pending','succeeded') for r in rows)
    with pytest.raises(StageSuperseded):ParentWork(x.index,x.manager.stages).run(old['id'],old['owner_token'])
    drain(x)
    for identity in reused:assert x.index.metadata(x.p,x.d,identity)['current']


def test_validation_rejects_preexisting_corruption(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x)
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_rebuild_parts SET seal=repeat('0',64) WHERE plan_id=%s",(plan['id'],))
    result=validate(x,plan)
    assert result['status']=='blocked' and result['error_code']=='invalid_payload'


def test_existing_driver_clears_retired_lineage_halt_after_adoption(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x)
    blocked=x.driver.step(x.job['id'],expected_epoch=x.job['run_epoch'],vision_policy=VISION,reading_done=True,flush=True)
    assert blocked['failed'] and x.driver.halt
    validate(x,plan);commit(x,plan)
    assert x.driver.status(x.job['id'],expected_epoch=x.job['run_epoch'])['failed']==0


def test_adopted_ack_survives_explicit_resume_and_never_resurrects_reset_lineage(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x);validate(x,plan);ack=commit(x,plan)
    x.manager.pause(x.p,x.d);x.job=x.manager.resume(x.p,x.d,wake=False)
    x.rebuild.resume(x.job['id'],plan['id'],expected_epoch=x.job['run_epoch'])
    assert commit(x,plan)==ack
    cp=x.store.load(x.p,x.d);x.store.restart(x.p,x.d,expected_generation=cp['generation'])
    assert commit(x,plan)==ack and live(x)==[]


def test_clean_adoption_is_noop_for_receipts_and_models(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=20);plan=ready(x,());before=live(x)
    assert validate(x,plan)['validated']==0
    result=commit(x,plan)
    assert result['status']=='adopted' and result['generation']==x.original['generation']
    assert live(x)==before and x.store.load(x.p,x.d)==x.original
    assert x.index.models.calls==x.index.budget.calls==0
