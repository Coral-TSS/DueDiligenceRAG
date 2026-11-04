"""Bounded material enumeration for the future evidence MCP adapter."""
from uuid import UUID, uuid4

from test_workspace import application, client, project, upload, PDF


def test_material_catalog_paginates_with_current_metadata_and_no_cross_project_cursor(tmp_path, database_url):
    app=application(tmp_path,database_url,migrate=True)
    with client(app) as c:
        p=project(c);other=project(c,'其他项目')
        empty=c.get(f'/api/projects/{p}/material-catalog')
        assert empty.status_code==200,empty.text
        assert empty.json()=={'items':[],'next_cursor':None,'total_documents':0}
        docs=[upload(c,p,f'材料{i}.pdf',PDF+str(i).encode()).json()['document'] for i in range(3)]
        foreign=upload(c,other).json()['document']
        app.state.database.set_reading(UUID(docs[0]['id']),649,'current-reading')
        # A duplicate source name must not create an additional material.
        upload(c,p,'材料别名.pdf',PDF+b'0')
        found=[];cursor=None
        for _ in range(3):
            params={'limit':1}
            if cursor:params['after']=cursor
            result=c.get(f'/api/projects/{p}/material-catalog',params=params)
            assert result.status_code==200,result.text
            data=result.json()
            assert data['total_documents']==3 and len(data['items'])==1
            found.extend(data['items']);cursor=data['next_cursor']
        assert cursor is None
        assert [d['document_id'] for d in found]==sorted(d['id'] for d in docs)
        known=next(d for d in found if d['document_id']==docs[0]['id'])
        assert known['page_count']==649 and known['reading_version']=='current-reading'
        assert sorted(known['filenames'])==['材料0.pdf','材料别名.pdf']
        assert all('storage_key' not in d and 'text' not in d for d in found)
        assert next(d for d in found if d['document_id']==docs[1]['id'])['page_count'] is None
        denied=c.get(f'/api/projects/{p}/material-catalog',params={'after':foreign['id']})
        assert denied.status_code==404
        assert foreign['id'] not in denied.text


def test_material_catalog_rejects_invalid_bounds_and_unknown_project(tmp_path,database_url):
    with client(application(tmp_path,database_url,migrate=True)) as c:
        p=project(c)
        for params in ({'limit':0},{'limit':101},{'after':'not-a-uuid'}):
            assert c.get(f'/api/projects/{p}/material-catalog',params=params).status_code==422
        assert c.get(f'/api/projects/{uuid4()}/material-catalog').status_code==404
