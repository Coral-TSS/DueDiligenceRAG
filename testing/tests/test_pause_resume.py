"""Pause must persist, preserve completed work and never leak project scope."""
from threading import Thread, Event

import pytest

from dd_agent.pipeline import JobManager, ProcessingPipeline
from dd_agent.automatic import AutomaticError, AutomaticProcessor
from test_retrieval import setup
from test_automatic import page, Vision
from test_workspace import client, project


def test_queued_pause_survives_new_manager_and_resume_reuses_job(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    manager=JobManager(evidence.settings)
    job=manager.enqueue(p,d,wake=False)
    assert manager.pause(p,d)['status']=='paused'
    restarted=JobManager(evidence.settings)
    restarted.drain()
    assert restarted.latest(p,d)['status']=='paused'
    assert restarted.enqueue(p,d,wake=False)['id']==job['id']
    assert restarted.resume(p,d,wake=False)['id']==job['id']
    assert restarted.resume(p,d,wake=False)['status']=='queued'


def test_running_pause_not_overwritten_by_progress_and_resume_finishes(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    entered=Event();release=Event()
    class Work:
        def run(self,project_id,document_id,pages,checkpoint=None):
            entered.set();assert release.wait(5)
            return {'reading':{},'automatic':{'failed_pages':[]},'index':{'failed_pages':[]}}
    manager=JobManager(evidence.settings,pipeline_factory=Work)
    job=manager.enqueue(p,d,wake=False)
    worker=Thread(target=manager.drain)
    worker.start()
    try:
        assert entered.wait(5)
        assert manager.pause(p,d)['status']=='pause_requested'
        release.set();worker.join(10)
        assert not worker.is_alive()
        result=manager.latest(p,d)
        assert result['id']==job['id'] and result['status']=='paused'
        manager.resume(p,d,wake=False);manager.drain()
        assert manager.latest(p,d)['status']=='completed'
    finally:
        release.set();worker.join(10)


def test_pause_resume_cannot_control_other_project(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    with client(app) as c:other=project(c,'另一个项目')
    # Populate projects before enqueueing; entering TestClient starts the worker.
    manager=JobManager(evidence.settings);manager.enqueue(p,d,wake=False)
    for operation in (manager.pause,manager.resume):
        with pytest.raises(AutomaticError):operation(other,d)
    assert manager.latest(p,d)['status']=='queued'


def test_embedding_pause_preserves_published_page_and_resumes_remaining(tmp_path,database_url):
    from dd_agent.control import ProcessingPaused
    app,p,d,evidence,_=setup(tmp_path,database_url)
    def pause(stage,number):
        if number==1:raise ProcessingPaused()
    with pytest.raises(ProcessingPaused):evidence.index(p,d,[1,2],checkpoint=pause)
    result=evidence.index(p,d,[1,2])
    assert result['skipped_pages']==[1] and result['indexed_pages']==[2]


def test_visual_pause_reuses_saved_candidate_without_changing_raw(tmp_path,database_url):
    from dd_agent.control import ProcessingPaused
    app,p,d,evidence,_=setup(tmp_path,database_url)
    original='注册资本2000万元'
    page(app.state.database,d,original)
    vision=Vision();vision.answer=original
    processor=AutomaticProcessor(evidence.settings,vision=vision)
    def pause(stage,number):
        if stage=='vision_saved':raise ProcessingPaused()
    with pytest.raises(ProcessingPaused):processor.process(p,d,[1],checkpoint=pause)
    assert app.state.database.pages(d)[0]['text']==original
    processor.process(p,d,[1])
    assert processor.state(p,d,1)['result']['verification']=='machine_checked'
    with app.state.database.connect() as c:
        assert c.execute('SELECT count(*) AS n FROM dd_agent.vision_candidates WHERE document_id=%s',(d,)).fetchone()['n']==1


def test_pipeline_exposes_stages_without_requiring_a_control_callback(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    class ExistingReader:
        def read(self,project_id,document_id,pages,checkpoint=None):return app.state.database.reading(document_id)
    page(app.state.database,d,'注册资本2000万元',method='text')
    engine=AutomaticProcessor(evidence.settings,vision=Vision())
    pipeline=ProcessingPipeline(evidence.settings,reader=ExistingReader(),automatic=engine,evidence=evidence)
    stages=[]
    pipeline.run(p,d,[1],checkpoint=lambda stage,number:stages.append(stage))
    assert 'reading' in stages and 'embedding' in stages


@pytest.mark.parametrize('managed',[False,True])
def test_ocr_pause_keeps_completed_page_and_closes_inference_stream(tmp_path,database_url,monkeypatch,managed):
    from dd_agent import reading
    from dd_agent.config import Settings
    from dd_agent.control import ProcessingPaused
    from test_reading import make_pdf
    from test_workspace import application,upload
    body=make_pdf(tmp_path/'source.pdf',['First company registration record.','Second company registration record.'])
    app=application(tmp_path/'data',database_url,migrate=True,limit=10000)
    with client(app) as c:
        p=project(c);d=upload(c,p,body=body).json()['document']['id']
    real=reading.worker_records
    closed=[]
    def records(python,worker,manifest,directory,**kwargs):
        if worker!='ocr_worker':
            yield from real(python,worker,manifest,directory,**kwargs)
            return
        try:
            for item in manifest['pages']:
                yield dict(page_number=item['page_number'],image_path=item['image_path'],payload={
                    'rec_texts':['注册资本2000万元'],'rec_scores':[.98],
                    'rec_polys':[[[10,10],[500,10],[500,50],[10,50]]]})
        finally:closed.append(True)
    monkeypatch.setattr(reading,'worker_records',records)
    from dd_agent import model_runtime
    monkeypatch.setattr(model_runtime,'runtime_records',lambda settings,kind,operation,payload,**kwargs:
        records(settings.ocr_python,'ocr_worker',payload,tmp_path))
    models=tmp_path/'ocr-fixture'
    for name in reading.OCR_MODEL_NAMES:
        folder=models/name;folder.mkdir(parents=True)
        (folder/'inference.pdiparams').write_bytes(b'fixture-only')
    settings=Settings(database_url=database_url,data_dir=tmp_path/'data',model_runtime_enabled=managed,ocr_models_dir=models)
    def pause(stage,number):
        if stage=='ocr' and app.state.database.pages(d):raise ProcessingPaused()
    with pytest.raises(ProcessingPaused):reading.Reader(settings).read(p,d,[1,2],force_ocr=True,checkpoint=pause)
    assert len(app.state.database.pages(d))==1 and closed
    result=reading.Reader(settings).read(p,d,[1,2],force_ocr=True)
    assert result['skipped_pages']==[1] and result['successful_pages']==2
