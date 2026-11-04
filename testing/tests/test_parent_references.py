"""HTTP parent reads bind every continuation to immutable build and source revisions."""
from uuid import uuid4
from test_parent_index import sample
from test_workspace import application,client


def reference(tmp_path,database_url):
    db,p,d,index,plan,manager,claim,_,_=sample(tmp_path,database_url)
    index.publish(manager.stages,claim['id'],claim['owner_token'],index.embed(plan))
    app=application(tmp_path,database_url,migrate=False)
    url=f'/api/projects/{p}/documents/{d}/parent-builds/{plan["id"]}/parents/{plan["parents"][0]["id"]}'
    return db,p,d,index,plan,app,url


def test_parent_reference_continuation_preserves_exact_text_and_identity(tmp_path,database_url):
    _,p,d,index,plan,app,url=reference(tmp_path,database_url)
    with client(app) as c:
        first=c.get(url,params={'index_version':index.version(),'limit':100})
        assert first.status_code==200,first.text
        body=first.json()
        assert body['text']==plan['parents'][0]['text'][:100]
        continuation=body['continuation']
        assert continuation['url']==url
        assert continuation['params']['index_version']==index.version()
        rest=c.get(continuation['url'],params=continuation['params'])
        assert rest.status_code==200,rest.text
        assert body['text']+rest.json()['text']==plan['parents'][0]['text']
        assert rest.json()['continuation'] is None
        assert body['source_spans']==plan['parents'][0]['spans']
        assert body['index_kind']=='parent_child' and body['snapshot']=='version_pinned'


def test_source_change_rejects_continuation_and_cross_project_hides_identity(tmp_path,database_url):
    db,p,d,index,plan,app,url=reference(tmp_path,database_url)
    with client(app) as c:
        params={'index_version':index.version(),'limit':100}
        first=c.get(url,params=params).json()
        wrong=url.replace(str(p),str(uuid4()))
        assert c.get(wrong,params=params).status_code==404
        assert c.get(url.replace(plan['id'],'0'*64),params=params).status_code==404
        db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='更新的原文'))
        stale=c.get(first['continuation']['url'],params=first['continuation']['params'])
        assert stale.status_code==409
        assert stale.json()['error_code']=='stale_reference'
        assert c.get(wrong,params=params).status_code==404


def test_reference_requires_version_and_bounded_offsets(tmp_path,database_url):
    _,_,_,index,_,app,url=reference(tmp_path,database_url)
    with client(app) as c:
        for params in ({},{'index_version':'x'}, {'index_version':index.version(),'limit':4001},
                       {'index_version':index.version(),'offset':-1}):
            assert c.get(url,params=params).status_code==422
        response=c.get(url,params={'index_version':'0'*64})
        assert response.status_code==409
