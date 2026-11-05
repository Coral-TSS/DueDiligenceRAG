from contextlib import contextmanager
from uuid import UUID
import pytest

from test_retrieval import setup, TinyModels
from test_workspace import client, project, upload, PDF


class CountingModels(TinyModels):
    def __init__(self):self.embeds=0;self.ranks=0;self.rank_version='rank-v1'
    def cache_profile(self,kind):return self.version if kind=='embedding' else self.rank_version
    def embed(self,*args,**kwargs):self.embeds+=1;return super().embed(*args,**kwargs)
    def rerank(self,*args,**kwargs):self.ranks+=1;return super().rerank(*args,**kwargs)


def indexed(tmp_path,database_url):
    app,p,d,service,_=setup(tmp_path,database_url)
    models=CountingModels();service.models=models
    service.index(p,d,[1,2]);models.embeds=models.ranks=0
    return app,p,d,service,models


def test_repeat_query_reuses_only_pure_model_outputs_and_profile_changes_miss(tmp_path,database_url):
    _,p,d,service,models=indexed(tmp_path,database_url)
    a=service.search(p,'注册资本');b=service.search(p,'注册资本')
    assert a==b and models.embeds==models.ranks==1
    models.rank_version='rank-v2'
    service.search(p,'注册资本')
    assert models.embeds==1 and models.ranks==2
    models.version='embedding-v2'
    assert service.search(p,'注册资本')['results']==[]
    service.index(p,d,[1,2]);models.embeds=0
    service.search(p,'注册资本')
    assert models.embeds==1


def test_new_material_and_new_filename_are_visible_with_warm_model_cache(tmp_path,database_url):
    app,p,d,service,models=indexed(tmp_path,database_url)
    service.search(p,'注册资本')
    with client(app) as c:
        upload(c,p,'新来源.pdf')
        other=upload(c,p,'新增材料.pdf',body=PDF+b' different').json()['document']['id']
    db=app.state.database;db.set_reading(other,1,'scan-v1')
    db.save_page(other,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='注册资本为6000万元。'))
    service.index(p,other,[1]);models.embeds=0
    result=service.search(p,'注册资本')
    assert result['coverage']['total_pages']==4
    assert result['coverage']['indexed_pages']==3
    assert any(r['document_id']==UUID(other) for r in result['results'])
    old=next(r for r in result['results'] if r['document_id']==UUID(d))
    assert '新来源.pdf' in old['filenames']
    assert models.embeds==0 and models.ranks==2


def test_cached_rerank_never_bypasses_final_source_revalidation(tmp_path,database_url,monkeypatch):
    app,p,d,service,models=indexed(tmp_path,database_url)
    service.search(p,'注册资本')
    from dd_agent.search_queries import SearchQueries
    real=SearchQueries.candidates
    def changed(self,*args,**kwargs):
        rows=real(self,*args,**kwargs)
        app.state.database.save_page(d,'scan-v1',dict(page_number=1,status='succeeded',method='ocr',text='新的基础文字'))
        return rows
    monkeypatch.setattr(SearchQueries,'candidates',changed)
    result=service.search(p,'注册资本')
    assert not any(r['page_number']==1 for r in result['results'])
    assert result['coverage']['indexed_pages']==1 and models.ranks==1


def test_search_batches_metadata_and_does_not_require_model_slot_for_empty_result(tmp_path,database_url,monkeypatch):
    app,p,d,service,models=indexed(tmp_path,database_url)
    def forbidden(*args,**kwargs):pytest.fail('Search must not read documents one by one')
    monkeypatch.setattr(service.database,'document',forbidden)
    assert service.search(p,'资本')['results']
    with client(app) as c:empty=project(c,'空项目')
    from dd_agent.retrieval import model_slot
    with model_slot(service.settings.data_dir):
        assert service.search(empty,'资本')['status']=='not_indexed'


def test_index_reads_only_requested_pages(tmp_path,database_url,monkeypatch):
    app,p,d,service,_=setup(tmp_path,database_url)
    real=service.database.evidence_pages
    def bounded(document_id,pages=None):
        assert pages==[1]
        return real(document_id,pages=pages)
    monkeypatch.setattr(service.database,'evidence_pages',bounded)
    assert service.index(p,d,[1])['indexed_pages']==[1]


def test_search_sql_returns_fixed_rows_for_20_and_649_page_corpora(tmp_path,database_url):
    import json
    from test_page_reads import seed
    from dd_agent.performance import capture
    app,p,d,service,_=indexed(tmp_path,database_url)
    measurements=[]
    for total in (20,649):
        seed(app,d,total,prefix='注册资本第')
        version=service.version()
        with service.database.connect() as conn:
            conn.execute('DELETE FROM dd_agent.indexed_pages WHERE document_id=%s',(d,))
            conn.execute('''INSERT INTO dd_agent.indexed_pages
                (document_id,page_number,index_version,pipeline_version,source_updated_at,source_revision)
                SELECT document_id,page_number,%s,pipeline_version,updated_at,source_revision
                FROM dd_agent.effective_document_pages WHERE document_id=%s''',(version,d))
            conn.execute('''INSERT INTO dd_agent.evidence_chunks
                (id,document_id,page_number,index_version,ordinal,start_offset,end_offset,text,embedding)
                SELECT gen_random_uuid(),document_id,page_number,%s,0,0,length(text),text,%s::public.vector
                FROM dd_agent.effective_document_pages WHERE document_id=%s''',
                (version,'['+','.join(['1']+['0']*1023)+']',d))
        folder=tmp_path/f'metrics-{total}'
        with capture(folder,'test',sample_memory=False):result=service.search(p,'注册资本')
        assert result['coverage']['indexed_pages']==total and len(result['results'])==5
        counters=json.loads(next(folder.glob('*.json')).read_text(encoding='utf-8'))['counters']
        measurements.append((counters['sql.calls'],counters['sql.fetched_rows']))
    assert measurements[0]==measurements[1]
    # 向量召回之外多一次有界关键词查询；20 页和 649 页的调用次数、返回行数仍然相同。
    assert measurements[0][0]<=7 and measurements[0][1]<=27


def test_invalid_model_scores_are_never_cached(tmp_path,database_url):
    from dd_agent.retrieval import RetrievalError
    _,p,_,service,models=indexed(tmp_path,database_url)
    real=models.rerank
    def invalid(*a,**k):
        values=real(*a,**k);values[0]=float('nan');return values
    models.rerank=invalid
    with pytest.raises(RetrievalError):service.search(p,'资本')
    models.rerank=real
    assert service.search(p,'资本')['results'] and models.ranks==2


def test_deferred_visual_candidate_is_closed_without_publishing_unknown_page(tmp_path,database_url):
    from dd_agent.control import ProcessingDeferred
    from dd_agent.automatic import AutomaticProcessor
    from test_automatic import Vision, page
    app,p,d,service,_=setup(tmp_path,database_url)
    page(app.state.database,d,'注册资本2000万元')
    class BusyVision(Vision):
        def recognize(self,**kwargs):raise ProcessingDeferred()
    processor=AutomaticProcessor(service.settings,vision=BusyVision(),database=app.state.database)
    with pytest.raises(ProcessingDeferred):processor.process(p,d,[1])
    with app.state.database.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.machine_pages WHERE document_id=%s',(d,)).fetchone()['n']==0
        row=conn.execute('SELECT status,error_code FROM dd_agent.vision_candidates WHERE document_id=%s',(d,)).fetchone()
        assert row==dict(status='failed',error_code='model_busy')
