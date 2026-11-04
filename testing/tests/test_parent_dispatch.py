"""Real work packets consumed by the scheduler without inventing page coverage."""
from types import SimpleNamespace

import pytest

from dd_agent.control import ProcessingDeferred
from dd_agent.parent_work import ParentWork
from dd_agent.stage_scheduler import StageScheduler
from test_parent_index import sample


def setup(tmp_path,database_url):
    db,p,d,index,plan,manager,task,models,budget=sample(tmp_path,database_url)
    calls=[]
    class Evidence:
        def index(self,project,document,pages,**kwargs):
            calls.extend(pages)
            return dict(failed_pages=[])
    pipeline=SimpleNamespace(evidence=Evidence(),parent_work=ParentWork(index,manager.stages))
    with db.connect() as conn:
        job=conn.execute('SELECT * FROM dd_agent.processing_jobs WHERE id=%s',(task['job_id'],)).fetchone()
    scheduler=StageScheduler(manager,job,pipeline)
    return db,p,d,index,plan,manager,task,models,scheduler,calls


def test_parent_packet_publishes_without_legacy_page_index_or_false_page_progress(tmp_path,database_url):
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    scheduler._execute('embedding',[task])
    assert index.metadata(p,d,plan['id'])['current']
    assert calls==[] and models.calls==1
    snapshot=scheduler.snapshot()
    assert snapshot['processed_pages']==0 and snapshot['failed_pages']==[]
    assert snapshot['stages']['embedding']['succeeded']==0
    assert snapshot['parent_work']['succeeded']==1 and snapshot['parent_work']['pending']==0


def test_mixed_batch_keeps_page_and_packet_publications_separate(tmp_path,database_url):
    db,p,d,index,plan,manager,parent,models,scheduler,calls=setup(tmp_path,database_url)
    row=db.page_row(d,2)
    manager.stages.ensure(parent['job_id'],'embedding',{'pages':[2]},source_revision='page-only',
        policy_version='legacy',dependencies=[dict(page_number=2,view='effective',revision=row['source_revision'])])
    page=manager.stages.claim(parent['job_id'],'embedding')
    scheduler._execute('embedding',[parent,page])
    assert calls==[2] and models.calls==1 and index.metadata(p,d,plan['id'])['current']
    snapshot=scheduler.snapshot()
    assert snapshot['processed_pages']==1 and snapshot['parent_work']['succeeded']==1


def test_resource_deferral_parks_packet_without_failure_or_page_outcome(tmp_path,database_url,monkeypatch):
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    def busy(*a,**k):raise ProcessingDeferred()
    monkeypatch.setattr(index,'embed',busy)
    scheduler._execute('embedding',[task])
    with db.connect() as conn:
        row=conn.execute('SELECT state,failures FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()
    assert row==dict(state='pending',failures=0)
    assert scheduler.snapshot()['parent_work']['pending']==1
    assert scheduler.snapshot()['failed_pages']==[] and calls==[]


def test_stale_packet_is_separate_failure_not_a_failed_source_page(tmp_path,database_url):
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='股东更新'))
    scheduler._execute('embedding',[task])
    snapshot=scheduler.snapshot()
    assert snapshot['parent_work']['cancelled']==1
    assert snapshot['failed_work_count']==1 and snapshot['failed_pages']==[]
    assert calls==[] and models.calls==0


def test_parent_progress_restores_from_database_and_fences_completion(tmp_path,database_url):
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    manager.stages.park(task['id'],task['owner_token'])
    scheduler._refresh_parent_work()
    assert scheduler.snapshot()['parent_work']['pending']==1
    assert not scheduler._parent_work_done()
    claim=scheduler._claims('embedding',10)
    assert len(claim)==1 and scheduler.snapshot()['processed_pages']==0
    assert scheduler.states['embedding']=={}
    scheduler._execute('embedding',claim)
    restored=StageScheduler(manager,scheduler.job,scheduler.pipeline)
    restored._refresh_parent_work()
    assert restored._parent_work_done() and restored.snapshot()['parent_work']['succeeded']==1


def test_stage_pause_preserves_packet_then_continues_without_legacy_fallback(tmp_path,database_url):
    from dd_agent.stage_publication import StagePaused
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    manager.control_stage(p,d,'embedding',pause=True)
    scheduler._execute('embedding',[task])
    assert models.calls==0 and scheduler.snapshot()['parent_work']['pending']==1
    with pytest.raises(StagePaused):scheduler._claims('embedding',1)
    manager.control_stage(p,d,'embedding',pause=False)
    scheduler._execute('embedding',scheduler._claims('embedding',1))
    assert index.metadata(p,d,plan['id'])['current'] and calls==[]


def test_running_packet_progress_is_saved_before_model_inference(tmp_path,database_url,monkeypatch):
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    actual=index.embed
    observed=[]
    def embed(plan):
        with db.connect() as conn:
            saved=conn.execute('SELECT progress FROM dd_agent.processing_jobs WHERE id=%s',(task['job_id'],)).fetchone()
        observed.append(saved['progress'].get('parent_work',{}).get('running',0))
        return actual(plan)
    monkeypatch.setattr(index,'embed',embed)
    scheduler._execute('embedding',[task])
    assert observed==[1]


def live_pipeline(db,index,manager,job_id):
    from dd_agent.pipeline import ProcessingPipeline
    from dd_agent.retrieval import EvidenceService
    from test_retrieval import TinyModels
    class Reader:
        def prepare(self,*a,**k):
            return dict(page_count=2,pages=[dict(page_number=n,route='text') for n in (1,2)]),'scan-v1'
        def read(self,*a,**k):return dict(skipped_pages=[1,2])
    class Automatic:
        def version(self):return 'no-enhancement'
        def process(self,*a,**k):return dict(failed_pages=[])
    pipeline=ProcessingPipeline(manager.settings,reader=Reader(),automatic=Automatic(),
        evidence=EvidenceService(manager.settings,database=db,models=TinyModels()),database=db)
    pipeline.parent_work=ParentWork(index,manager.stages)
    manager.factory=lambda:pipeline
    # 合成上游只替换识别推理，不产生机器增强记录；预置两页的持久向量待办。
    # 队列、索引发布和父任务结束仍执行真实生产代码。
    with db.connect() as conn:
        job=conn.execute('SELECT id,document_id FROM dd_agent.processing_jobs WHERE id=%s',(job_id,)).fetchone()
    for n in (1,2):
        row=db.page_row(job['document_id'],n)
        manager.stages.ensure(job['id'],'embedding',dict(pages=[n]),source_revision=row['source_revision'],
            policy_version=pipeline.evidence.version(),
            dependencies=[dict(page_number=n,view='effective',revision=row['source_revision'])])


@pytest.mark.parametrize('broken',[False,True])
def test_live_scheduler_waits_for_packets_and_reports_packet_failure_separately(tmp_path,database_url,monkeypatch,broken):
    from threading import Event,Thread
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    manager.stages.park(task['id'],task['owner_token'])
    live_pipeline(db,index,manager,task['job_id'])
    entered,release=Event(),Event()
    actual=index.embed
    def embed(value):
        entered.set()
        assert release.wait(10)
        if broken:raise ValueError('synthetic model failure')
        return actual(value)
    monkeypatch.setattr(index,'embed',embed)
    worker=Thread(target=manager.drain)
    worker.start()
    try:
        assert entered.wait(10)
        # 页级索引完成也不得跳过尚在运行的持久工作包。
        assert manager.latest(p,d)['status']=='running'
    finally:
        release.set();worker.join(20)
        manager._stop.set()
        if worker.is_alive():worker.join(5)
    assert not worker.is_alive()
    row=manager.latest(p,d)
    assert row['status']==('partial' if broken else 'completed')
    assert row['progress']['failed_pages']==[] and row['progress']['processed_pages']==2
    assert row['progress']['parent_work']['failed' if broken else 'succeeded']==1
    assert row['progress']['failed_work_count']==int(broken)
    with db.connect() as conn:
        packet=conn.execute('SELECT state,failures FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()
    assert packet==dict(state='failed' if broken else 'succeeded',failures=3 if broken else 0)


def test_default_consumer_constructs_parent_service_without_legacy_fallback(tmp_path,database_url,monkeypatch):
    from dd_agent import parent_index
    from test_parent_index import Budget
    db,p,d,index,plan,manager,task,models,scheduler,calls=setup(tmp_path,database_url)
    del scheduler.pipeline.parent_work
    scheduler.pipeline.evidence.models=models
    monkeypatch.setattr(parent_index,'LocalTokenBudget',lambda settings:Budget())
    scheduler._execute('embedding',[task])
    assert index.metadata(p,d,plan['id'])['current'] and models.calls==1 and calls==[]


def test_global_pause_returns_every_packet_claim_in_batch(tmp_path,database_url):
    from dd_agent.assembly import assemble
    from dd_agent.assembly_store import AssemblyStore
    from dd_agent.control import ProcessingPaused
    db,p,d,index,plan,manager,first,models,scheduler,calls=setup(tmp_path,database_url)
    snapshots=AssemblyStore(db).snapshots(p,d,[2],ready_pages={2})
    extra=index.prepare(p,d,assemble(snapshots,final=True)['parents'],snapshots)
    index.register(manager.stages,first['job_id'],extra)
    second=manager.stages.claim(first['job_id'],'embedding')
    manager.pause(p,d)
    with pytest.raises(ProcessingPaused):scheduler._execute('embedding',[first,second])
    with db.connect() as conn:
        rows=conn.execute('SELECT state,failures FROM dd_agent.stage_jobs WHERE id=ANY(%s)',([first['id'],second['id']],)).fetchall()
    assert len(rows)==2 and all(row==dict(state='pending',failures=0) for row in rows)
    assert scheduler.snapshot()['parent_work']['pending']==2 and models.calls==0 and calls==[]
