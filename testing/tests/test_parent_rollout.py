"""Controlled production wiring: real scheduler/SQL, substitute only inference."""
from dataclasses import replace
from uuid import uuid4

import pytest

from dd_agent.config import Settings
from dd_agent.control import ProcessingPaused
from dd_agent.pipeline import ProcessingPipeline
from test_parent_index import Budget
from test_stage_scheduler import fixture,cleanup


@pytest.mark.parametrize('kwargs',[
    {'parent_index_mode':'all'},
    {'parent_index_mode':'shadow'},
    {'parent_index_documents':('*',)},
    {'parent_index_documents':tuple(str(uuid4()) for _ in range(21))},
    {'parent_index_documents':('00000000-0000-0000-0000-000000000001',)*2},
])
def test_invalid_rollout_configuration_is_rejected(kwargs):
    # A typo or missing allowlist must never silently enable every document.
    try:
        with pytest.raises(ValueError):Settings(**kwargs)
    except TypeError:pytest.fail('Controlled rollout settings are missing')


def test_environment_loads_explicit_document_allowlist(monkeypatch):
    monkeypatch.setattr('dd_agent.config.load_dotenv',lambda *a:None)
    doc=str(uuid4())
    monkeypatch.setenv('DDA_PARENT_INDEX_MODE','shadow')
    monkeypatch.setenv('DDA_PARENT_INDEX_DOCUMENT_IDS',' '+doc+' ')
    settings=Settings.from_env()
    assert getattr(settings,'parent_index_mode',None)=='shadow'
    assert getattr(settings,'parent_index_documents',None)==(doc,)


def configure(manager,pipeline,mode,documents):
    try:settings=replace(manager.settings,parent_index_mode=mode,parent_index_documents=tuple(documents))
    except TypeError:pytest.fail('Controlled rollout settings are missing')
    manager.settings=pipeline.settings=settings
    return settings


@pytest.mark.parametrize('selection',['off','outside','shadow'])
def test_production_scheduler_only_assembles_allowlisted_documents(tmp_path,database_url,monkeypatch,selection):
    db,manager,p,d,job,calls,_,_,evidence=fixture(tmp_path,database_url,total=13)
    pipeline=manager.factory()
    configure(manager,pipeline,'off' if selection=='off' else 'shadow',
        [d if selection!='outside' else str(uuid4())])
    def budget(settings):
        assert selection=='shadow','Unselected document must not initialize parent tokenizer'
        return Budget()
    monkeypatch.setattr('dd_agent.parent_index.LocalTokenBudget',budget)
    manager.drain()
    row=manager.latest(p,d)
    assert row['status']=='completed'
    assert len(calls['reading'])==13 and row['progress']['processed_pages']==13
    assert evidence.search(p,'注册资本')['coverage']['indexed_pages']==13
    with db.connect() as conn:
        builds=conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_builds').fetchone()['n']
    if selection=='shadow':
        assert builds>=2
        assert row['progress']['assembly']['closed'] and row['progress']['assembly']['covered_pages']==13
        assert row['progress']['parent_index_mode']=='shadow'
    else:
        assert builds==0 and row['progress']['assembly'] is None


def test_restart_with_rollout_off_keeps_tail_and_reenable_reuses_saved_pages(tmp_path,database_url,monkeypatch):
    db,manager,p,d,job,calls,_,_,_=fixture(tmp_path,database_url,total=13)
    pipeline=manager.factory();configure(manager,pipeline,'shadow',[d])
    # The scheduler's cheap Automatic fixture always appends a new revision.
    # Across a *new job* that fabricates a source change, unlike production
    # AutomaticProcessor.process which reuses matching base/profile results.
    # Keep this inference substitute faithful to that storage contract.
    recognize=pipeline.automatic.process
    def reusable_vision(project,document,pages,**kwargs):
        pending=[];skipped=[]
        with db.connect() as conn:
            for number in pages:
                source=db.page_row(document,number)
                saved=conn.execute('''SELECT 1 FROM dd_agent.machine_pages WHERE document_id=%s
                    AND page_number=%s AND base_revision=%s AND profile_version=%s LIMIT 1''',
                    (document,number,source['raw_revision'],pipeline.automatic.version())).fetchone()
                (skipped if saved else pending).append(number)
        result=recognize(project,document,pending,**kwargs)
        result['skipped_pages']=skipped
        return result
    monkeypatch.setattr(pipeline.automatic,'process',reusable_vision)
    monkeypatch.setattr('dd_agent.parent_index.LocalTokenBudget',lambda _:Budget())
    actual=getattr(pipeline,'assembly_for',None)
    assert actual is not None,'Production pipeline lacks controlled assembly wiring'
    stopped=[]
    def assembly_for(job,stages):
        driver=actual(job,stages);original=driver.step
        def step(*a,**kwargs):
            result=original(*a,**kwargs)
            if result['saved_pages']>=10 and not stopped:
                stopped.append(True);manager.pause(p,d);raise ProcessingPaused()
            return result
        driver.step=step
        return driver
    monkeypatch.setattr(pipeline,'assembly_for',assembly_for)
    manager.drain()
    assert manager.latest(p,d)['status']=='paused'
    with db.connect() as conn:
        before=conn.execute('SELECT generation,state FROM dd_agent.assembly_checkpoints WHERE document_id=%s',(d,)).fetchone()
        assert before['state']['next_page']==11
        receipts=conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches').fetchone()['n']
    # Fresh pipeline simulates the next process's configuration snapshot. No
    # explicit driver is injected; old epoch work follows normal resume rules.
    settings=configure(manager,pipeline,'off',[d])
    manager.factory=lambda:ProcessingPipeline(settings,reader=pipeline.reader,automatic=pipeline.automatic,
        evidence=pipeline.evidence,database=db)
    manager.resume(p,d,wake=False);manager.drain()
    assert manager.latest(p,d)['status']=='completed'
    with db.connect() as conn:
        assert conn.execute('SELECT generation,state FROM dd_agent.assembly_checkpoints WHERE document_id=%s',(d,)).fetchone()==before
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_batches').fetchone()['n']==receipts
    settings=replace(settings,parent_index_mode='shadow');manager.settings=settings
    manager.enqueue(p,d,wake=False);manager.drain()
    row=manager.latest(p,d)
    assert row['status']=='completed' and row['progress']['assembly']['closed'],row['progress']
    assert len(calls['reading'])==len(set(calls['reading']))==13
