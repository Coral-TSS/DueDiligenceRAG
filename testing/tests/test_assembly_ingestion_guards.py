"""Failure bounds and conservative final coverage of automatic ingestion."""
import pytest
from psycopg.types.json import Jsonb
from uuid import uuid4

from dd_agent.control import ProcessingStopped
from test_assembly_ingestion import sample,machine,step,drain,driver,VISION


def test_wrong_vision_policy_is_never_treated_as_ready(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    row=x.db.page_row(x.d,1)
    with x.db.connect() as conn:
        conn.execute('''INSERT INTO dd_agent.machine_pages(id,document_id,page_number,base_revision,profile_version,result)
            VALUES(%s,%s,1,%s,'wrong-policy',%s)''',
            (uuid4(),x.d,row['raw_revision'],Jsonb(dict(text='错误版本',verification='unverified'))))
    result=step(x)
    assert not result['terminal'] and result['waiting']=='vision' and result['saved_pages']==0


def test_successful_stage_without_current_machine_artifact_is_explicit_failure(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2,machines=False)
    row=x.db.page_row(x.d,1)
    task=x.manager.stages.ensure(x.job['id'],'vision',dict(pages=[1]),source_revision=row['raw_revision'],policy_version=VISION,
        dependencies=[dict(page_number=1,view='raw',revision=row['raw_revision'])])
    claim=x.manager.stages.claim(x.job['id'],'vision',task_id=task['id'])
    x.manager.stages.complete(claim['id'],claim['owner_token'])
    result=step(x)
    assert result['terminal'] and result['failed']>0 and result['waiting']=='vision_artifact_missing'
    assert x.store.load(x.p,x.d)['generation']==0


def test_page_count_growth_cannot_reuse_old_document_end(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    step(x);drain(x);assert step(x)['terminal']
    x.db.set_reading(x.d,3,'scan-v1')
    result=step(x)
    assert result['terminal'] and result['failed']>0 and result['waiting']=='stale_sources'


def test_stop_after_preparing_handoff_keeps_saved_receipt_unregistered(tmp_path,database_url,monkeypatch):
    x=sample(tmp_path,database_url,total=2)
    actual=x.driver.bridge._prepare;prepared=[]
    def prepare(*a,**k):
        result=actual(*a,**k);prepared.append(True);return result
    def checkpoint(*a):
        if prepared:raise ProcessingStopped()
    monkeypatch.setattr(x.driver.bridge,'_prepare',prepare)
    with pytest.raises(ProcessingStopped):step(x,checkpoint=checkpoint)
    assert x.store.load(x.p,x.d)['state']['closed']
    with x.db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_work').fetchone()['n']==0
    x.driver=driver(x)
    step(x);drain(x)
    assert step(x)['terminal']


@pytest.mark.parametrize('permanent',[False,True])
def test_publish_failures_retry_durably_and_stop_at_budget(tmp_path,database_url,monkeypatch,permanent):
    x=sample(tmp_path,database_url,total=2)
    actual=x.driver.store._save;calls=[]
    def save(*a,**k):
        calls.append(True)
        if permanent or len(calls)==1:raise RuntimeError('synthetic SQL publish failure')
        return actual(*a,**k)
    monkeypatch.setattr(x.driver.store,'_save',save)
    first=step(x)
    assert not first['terminal'] and first['waiting']=='assembly_retry' and first['saved_pages']==0
    for _ in range(2 if permanent else 1):
        with x.db.connect() as conn:
            conn.execute("UPDATE dd_agent.stage_jobs SET available_at=clock_timestamp() WHERE job_id=%s AND stage='assembly'",(x.job['id'],))
        result=step(x)
    if permanent:
        assert result['terminal'] and result['failed']>0 and result['waiting']=='assembly_failed'
        assert len(calls)==3 and x.store.load(x.p,x.d)['generation']==0
    else:
        drain(x)
        assert step(x)['terminal'] and len(calls)==2


def test_oversized_single_page_is_not_truncated_or_sent_to_tokenizer(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='甲'*200001))
    machine(x,[1])
    result=step(x)
    assert result['terminal'] and result['failed']>0 and result['waiting']=='assembly_input_too_large'
    assert x.index.budget.calls==0 and x.store.load(x.p,x.d)['generation']==0


def test_historical_missing_input_metadata_never_implies_full_coverage(tmp_path,database_url):
    x=sample(tmp_path,database_url,total=2)
    x.store.advance(x.p,x.d,x.store.snapshots(x.p,x.d,[1,2],ready_pages={1,2}),expected_generation=0,final=True)
    with x.db.connect() as conn:
        conn.execute("UPDATE dd_agent.assembly_batches SET result=result-'input_dependencies' WHERE document_id=%s",(x.d,))
    step(x);drain(x)
    result=step(x)
    assert result['terminal'] and result['failed']>0 and result['waiting']=='legacy_receipt_unverified'
