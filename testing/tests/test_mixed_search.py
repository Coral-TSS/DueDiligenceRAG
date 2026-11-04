"""Material routing and one shared rerank against real old/new indexes."""
from io import BytesIO
from uuid import uuid4
import pytest
from test_assembly_ingestion import sample,step,drain,machine
from dd_agent.retrieval import EvidenceService
from dd_agent.storage import Storage


def fixture(tmp_path,database_url,*,published=True):
    from dd_agent.mixed_search import MixedSearch
    x=sample(tmp_path,database_url,total=2)
    step(x)
    if published:drain(x)
    evidence=EvidenceService(x.index.settings,models=x.index.models,database=x.db)
    evidence.index(x.p,x.d,[1,2])
    stored=Storage(tmp_path,10000).save(x.p,'另一材料.pdf',BytesIO(b'%PDF-1.4\nother\n%%EOF'))
    other,_=x.db.register(x.p,stored);x.other=other['id']
    x.db.set_reading(x.other,1,'scan-v1')
    x.db.save_page(x.other,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='另一公司注册资本100万元。'))
    evidence.index(x.p,x.other,[1])
    x.evidence=evidence;x.search=MixedSearch(evidence,x.index,(str(x.d),))
    return x


def test_complete_material_replaces_old_hits_and_reranks_candidates_once(tmp_path,database_url):
    x=fixture(tmp_path,database_url)
    calls=[];rerank=x.index.models.rerank
    def counted(query,texts):calls.append(list(texts));return rerank(query,texts)
    x.index.models.rerank=counted
    result=x.search.search(x.p,'注册资本')
    assert len(calls)==1 and len(calls[0])<=12
    assert result['coverage']['complete'] and result['coverage']['indexed_pages']==3
    assert {hit['index_kind'] for hit in result['results']}=={'page','parent_child'}
    assert all(hit['index_kind']=='parent_child' for hit in result['results'] if str(hit['document_id'])==str(x.d))
    assert all(hit['index_kind']=='page' for hit in result['results'] if str(hit['document_id'])==str(x.other))


def test_unpublished_material_falls_back_as_a_whole(tmp_path,database_url):
    x=fixture(tmp_path,database_url,published=False)
    result=x.search.search(x.p,'注册资本')
    assert result['coverage']['complete']
    assert result['results'] and all(h['index_kind']=='page' for h in result['results'])
    assert result['parent_documents']==[]


def test_source_change_during_rerank_drops_parent_and_reports_selection_change(tmp_path,database_url):
    x=fixture(tmp_path,database_url)
    rerank=x.index.models.rerank
    def changed(query,texts):
        x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='变更后的原文'))
        return rerank(query,texts)
    x.index.models.rerank=changed
    result=x.search.search(x.p,'注册资本')
    assert not result['coverage']['complete'] and result['selection_changed']
    assert all(str(h['document_id'])!=str(x.d) for h in result['results'])


def test_no_closed_parent_keeps_page_path_without_parent_version_work(tmp_path,database_url):
    from dd_agent.mixed_search import MixedSearch
    x=fixture(tmp_path,database_url)
    checkpoint=x.store.load(x.p,x.d)
    x.store.restart(x.p,x.d,expected_generation=checkpoint['generation'])
    x.index.version=lambda:pytest.fail('Empty selection must not profile parent tokenizer')
    result=MixedSearch(x.evidence,x.index,()).search(x.p,'注册资本')
    assert result['results'] and all(h['index_kind']=='page' for h in result['results'])


def test_rebuilt_material_during_rerank_reports_changed_candidate_snapshot(tmp_path,database_url):
    x=fixture(tmp_path,database_url)
    rerank=x.index.models.rerank
    def rebuild(query,texts):
        x.db.save_page(x.d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='更新后的主体资料，'))
        machine(x,[1])
        checkpoint=x.store.load(x.p,x.d)
        x.store.restart(x.p,x.d,expected_generation=checkpoint['generation'])
        step(x);drain(x)
        return rerank(query,texts)
    x.index.models.rerank=rebuild
    result=x.search.search(x.p,'注册资本')
    assert x.search.coverage.inspect(x.p,x.d,x.index.version())['eligible']
    assert result['selection_changed'] and not result['coverage']['complete']
    assert all(str(h['document_id'])!=str(x.d) for h in result['results'])


@pytest.mark.parametrize('documents',[('*',),tuple(str(uuid4()) for _ in range(21)),('bad',)])
def test_invalid_explicit_selection_is_rejected(documents):
    from dd_agent.mixed_search import MixedSearch
    with pytest.raises(ValueError):MixedSearch(None,None,documents)
