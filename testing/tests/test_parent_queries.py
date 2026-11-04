"""Real pgvector recall and revision-bound parent reads."""
from uuid import uuid4
import pytest
from test_parent_index import sample


def test_recall_and_bounded_context_preserve_sources(tmp_path,database_url):
    from dd_agent.parent_queries import ParentQueries
    db,p,d,index,plan,manager,claim,models,_=sample(tmp_path,database_url)
    index.publish(manager.stages,claim['id'],claim['owner_token'],index.embed(plan))
    queries=ParentQueries(db)
    rows=queries.candidates(p,index.version(),models.embed(['资本'])[0])
    assert rows and len(rows)<=12
    row=rows[0]
    assert row['source_spans'] and row['build_id']==plan['id']
    context=queries.context(p,d,index.version(),row['build_id'],row['parent_id'],offset=0,limit=100)
    parent=plan['parents'][0]
    assert context['text']==parent['text'][:100]
    assert context['next_offset']==100 and context['source_spans']==parent['spans']
    rest=queries.context(p,d,index.version(),row['build_id'],row['parent_id'],offset=100)
    assert context['text']+rest['text']==parent['text']
    assert queries.candidates(uuid4(),index.version(),models.embed(['资本'])[0])==[]
    assert queries.context(uuid4(),d,index.version(),row['build_id'],row['parent_id']) is None
    db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='修订'))
    assert queries.candidates(p,index.version(),models.embed(['资本'])[0])==[]
    assert queries.context(p,d,index.version(),row['build_id'],row['parent_id'],offset=100) is None


@pytest.mark.parametrize('offset,limit',[(-1,100),(0,0),(0,4001),(True,100)])
def test_context_rejects_unbounded_arguments(offset,limit):
    from dd_agent.parent_queries import ParentQueries
    with pytest.raises(ValueError):
        ParentQueries(None).context(uuid4(),uuid4(),'v','b','p',offset=offset,limit=limit)
