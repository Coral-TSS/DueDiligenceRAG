"""Check shared candidate budget and exclusion of standalone historical publications."""
from test_mixed_search import fixture
from dd_agent.assembly import assemble


def test_standalone_parent_build_is_not_a_managed_query_candidate(tmp_path,database_url):
    x=fixture(tmp_path,database_url)
    snapshots=x.store.snapshots(x.p,x.d,[2],ready_pages={2})
    plan=x.index.prepare(x.p,x.d,assemble(snapshots,final=True)['parents'],snapshots)
    task=x.index.register(x.manager.stages,x.job['id'],plan)
    claim=x.manager.stages.claim(x.job['id'],'embedding',task_id=task['id'])
    x.index.publish(x.manager.stages,claim['id'],claim['owner_token'],x.index.embed(plan))
    vector=x.index.models.embed(['注册资本'])[0]
    repository=x.search.parent.queries
    assert plan['id'] in {r['build_id'] for r in repository.candidates(x.p,x.index.version(),vector)}
    managed=repository.candidates(x.p,x.index.version(),vector,documents=[x.d],current_receipts_only=True)
    assert managed and plan['id'] not in {r['build_id'] for r in managed}
    assert all(h.get('build_id')!=plan['id'] for h in x.search.search(x.p,'注册资本')['results'])


def test_dual_recall_has_one_twelve_candidate_rerank_budget(tmp_path,database_url):
    x=fixture(tmp_path,database_url)
    x.db.save_page(x.other,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='注册资本2000万元，'*1000))
    x.evidence.index(x.p,x.other,[1])
    calls=[];rerank=x.index.models.rerank
    def counted(query,texts):calls.append(len(texts));return rerank(query,texts)
    x.index.models.rerank=counted
    result=x.search.search(x.p,'注册资本')
    assert calls==[12] and len(result['results'])<=5
    other_project=x.db.create_project('另一个项目')['id']
    empty=x.search.search(other_project,'注册资本')
    assert empty['results']==[] and empty['parent_documents']==[]
