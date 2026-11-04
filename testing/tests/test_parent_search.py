"""Parent ranking uses real SQL and source changes; only model inference is substituted."""
from threading import Event
from uuid import uuid4
import pytest
from test_parent_index import sample
from dd_agent.retrieval import EvidenceService,RetrievalError,SearchCancelled
from dd_agent.parent_references import ParentReferences


def prepared(tmp_path,database_url):
    from dd_agent.parent_search import ParentSearch
    db,p,d,index,plan,manager,claim,models,_=sample(tmp_path,database_url)
    index.publish(manager.stages,claim['id'],claim['owner_token'],index.embed(plan))
    evidence=EvidenceService(index.settings,models=models,database=db)
    # Synthetic inference explicitly supplies the same cache identity contract as LocalModels.
    models.cache_profile=lambda kind:models.version+'-'+kind
    return ParentSearch(evidence,index),db,p,d,plan,models


def test_same_parent_hits_are_grouped_with_exact_context_and_page_links(tmp_path,database_url):
    search,db,p,d,plan,models=prepared(tmp_path,database_url)
    result=search.search(p,'资本')
    assert result['index_kind']=='parent_child' and result['status']=='candidates'
    assert result['coverage']['complete'] is False  # full-document coverage is not established by candidate recall
    assert len(result['results'])==1
    hit=result['results'][0]
    assert len(hit['matched_children'])>=2
    assert hit['rerank_score']==max(c['rerank_score'] for c in hit['matched_children'])
    assert hit['context']['text']==plan['parents'][0]['text']
    assert [{k:v for k,v in s.items() if k!='source_url'} for s in hit['context']['source_spans']]==plan['parents'][0]['spans']
    assert {s['page_number'] for s in hit['context']['source_spans']}=={1,2}
    assert all('page=' in s['source_url'] for s in hit['context']['source_spans'])
    for child in hit['matched_children']:
        assert hit['context']['text'][child['start_offset']:child['end_offset']]==child['text']
    assert search.search(uuid4(),'资本')['results']==[]


def test_source_change_during_rerank_discards_parent(tmp_path,database_url):
    search,db,p,d,_,models=prepared(tmp_path,database_url)
    rerank=models.rerank
    def changed(query,texts):
        db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='已变更'))
        return rerank(query,texts)
    models.rerank=changed
    assert search.search(p,'资本')['results']==[]


def test_invalid_scores_and_cancellation_do_not_return_evidence(tmp_path,database_url):
    search,_,p,_,_,models=prepared(tmp_path,database_url)
    models.rerank=lambda q,t:[float('nan')]*len(t)
    with pytest.raises(RetrievalError):search.search(p,'资本')
    cancel=Event();cancel.set()
    with pytest.raises(SearchCancelled):search.search(p,'资本',cancel=cancel)


def test_cached_inference_still_rechecks_current_sources(tmp_path,database_url):
    search,db,p,d,_,models=prepared(tmp_path,database_url)
    assert search.search(p,'资本')['results']
    before=models.calls
    assert search.search(p,'资本')['results']
    assert models.calls==before
    db.save_page(d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='新版本'))
    assert search.search(p,'资本')['results']==[]


def test_search_returns_reference_resolvable_against_the_exact_build(tmp_path,database_url):
    search,db,p,d,plan,_=prepared(tmp_path,database_url)
    hit=search.search(p,'资本')['results'][0]
    ref=hit['reference']
    assert ref['url']==f'/api/projects/{p}/documents/{d}/parent-builds/{plan["id"]}/parents/{hit["parent_id"]}'
    resolved=ParentReferences(db).read(p,d,hit['build_id'],hit['parent_id'],**ref['params'])
    assert resolved['text']==hit['context']['text']
