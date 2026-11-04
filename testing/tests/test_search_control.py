from dataclasses import replace
from threading import Event
import asyncio
import json
from uuid import uuid4
import pytest

from dd_agent.config import Settings
from dd_agent.app import create_app
from test_workspace import client
from test_search_path import indexed


def test_cancel_and_deadline_do_not_cache_unpublished_model_outputs(tmp_path,database_url,monkeypatch):
    from dd_agent import retrieval, model_admission
    _,p,_,service,models=indexed(tmp_path,database_url)
    stop=Event();stop.set()
    with pytest.raises(retrieval.SearchCancelled):service.search(p,'资本',cancel=stop)
    assert models.embeds==0
    clock=[0.]
    monkeypatch.setattr(retrieval,'monotonic',lambda:clock[0])
    monkeypatch.setattr(model_admission,'monotonic',lambda:clock[0])
    service.settings=replace(service.settings,search_timeout_seconds=.05)
    real=models.embed
    def late(*a,**k):
        result=real(*a,**k);clock[0]=1.;return result
    models.embed=late
    with pytest.raises(retrieval.SearchTimeout):service.search(p,'资本')
    models.embed=real;service.settings=replace(service.settings,search_timeout_seconds=120)
    clock[0]=0.
    assert service.search(p,'资本')['results'] and models.embeds==2


@pytest.mark.parametrize('error,status,code',[('RetrievalBusy',503,'model_busy'),('SearchTimeout',504,'search_timeout'),('SearchCancelled',499,'search_cancelled')])
def test_api_reports_busy_timeout_and_cancel_explicitly(tmp_path,monkeypatch,error,status,code):
    from dd_agent import retrieval
    app=create_app(Settings(data_dir=tmp_path))
    monkeypatch.setattr(app.state.database,'project_exists',lambda p:True)
    def fail(*a,**k):raise getattr(retrieval,error)('合成错误')
    monkeypatch.setattr(app.state.evidence,'search',fail)
    with client(app) as c:
        response=c.post(f'/api/projects/{uuid4()}/search',json={'query':'资本'})
        assert response.status_code==status and response.json()['error_code']==code
        if code=='model_busy':assert response.headers['retry-after']=='1'


def test_http_disconnect_signals_search_worker(tmp_path,monkeypatch):
    app=create_app(Settings(data_dir=tmp_path));entered=Event();received_cancel=Event()
    monkeypatch.setattr(app.state.database,'project_exists',lambda p:True)
    def search(*a,cancel=None,**k):
        assert cancel is not None
        entered.set()
        if cancel.wait(2):received_cancel.set()
        return {'status':'not_indexed','results':[],'coverage':{}}
    monkeypatch.setattr(app.state.evidence,'search',search)
    async def run():
        body=json.dumps({'query':'资本'}).encode();sent=False
        async def receive():
            nonlocal sent
            if not sent:sent=True;return dict(type='http.request',body=body,more_body=False)
            if entered.is_set():return {'type':'http.disconnect'}
            await asyncio.sleep(.01)
            return await receive()
        async def send(message):pass
        scope=dict(type='http',asgi={'version':'3.0'},http_version='1.1',method='POST',scheme='http',
            path=f'/api/projects/{uuid4()}/search',root_path='',query_string=b'',
            headers=[(b'host',b'127.0.0.1'),(b'content-type',b'application/json')],client=('127.0.0.1',1234),server=('127.0.0.1',80))
        await asyncio.wait_for(app(scope,receive,send),4)
    asyncio.run(run())
    assert received_cancel.wait(1)


def test_async_search_preserves_existing_database_busy_handler(tmp_path,monkeypatch):
    from dd_agent.db_connections import DatabaseBusy
    app=create_app(Settings(data_dir=tmp_path))
    def busy(*a):raise DatabaseBusy('synthetic')
    monkeypatch.setattr(app.state.database,'project_exists',busy)
    with client(app) as c:
        assert c.post(f'/api/projects/{uuid4()}/search',json={'query':'资本'}).status_code==503
