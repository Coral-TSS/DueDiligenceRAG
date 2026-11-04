"""Automatic extraction must progress without manual review or silent number changes."""
import importlib
from uuid import UUID

import pytest

from dd_agent.db import Database
from test_retrieval import setup
from test_workspace import client, project


def automatic():
    try:
        return importlib.import_module('dd_agent.automatic')
    except ModuleNotFoundError:
        pytest.fail('Automatic routing and machine result selection are not implemented')


def page(db, d, text, method='ocr'):
    lines=[]
    for i, line in enumerate(text.split('\n')):
        lines.append({'text':line,'score':.99,'box':[[10,10+i*30],[800,10+i*30],[800,35+i*30],[10,35+i*30]]})
    db.save_page(d, 'scan-v1', dict(page_number=1, status='succeeded', method=method, text=text, lines=lines))


class Vision:
    version='fixture-v1'
    calls=0
    answer='自2013年11月05日至2013年11月25日'
    fail=False
    def profile(self):
        return self.version
    def recognize(self, **kwargs):
        self.calls+=1
        if self.fail:
            raise automatic().AutomaticError('Fixture visual failure')
        return {'text':self.answer,'truncated':False,'model_version':self.version}


def test_route_fuzzy_important_text_but_not_plain_dates_or_native_text():
    route=automatic().route_page
    assert route({'method':'ocr','text':'企业名称预先核准申请书\n注册资木2000万元','lines':[]})['important']
    assert route({'method':'ocr','text':'注册资本2000万元','lines':[]})['needs_vision']
    assert not route({'method':'text','text':'注册资本2000万元','lines':[]})['needs_vision']
    assert not route({'method':'ocr','text':'2026年10月，收到2000元。','lines':[]})['important']
    unknown=route({'method':'ocr','text':'\ufffd\ufffd','lines':[]}, filename='工商登记申请.pdf')
    assert unknown['needs_vision'] and unknown['classification']=='uncertain'


def test_automatic_date_repair_and_index_resume_without_editing_original(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    db=app.state.database
    raw='名称预先核准\n自2013年11月05日至203年11月25日\n无关说明保留'
    page(db,d,raw)
    vision=Vision(); vision.answer='名称预先核准\n自2013年11月05日至2013年11月25日\n无关说明保留'
    engine=automatic().AutomaticProcessor(evidence.settings,vision=vision)
    first=engine.process(p,d,[1])
    assert first['processed_pages']==[1]
    assert db.pages(d)[0]['text']==raw
    effective=db.evidence_pages(d)[0]
    assert '至2013年' in effective['text']
    assert effective['verification']=='machine_enhanced'
    assert evidence.index(p,d,[1])['indexed_pages']==[1]
    assert engine.process(p,d,[1])['skipped_pages']==[1]
    assert vision.calls==1
    assert evidence.search(p,'名称')['results'][0]['verification']=='machine_enhanced'
    page(db,d,raw+'\n新的基础识别')
    assert evidence.search(p,'名称')['results']==[]


def test_returning_to_cached_policy_republishes_without_repeating_uncertain_inference(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    page(app.state.database,d,'注册资本2000万元')
    vision=Vision();vision.fail=True
    engine=automatic().AutomaticProcessor(evidence.settings,vision=vision)
    first_version=vision.version
    engine.process(p,d,[1]);engine.process(p,d,[1])
    vision.version='other-policy';engine.process(p,d,[1])
    calls=vision.calls
    vision.version=first_version;engine.process(p,d,[1])
    assert vision.calls==calls
    assert engine.state(p,d,1)['profile_version']==engine.version()


def test_valid_amount_conflict_is_unknown_and_not_silently_replaced(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    page(app.state.database,d,'注册资本2000万元\n无关说明保留')
    vision=Vision();vision.answer='注册资本5000万元\n无关说明保留'
    engine=automatic().AutomaticProcessor(evidence.settings,vision=vision)
    engine.process(p,d,[1])
    current=app.state.database.evidence_pages(d)[0]
    assert current['verification']=='machine_uncertain'
    assert '2000' not in current['text'] and '5000' not in current['text']
    assert app.state.database.pages(d)[0]['text'].startswith('注册资本2000')
    assert engine.state(p,d,1)['result']['regions'][0]['decision']=='unknown'


def test_visual_failure_is_bounded_and_recovery_preserves_history(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    page(app.state.database,d,'注册资本2000万元')
    vision=Vision();vision.fail=True
    engine=automatic().AutomaticProcessor(evidence.settings,vision=vision)
    engine.process(p,d,[1]);engine.process(p,d,[1]);engine.process(p,d,[1])
    assert vision.calls==2
    assert engine.state(p,d,1)['result']['outcome']=='uncertain'
    vision.fail=False;vision.version='fixture-v2';vision.answer='注册资本2000万元'
    assert engine.process(p,d,[1])['processed_pages']==[1]
    assert app.state.database.evidence_pages(d)[0]['verification']=='machine_checked'
    assert len(engine.state(p,d,1)['history'])==3


def test_native_text_bypasses_vision_and_other_project_cannot_process(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    page(app.state.database,d,'注册资本2000万元',method='text')
    vision=Vision();engine=automatic().AutomaticProcessor(evidence.settings,vision=vision)
    engine.process(p,d,[1])
    assert vision.calls==0
    with client(app) as c:other=project(c,'隔离项目')
    with pytest.raises(automatic().AutomaticError):engine.process(other,d,[1])


def test_stale_machine_output_not_published(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    page(app.state.database,d,'注册资本2000万元')
    class Changed(Vision):
        def recognize(self,**kwargs):
            page(app.state.database,d,'注册资本3000万元')
            return super().recognize(**kwargs)
    engine=automatic().AutomaticProcessor(evidence.settings,vision=Changed())
    assert engine.process(p,d,[1])['failed_pages']==[1]
    assert app.state.database.evidence_pages(d)[0]['verification']=='unverified'


def test_manual_write_routes_removed_but_history_stays_readable(tmp_path,database_url):
    app,p,d,_,_=setup(tmp_path,database_url)
    with client(app) as c:
        route=f'/api/projects/{p}/documents/{d}/pages/1/review'
        assert c.get(route).status_code==200
        assert c.post(route,json={}).status_code in (404,405)
        assert c.post(route+'/vision',json={}).status_code in (404,405)


def test_processing_pipeline_runs_recognition_and_index_without_adoption(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    page(app.state.database,d,'注册资本2000万元')
    vision=Vision();vision.answer='注册资本2000万元'
    engine=automatic().AutomaticProcessor(evidence.settings,vision=vision)
    pipeline_module=importlib.import_module('dd_agent.pipeline') if importlib.util.find_spec('dd_agent.pipeline') else None
    assert pipeline_module is not None, 'Automatic processing pipeline is missing'
    class ExistingReader:
        def read(self,project_id,document_id,pages):return app.state.database.reading(document_id)
    pipeline=pipeline_module.ProcessingPipeline(evidence.settings,reader=ExistingReader(),automatic=engine,evidence=evidence)
    result=pipeline.run(p,d,[1])
    assert result['automatic']['processed_pages']==[1]
    assert result['index']['indexed_pages']==[1]
    assert evidence.search(p,'资本')['results'][0]['verification']=='machine_checked'


def test_persistent_job_scope_dedup_and_batch_progress(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    assert importlib.util.find_spec('dd_agent.pipeline'), 'Persistent processing jobs are missing'
    from dd_agent.pipeline import JobManager
    class Pipeline:
        def run(self,project_id,document_id,pages,checkpoint=None):
            return {'reading':{'page_count':3},'automatic':{'failed_pages':[]},'index':{'failed_pages':[]}}
    manager=JobManager(evidence.settings,pipeline_factory=Pipeline)
    # Use already inspected page_count; no raw fixture PDF needs to be parsed.
    first=manager.enqueue(p,d,wake=False)
    assert manager.enqueue(p,d,wake=False)['id']==first['id']
    manager.drain()
    assert manager.latest(p,d)['status']=='completed'
    assert manager.latest(p,d)['progress']['processed_pages']==3
    with client(app) as c:other=project(c,'隔离任务')
    with pytest.raises(automatic().AutomaticError):manager.enqueue(other,d,wake=False)


@pytest.mark.parametrize('before,after',[
    ('企业名称：甲公司','企业名称：乙公司'),
    ('法定代表人：张三','法定代表人：张二'),
    ('注册地址：甲路','注册地址：乙路'),
])
def test_conflicting_identity_fields_are_unknown(before,after):
    choice=automatic().choose_region(before,{'text':after,'truncated':False})
    assert choice['decision']=='unknown'
    assert choice['text']==automatic().UNKNOWN


def test_region_geometry_and_budget_preserve_explicit_unknown(tmp_path):
    import struct
    image=tmp_path/'page.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',1000,2000))
    lines=[dict(text=('注册资本2000万元' if i in (1,12) else f'普通文字{i}'),box=[[10,10+i*100],[900,10+i*100],[900,50+i*100],[10,50+i*100]]) for i in range(16)]
    raw=dict(method='ocr',text='\n'.join(x['text'] for x in lines),lines=lines)
    regions=automatic().regions_for(raw,automatic().route_page(raw),image,limit=1)
    assert len(regions)==2 and regions[1]['over_budget']
    assert all(0<=v<=1 for r in regions for v in r['bounds'])
    assert regions[0]['end']<regions[1]['start']


def test_interrupted_job_waits_for_resume_and_partial_batches_finish(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    from dd_agent.pipeline import JobManager
    class Pipeline:
        def run(self,project_id,document_id,pages,checkpoint=None):
            return {'reading':{'failed_pages':1},'automatic':{'failed_pages':[2]},'index':{'failed_pages':[2]}}
    manager=JobManager(evidence.settings,pipeline_factory=Pipeline)
    job=manager.enqueue(p,d,wake=False)
    with app.state.database.connect() as conn:
        conn.execute("UPDATE dd_agent.processing_jobs SET status='running' WHERE id=%s",(job['id'],))
    manager.drain()
    # RG-01: startup preserves an interrupted task, but does not run it silently.
    assert manager.latest(p,d)['status']=='paused'
    manager.resume(p,d,wake=False)
    manager.drain()
    result=manager.latest(p,d)
    assert result['status']=='partial' and result['progress']['processed_pages']==3
    assert result['progress']['failed_pages']==[2]
