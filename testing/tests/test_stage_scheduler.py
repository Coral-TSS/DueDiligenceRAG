"""Default production scheduler with real SQL/publication fences and cheap inference fixtures."""
from threading import Event, Thread
from time import monotonic, sleep
import importlib

import pytest
from psycopg.types.json import Jsonb
from uuid import uuid4

from dd_agent.config import Settings
from dd_agent.pipeline import JobManager, ProcessingPipeline
from dd_agent.retrieval import EvidenceService
from test_retrieval import TinyModels
from test_workspace import application, client, project, upload

instances=[]


@pytest.fixture(autouse=True)
def cleanup():
    yield
    for db,manager,release in instances:
        release.set();manager._stop.set()
        thread=getattr(manager,'test_thread',None)
        if thread:thread.join(20)
        db.provider.close()
    instances.clear()


def start(manager):
    thread=Thread(target=manager.drain);manager.test_thread=thread;thread.start();return thread


def wait_for(check, seconds=8):
    end=monotonic()+seconds
    while monotonic()<end:
        if check(): return
        sleep(.02)
    pytest.fail('Timed out waiting for scheduler condition')


def fixture(tmp_path, database_url, total=13, slow=False, failed=()):
    try: importlib.import_module('dd_agent.stage_scheduler')
    except ModuleNotFoundError: pytest.fail('RG-05d stage scheduler is missing')
    app=application(tmp_path,database_url,migrate=True)
    db=app.state.database
    with client(app) as c: p=project(c); d=upload(c,p).json()['document']['id']
    db.provider.start()  # Same bounded provider lifetime as the real app; no per-operation TCP setup.
    # 生产默认会建父子索引。这组调度测试只验证页级阶段，显式关闭以免改写原断言。
    settings=Settings(database_url=database_url,data_dir=tmp_path,stage_window_pages=10,
        stage_backlog_pages=20,stage_flush_seconds=.1,parent_index_mode='off')
    entered,release=Event(),Event()
    calls={'reading':[], 'vision':[]}
    class Reader:
        def prepare(self,project_id,document_id,force_ocr=False,activate=True):
            if activate:db.set_reading(document_id,total,'test-reader-v1')
            return {'page_count':total,'pages':[dict(page_number=n,route='text') for n in range(1,total+1)]},'test-reader-v1'
        def read(self,project_id,document_id,pages,checkpoint=None,prepared=None):
            existing={r['page_number'] for r in db.page_status(document_id,'test-reader-v1',pages) if r['status']=='succeeded'}
            for n in pages:
                if n not in existing:
                    calls['reading'].append(n)
                    db.save_page(document_id,'test-reader-v1',dict(page_number=n,status='failed' if n in failed else 'succeeded',
                        method='text',text=f'注册资本第{n}页',error_code='test_failed' if n in failed else ''))
                    if checkpoint: checkpoint('reading',n)
            return dict(skipped_pages=list(existing))
    class Automatic:
        def version(self): return 'test-automatic-v1'
        def process(self,project_id,document_id,pages,checkpoint=None):
            processed=[]
            for n in pages:
                if checkpoint: checkpoint('vision',n)
                if slow and n==1 and not release.is_set():
                    entered.set(); assert release.wait(15)
                calls['vision'].append(n)
                row=db.page_row(document_id,n)
                with db.publication('vision',n) as conn:
                    conn.execute('INSERT INTO dd_agent.machine_pages(id,document_id,page_number,base_revision,profile_version,result) VALUES (%s,%s,%s,%s,%s,%s)',
                        (uuid4(),document_id,n,row['raw_revision'],self.version(),Jsonb(dict(text=row['text'],outcome='base',route={},regions=[],verification='unverified'))))
                processed.append(n)
                if checkpoint: checkpoint('vision_saved',n)
            return dict(processed_pages=processed,skipped_pages=[],failed_pages=[])
    evidence=EvidenceService(settings,models=TinyModels(),database=db)
    pipeline=ProcessingPipeline(settings,reader=Reader(),automatic=Automatic(),evidence=evidence,database=db)
    manager=JobManager(settings,pipeline_factory=lambda:pipeline,database=db)
    job=manager.enqueue(p,d,wake=False)
    instances.append((db,manager,release))
    return db,manager,p,d,job,calls,entered,release,evidence


def test_tail_batch_and_real_index_publications(tmp_path,database_url):
    db,manager,p,d,job,calls,_,_,evidence=fixture(tmp_path,database_url)
    manager.drain()
    result=manager.latest(p,d)
    assert result['status']=='completed'
    assert result['progress']['scheduler']=='stages-v1'
    assert result['progress']['stages']['embedding']['succeeded']==13
    assert sorted(calls['reading'])==list(range(1,14))
    assert evidence.search(p,'注册资本')['coverage']['indexed_pages']==13
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM dd_agent.stage_jobs WHERE job_id=%s AND state IN ('pending','running')",(job['id'],)).fetchone()


def test_resource_deferral_never_consumes_recognition_failure_budget(tmp_path,database_url):
    from dd_agent.control import ProcessingDeferred
    db,manager,p,d,job,_,_,_,_=fixture(tmp_path,database_url)
    engine=manager.factory().automatic;real=engine.process;busy=[0]
    def process(*a,**k):
        if busy[0]<5:busy[0]+=1;raise ProcessingDeferred()
        return real(*a,**k)
    engine.process=process
    manager.drain()
    assert manager.latest(p,d)['status']=='completed' and busy[0]==5
    with db.connect() as conn:
        assert conn.execute('SELECT sum(failures) AS n FROM dd_agent.stage_jobs WHERE job_id=%s',(job['id'],)).fetchone()['n']==0
        assert conn.execute("SELECT count(*) AS n FROM dd_agent.stage_attempts a JOIN dd_agent.stage_jobs s ON s.id=a.stage_job_id WHERE s.job_id=%s AND a.outcome='deferred'",(job['id'],)).fetchone()['n']>=5


def test_paused_embedding_keeps_the_reading_backlog_bounded(tmp_path,database_url):
    db,manager,p,d,_,calls,_,release,_=fixture(tmp_path,database_url,total=45)
    manager.control_stage(p,d,'embedding',pause=True)
    thread=start(manager)
    try:
        wait_for(lambda:len(calls['reading'])>=20, seconds=15)
        assert len(calls['reading'])<=20
        assert calls['vision']==[]
        manager.control_stage(p,d,'embedding',pause=False)
    finally:
        release.set();thread.join(30)
    assert not thread.is_alive() and manager.latest(p,d)['status']=='completed'


def test_slow_vision_starts_only_after_the_volume_is_read(tmp_path,database_url):
    db,manager,p,d,_,calls,entered,release,_=fixture(tmp_path,database_url,total=45,slow=True)
    thread=start(manager)
    try:
        assert entered.wait(15)
        # 视觉要等整卷基础文字嵌入之后才加载，不能把阅读卡在第一页。
        assert len(calls['reading'])==45
    finally:
        release.set();thread.join(20)
    assert not thread.is_alive() and manager.latest(p,d)['status']=='completed'


def test_stage_pause_is_persistent_and_other_stage_can_advance(tmp_path,database_url):
    db,manager,p,d,_,calls,_,release,_=fixture(tmp_path,database_url,total=13)
    manager.control_stage(p,d,'embedding',pause=True)
    worker=start(manager)
    try:
        wait_for(lambda:len(calls['reading'])==13)
        # 基础索引暂停时不提前加载视觉；阅读仍可完成。
        assert calls['vision']==[]
        assert manager.latest(p,d)['status']=='running'
        assert 'embedding' in JobManager(manager.settings,database=db).latest(p,d)['paused_stages']
        manager.control_stage(p,d,'reading',pause=True)
        assert 'reading' in JobManager(manager.settings,database=db).latest(p,d)['paused_stages']
        manager.control_stage(p,d,'reading',pause=False)
        manager.control_stage(p,d,'embedding',pause=False)
    finally:
        release.set();worker.join(20)
    assert not worker.is_alive() and manager.latest(p,d)['status']=='completed'
    assert len(calls['vision'])==13


def test_global_pause_waits_for_inflight_work_and_resume_reuses_ocr(tmp_path,database_url):
    db,manager,p,d,job,calls,entered,release,_=fixture(tmp_path,database_url,total=13,slow=True)
    worker=start(manager)
    assert entered.wait(8)
    manager.pause(p,d)
    assert manager.latest(p,d)['status']=='pause_requested'
    release.set();worker.join(15)
    assert not worker.is_alive() and manager.latest(p,d)['status']=='paused'
    before=list(calls['reading'])
    manager.resume(p,d,wake=False); manager.drain()
    assert manager.latest(p,d)['status']=='completed'
    assert len(calls['reading'])==13 and len(set(calls['reading']))==13
    assert set(before).issubset(calls['reading'])


def test_gap_is_not_reported_as_complete_and_later_pages_are_indexed(tmp_path,database_url):
    db,manager,p,d,_,calls,_,_,evidence=fixture(tmp_path,database_url,total=13,failed=(2,))
    manager.drain()
    row=manager.latest(p,d)
    assert row['status']=='partial'
    assert 2 in row['progress']['failed_pages']
    assert row['progress']['stages']['embedding']['succeeded']==12
    assert row['progress']['stages']['embedding']['ready_ranges']==[[1,1],[3,13]]
    assert evidence.search(p,'注册资本')['coverage']['indexed_pages']==12


def test_stage_control_api_is_project_bound_and_persistent(tmp_path,database_url,monkeypatch):
    db,manager,p,d,_,_,_,_,_=fixture(tmp_path,database_url,total=1)
    from dd_agent.app import create_app
    app=create_app(manager.settings);app.state.jobs=manager
    monkeypatch.setattr(manager,'wake',lambda:None)
    with client(app) as c:
        path=f'/api/projects/{p}/documents/{d}/processing/stages/reading'
        result=c.post(path+'/pause')
        assert result.status_code==200
        assert result.json()['paused_stages']==['reading']
        assert c.post(path+'/resume').json()['paused_stages']==[]
        other=project(c,'另一个项目')
        assert c.post(f'/api/projects/{other}/documents/{d}/processing/stages/reading/pause').status_code==404
        assert c.post(path.replace('reading','unknown')+'/pause').status_code==422


def test_cooperative_parking_does_not_consume_failure_budget(tmp_path,database_url):
    db,manager,p,d,job,_,_,_,_=fixture(tmp_path,database_url,total=1)
    store=manager.stages
    task=store.ensure(job['id'],'reading',{'pages':[1]},source_revision='synthetic',policy_version='reading-test')
    for _ in range(4):
        claim=store.claim(job['id'],'reading')
        assert store.park(claim['id'],claim['owner_token'])
    claim=store.claim(job['id'],'reading')
    assert claim['failures']==0 and claim['attempts']==5
    assert store.fail(claim['id'],claim['owner_token'],category='transient',error_code='test_failure',retry_after=0)
    assert store.claim(job['id'],'reading') is not None


def test_actual_page_publication_is_fenced_after_resume(tmp_path,database_url):
    from dd_agent.stage_publication import bind_publications,StageSuperseded
    db,manager,p,d,job,_,_,_,_=fixture(tmp_path,database_url,total=1)
    store=manager.stages
    store.ensure(job['id'],'reading',{'pages':[1]},source_revision='synthetic',policy_version='reading-test')
    task=store.claim(job['id'],'reading')
    manager.pause(p,d);manager.resume(p,d,wake=False)
    with bind_publications(store,'reading',[task]),pytest.raises(StageSuperseded):
        db.save_page(d,'test-reader-v1',dict(page_number=1,status='succeeded',method='text',text='must not publish'))
    assert db.pages(d)==[]


def test_progress_distinguishes_stage_counts():
    import subprocess
    from dd_agent.config import ROOT
    js="""const {processingView}=require(process.argv[1]); console.log(processingView({status:'running',
        progress:{scheduler:'stages-v1',total_pages:20,chunk_rebuild:{done:4,total:20},stages:{reading:{succeeded:13},vision:{succeeded:10},embedding:{succeeded:3,paused:true}}}}).text);"""
    from node_bin import node_executable
    result=subprocess.run([str(node_executable()),'-e',js,str(ROOT/'src/dd_agent/static/progress.js')],
        capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
    assert '基础识别 13' in result.stdout and '向量入库 3' in result.stdout
    assert '视觉增强 10/20 页' in result.stdout and '切片重建 4/20 页' in result.stdout
    assert '机器文字' not in result.stdout
    assert '已保存整批进度' not in result.stdout


def test_old_preflight_cannot_change_reading_pointer(tmp_path,database_url):
    from dd_agent.stage_publication import StageSuperseded
    db,manager,p,d,job,_,_,_,_=fixture(tmp_path,database_url,total=1)
    db.set_reading(d,1,'current-version')
    manager.pause(p,d);manager.resume(p,d,wake=False)
    with pytest.raises(StageSuperseded):
        with manager.stages.parent_scope(job['id'],job['run_epoch']) as conn:
            db.set_reading(d,1,'obsolete-version',conn=conn)
    assert db.reading_version(d)['pipeline_version']=='current-version'
