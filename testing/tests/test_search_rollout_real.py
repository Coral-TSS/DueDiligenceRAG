"""Opt-in real local OCR/vision/embedding/rerank through controlled HTTP search."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4
import pytest
from dd_agent.app import create_app
from dd_agent.config import Settings
from dd_agent.model_runtime import close_runtime
from dd_agent.performance import capture
from dd_agent.storage import Storage
from test_workspace import client


def test_real_http_mixed_search_reference_and_restart_rollback(tmp_path,database_url,request):
    if os.getenv('DDA_TEST_MIXED_MODELS')!='1':pytest.skip('Enable real controlled mixed HTTP acceptance')
    output=Path(os.getenv('DDA_TEST_RUN_DIR',str(tmp_path)))/'mixed-http-real'
    output.mkdir(parents=True,exist_ok=True)
    settings=Settings(database_url=database_url,data_dir=tmp_path/'data')
    seed=create_app(settings);db=seed.state.database;db.migrate()
    source=output/'synthetic.pdf'
    helper=Path(__file__).resolve().parents[1]/'scripts/performance/make_sample.py'
    subprocess.run([str(settings.pdf_python),str(helper),str(source)],check=True,timeout=60)
    p=db.create_project('受控混合 HTTP 合成验收')['id']
    storage=Storage(settings.data_dir,settings.max_upload_bytes)
    def register(name,body):return db.register(p,storage.save(p,name,io.BytesIO(body)))[0]['id']
    first=register('跨页材料.pdf',source.read_bytes())
    other=register('页级材料.pdf',source.read_bytes()+b'\n% independent legacy material\n')
    db.provider.close()
    # Production default mixed mode discovers the completed parent material without a query allowlist.
    settings=replace(settings,parent_index_mode='shadow',parent_index_documents=(str(first),))
    app=create_app(settings);db=app.state.database
    request.addfinalizer(db.provider.close)
    request.addfinalizer(lambda:close_runtime(settings))
    db.provider.start();manager=app.state.jobs
    request.addfinalizer(manager.close)
    traces={};responses={}
    def measured(label,operation):
        directory=output/label
        with capture(directory,label,sample_memory=True):result=operation()
        traces[label]=json.loads(next(directory.glob('*.json')).read_text(encoding='utf-8'))
        return result
    try:
        close_runtime()
        manager.enqueue(p,first,wake=False);manager.enqueue(p,other,wake=False)
        measured('build',manager.drain)
        assert all(manager.latest(p,d)['status']=='completed' for d in (first,other))
        with client(app) as c:
            response=measured('mixed_http',lambda:c.post(f'/api/projects/{p}/search',json={'query':'公司的注册资本是多少？'}))
            assert response.status_code==200,response.text
            result=response.json();responses['mixed']=len(response.content)
            assert result['coverage']['complete'] and result['coverage']['indexed_pages']==4
            assert {h['index_kind'] for h in result['results']}=={'parent_child','page'}
            parent=next(h for h in result['results'] if h['index_kind']=='parent_child')
            assert parent['filenames']==['跨页材料.pdf'] and parent['document_id']==str(first)
            assert any('2000' in child['text'] for child in parent['matched_children'])
            ref=parent['reference']
            resolved=measured('reference_http',lambda:c.get(ref['url'],params=ref['params']))
            assert resolved.status_code==200 and resolved.json()['text']==parent['context']['text']
            responses['reference']=len(resolved.content)
            assert c.get(ref['url'].replace(str(p),str(uuid4())),params=ref['params']).status_code==404
        # Restart with query page while preserving both index families and all source data.
        rollback=create_app(replace(settings,search_index_mode='page'))
        with client(rollback) as c:
            response=measured('rollback_http',lambda:c.post(f'/api/projects/{p}/search',json={'query':'公司的注册资本是多少？'}))
            assert response.status_code==200,response.text
            result=response.json();responses['rollback']=len(response.content)
            assert result['results'] and all(h.get('index_kind')!='parent_child' for h in result['results'])
            assert result['coverage']['complete']
            assert c.get(ref['url'],params=ref['params']).status_code==200
    finally:
        (output/'summary.json').write_text(json.dumps(dict(phases=traces,response_bytes=responses),
            ensure_ascii=False,indent=2),encoding='utf-8')
