"""Default mixed mode discovers only project-local closed candidates without starting work."""
import pytest
from dd_agent.config import Settings
from dd_agent.parent_coverage import ParentCoverage
from test_mixed_search import fixture
from test_parent_index import Budget


def test_default_mode_and_missing_environment_choose_mixed(monkeypatch):
    assert Settings().search_index_mode=='mixed'
    monkeypatch.setattr('dd_agent.config.load_dotenv',lambda *a:None)
    monkeypatch.delenv('DDA_SEARCH_INDEX_MODE',raising=False)
    monkeypatch.delenv('DDA_SEARCH_PARENT_DOCUMENT_IDS',raising=False)
    assert Settings.from_env().search_index_mode=='mixed'
    assert Settings(search_index_mode='mixed').search_parent_documents==()


def test_default_search_discovers_complete_parent_material(tmp_path,database_url,monkeypatch):
    x=fixture(tmp_path,database_url)
    monkeypatch.setattr('dd_agent.parent_index.LocalTokenBudget',lambda _:Budget())
    result=x.evidence.search(x.p,'注册资本')
    assert result['index_kind']=='mixed'
    assert result['parent_documents']==[str(x.d)]
    assert {h['index_kind'] for h in result['results']}=={'parent_child','page'}
    assert result['coverage']['complete']
    other=x.db.create_project('其他项目')['id']
    discovered=ParentCoverage(x.db).discover(other)
    assert discovered==dict(documents=[],limited=False)


def test_unpublished_discovery_falls_back_without_starting_work(tmp_path,database_url,monkeypatch):
    x=fixture(tmp_path,database_url,published=False)
    monkeypatch.setattr('dd_agent.parent_index.LocalTokenBudget',lambda _:Budget())
    with x.db.connect() as conn:
        before=conn.execute('SELECT count(*) AS n FROM dd_agent.stage_jobs').fetchone()['n']
    result=x.evidence.search(x.p,'注册资本')
    assert result['results'] and all(h['index_kind']=='page' for h in result['results'])
    with x.db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.stage_jobs').fetchone()['n']==before
