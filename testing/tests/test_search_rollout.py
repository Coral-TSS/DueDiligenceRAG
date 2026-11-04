"""Query rollout is independent from background indexing and uses the existing HTTP route."""
from dataclasses import replace
from threading import Event
from uuid import uuid4
import pytest
from dd_agent.config import Settings
from dd_agent.app import create_app
from dd_agent.retrieval import EvidenceService,SearchCancelled
from test_workspace import client
from test_mixed_search import fixture
from test_parent_index import Budget


@pytest.mark.parametrize('kwargs',[
    {'search_index_mode':'all'}, {'search_index_mode':''},
    {'search_parent_documents':('*',)},
    {'search_parent_documents':tuple(str(uuid4()) for _ in range(21))},
    {'search_parent_documents':('00000000-0000-0000-0000-000000000001',)*2},
])
def test_invalid_query_rollout_is_rejected(kwargs):
    with pytest.raises(ValueError):Settings(**kwargs)


def test_query_environment_is_independent_of_shadow_index(monkeypatch):
    monkeypatch.setattr('dd_agent.config.load_dotenv',lambda *a:None)
    document=str(uuid4())
    monkeypatch.setenv('DDA_SEARCH_INDEX_MODE','mixed')
    monkeypatch.setenv('DDA_SEARCH_PARENT_DOCUMENT_IDS',' '+document+' ')
    monkeypatch.setenv('DDA_PARENT_INDEX_MODE','off')
    settings=Settings.from_env()
    assert settings.search_index_mode=='mixed' and settings.search_parent_documents==(document,)
    assert settings.parent_index_mode=='off'


@pytest.mark.parametrize('selection',['page','mixed','outside'])
def test_existing_http_route_selects_only_allowed_complete_materials(tmp_path,database_url,monkeypatch,selection):
    x=fixture(tmp_path,database_url);x.manager.pause(x.p,x.d)
    settings=replace(x.evidence.settings,search_index_mode='page' if selection=='page' else 'mixed',
        search_parent_documents=(str(uuid4()) if selection=='outside' else str(x.d),))
    def budget(_):
        assert selection!='page','Page mode must not initialize parent tokenizer'
        return Budget()
    monkeypatch.setattr('dd_agent.parent_index.LocalTokenBudget',budget)
    app=create_app(settings)
    app.state.evidence=EvidenceService(settings,models=x.index.models,database=app.state.database)
    with client(app) as c:
        response=c.post(f'/api/projects/{x.p}/search',json={'query':'注册资本'})
        assert response.status_code==200,response.text
        result=response.json()
        parents=[h for h in result['results'] if h.get('index_kind')=='parent_child']
        assert bool(parents)==(selection=='mixed')
        if parents:
            hit=parents[0];assert hit['filenames']==['合同.pdf']
            assert c.get(hit['reference']['url'],params=hit['reference']['params']).status_code==200
        assert result['coverage']['complete']
        stopped=Event();stopped.set()
        with pytest.raises(SearchCancelled):app.state.evidence.search(x.p,'资本',cancel=stopped)


def test_closing_query_rollout_returns_to_page_search_without_rebuilding(tmp_path,database_url,monkeypatch):
    x=fixture(tmp_path,database_url)
    monkeypatch.setattr('dd_agent.parent_index.LocalTokenBudget',lambda _:Budget())
    x.evidence.settings=replace(x.evidence.settings,search_index_mode='mixed',search_parent_documents=(str(x.d),))
    assert any(h.get('index_kind')=='parent_child' for h in x.evidence.search(x.p,'资本')['results'])
    x.evidence.settings=replace(x.evidence.settings,search_index_mode='page')
    assert all(h.get('index_kind')!='parent_child' for h in x.evidence.search(x.p,'资本')['results'])
    assert x.search.coverage.inspect(x.p,x.d,x.index.version())['eligible']
