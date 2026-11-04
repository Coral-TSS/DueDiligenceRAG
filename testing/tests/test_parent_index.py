"""Parent/child publication against real pgvector; only inference is substituted."""
from copy import deepcopy
from dataclasses import replace
import importlib
import os
from uuid import uuid4
import pytest

from dd_agent.assembly import assemble
from dd_agent.assembly_store import AssemblyStore
from dd_agent.pipeline import JobManager
from dd_agent.stage_publication import StageSuperseded
from test_retrieval import setup,TinyModels


class Budget:
    def __init__(self,scale=1):self.scale=scale;self.calls=0
    def profile(self):return f'test-tokenizer-{self.scale}'
    def counts(self,texts):
        self.calls+=1
        return [dict(embedding=len(t)*self.scale+1,reranker=len(t)*self.scale+100) for t in texts]


class Models(TinyModels):
    def __init__(self):self.calls=0
    def embed(self,*a,**k):self.calls+=1;return super().embed(*a,**k)


def module():
    try:return importlib.import_module('dd_agent.parent_index')
    except ModuleNotFoundError:pytest.fail('RG-06b parent/child publication is missing')


def sample(tmp_path,database_url,*,scale=1):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    db=app.state.database;db.set_reading(d,2,'scan-v1')
    for n,text in [(1,'注册资本2000万元，'+'甲'*350+'，'),(2,'并说明股东资料。'+'乙'*400)]:
        db.save_page(d,'scan-v1',dict(page_number=n,status='succeeded',method='text',text=text))
    models=Models();budget=Budget(scale)
    service=module().ParentIndex(evidence.settings,database=db,models=models,budget=budget)
    snapshots=AssemblyStore(db).snapshots(p,d,[1,2],ready_pages={1,2})
    parents=assemble(snapshots,final=True)['parents']
    plan=service.prepare(p,d,parents,snapshots)
    manager=JobManager(evidence.settings,database=db);job=manager.enqueue(p,d,wake=False)
    task=service.register(manager.stages,job['id'],plan)
    claim=manager.stages.claim(job['id'],'embedding')
    assert claim['id']==task['id']
    return db,p,d,service,plan,manager,claim,models,budget


def test_children_keep_exact_cross_page_spans_and_real_token_budget(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,budget=sample(tmp_path,database_url,scale=10)
    assert models.calls==0 and len(plan['parents'])==1
    assert len(plan['children'])>2
    parent=plan['parents'][0]
    originals={v.page_number:v.text for v in AssemblyStore(db).snapshots(p,d,[1,2],ready_pages={1,2})}
    covered=set()
    for child in plan['children']:
        assert child['text']==parent['text'][child['start']:child['end']]
        assert child['embedding_tokens']<=4096 and child['reranker_tokens']<=2048
        covered.update(range(child['start'],child['end']))
        for span in child['spans']:
            assert child['text'][span['derived_start']:span['derived_end']]==originals[span['page_number']][span['start']:span['end']]
    assert all(i in covered for i,c in enumerate(parent['text']) if not c.isspace())
    assert any(len({s['page_number'] for s in c['spans']})==2 for c in plan['children'])


def test_publication_is_atomic_idempotent_and_preserves_old_page_index(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    from dd_agent.retrieval import EvidenceService
    old=EvidenceService(service.settings,models=TinyModels(),database=db)
    old.index(p,d,[1,2])
    computed=service.embed(plan)
    identity=service.publish(manager.stages,claim['id'],claim['owner_token'],computed)
    assert service.publish(manager.stages,claim['id'],claim['owner_token'],computed)==identity
    meta=service.metadata(p,d,identity)
    assert meta['current'] and meta['parents']==1 and meta['children']>=2
    assert old.search(p,'资本')['results']
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_builds').fetchone()['n']==1
        row=conn.execute('SELECT state,result_ref FROM dd_agent.stage_jobs WHERE id=%s',(claim['id'],)).fetchone()
        assert row==dict(state='succeeded',result_ref=identity)
    assert models.calls==1


def test_nonfirst_page_change_invalidates_old_build_and_refuses_old_replay(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    computed=service.embed(plan);identity=service.publish(manager.stages,claim['id'],claim['owner_token'],computed)
    db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='非首页已变更'))
    assert service.metadata(p,d,identity)['current'] is False
    with pytest.raises(StageSuperseded):service.publish(manager.stages,claim['id'],claim['owner_token'],computed)


def test_source_changed_during_embedding_leaves_no_artifacts(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    real=models.embed
    def changed(*a,**k):
        db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='计算中来源已变更'))
        return real(*a,**k)
    models.embed=changed
    computed=service.embed(plan)
    with pytest.raises(StageSuperseded):service.publish(manager.stages,claim['id'],claim['owner_token'],computed)
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_builds').fetchone()['n']==0


def test_child_failure_rolls_back_everything_and_same_vectors_can_retry(tmp_path,database_url):
    import psycopg
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    computed=service.embed(plan)
    with db.connect() as conn:conn.execute('ALTER TABLE dd_agent.parent_index_children ADD CONSTRAINT reject_probe CHECK(ordinal<1)')
    with pytest.raises(psycopg.errors.CheckViolation):service.publish(manager.stages,claim['id'],claim['owner_token'],computed)
    with db.connect() as conn:
        for table in ('builds','parents','children','dependencies'):
            assert conn.execute(f'SELECT count(*) AS n FROM dd_agent.parent_index_{table}').fetchone()['n']==0
        assert conn.execute('SELECT state FROM dd_agent.stage_jobs WHERE id=%s',(claim['id'],)).fetchone()['state']=='running'
        conn.execute('ALTER TABLE dd_agent.parent_index_children DROP CONSTRAINT reject_probe')
    identity=service.publish(manager.stages,claim['id'],claim['owner_token'],computed)
    assert service.metadata(p,d,identity)['current'] and models.calls==1


def test_resume_and_wrong_owner_cannot_publish(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    computed=service.embed(plan)
    with pytest.raises(StageSuperseded):service.publish(manager.stages,claim['id'],uuid4(),computed)
    manager.pause(p,d);manager.resume(p,d,wake=False)
    with pytest.raises(StageSuperseded):service.publish(manager.stages,claim['id'],claim['owner_token'],computed)
    assert service.metadata(str(uuid4()),d,plan['id']) is None
    with db.connect() as conn:assert conn.execute('SELECT count(*) AS n FROM dd_agent.parent_index_builds').fetchone()['n']==0


def test_manifest_corruption_and_model_version_change_are_refused(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    computed=service.embed(plan);broken=deepcopy(computed);broken['children'][0]['text']='篡改文字'
    with pytest.raises(ValueError):service.publish(manager.stages,claim['id'],claim['owner_token'],broken)
    models.version='changed-embedding'
    with pytest.raises(ValueError):service.publish(manager.stages,claim['id'],claim['owner_token'],computed)


def test_blank_ready_page_publishes_zero_children_without_tokenizer_or_embedding(tmp_path,database_url):
    db,p,d,service,_,manager,_,models,budget=sample(tmp_path,database_url)
    db.save_page(d,'scan-v1',dict(page_number=1,status='succeeded',method='text',text='  '))
    snapshots=AssemblyStore(db).snapshots(p,d,[1],ready_pages={1})
    before=budget.calls
    plan=service.prepare(p,d,[],snapshots)
    task=service.register(manager.stages,manager.latest(p,d)['id'],plan)
    claim=manager.stages.claim(task['job_id'],'embedding')
    identity=service.publish(manager.stages,claim['id'],claim['owner_token'],service.embed(plan))
    meta=service.metadata(p,d,identity)
    assert meta['current'] and meta['parents']==meta['children']==0
    assert models.calls==0 and budget.calls==before


def test_gap_is_boundary_dependency_and_filling_it_invalidates_build(tmp_path,database_url):
    db,p,d,service,_,manager,_,_,_=sample(tmp_path,database_url)
    db.set_reading(d,3,'scan-v1')
    with db.connect() as conn:conn.execute('DELETE FROM dd_agent.document_pages WHERE document_id=%s AND page_number=2',(d,))
    db.save_page(d,'scan-v1',dict(page_number=3,status='succeeded',method='text',text='并说明第三页。'))
    snapshots=AssemblyStore(db).snapshots(p,d,[1,2,3],ready_pages={1,3})
    plan=service.prepare(p,d,assemble(snapshots,final=True)['parents'],snapshots)
    task=service.register(manager.stages,manager.latest(p,d)['id'],plan)
    claim=manager.stages.claim(task['job_id'],'embedding')
    identity=service.publish(manager.stages,claim['id'],claim['owner_token'],service.embed(plan))
    assert service.metadata(p,d,identity)['current']
    db.save_page(d,'scan-v1',dict(page_number=2,status='succeeded',method='text',text='缺页已补齐'))
    assert not service.metadata(p,d,identity)['current']


@pytest.mark.parametrize('damage',['text','dependencies','span_order'])
def test_forged_spans_cannot_be_made_valid_by_rehashing_parent(tmp_path,database_url,damage):
    from dd_agent.assembly import digest,VERSION
    db,p,d,service,plan,_,_,_,_=sample(tmp_path,database_url)
    snapshots=AssemblyStore(db).snapshots(p,d,[1,2],ready_pages={1,2})
    parents=deepcopy(plan['parents'])
    if damage=='text':parents[0]['text']='伪造原文'+parents[0]['text'][4:]
    elif damage=='dependencies':parents[0]['dependencies']=[]
    else:parents[0]['spans'].reverse()
    parents[0]['id']=digest(dict(version=VERSION,**{k:v for k,v in parents[0].items() if k!='id'}))
    with pytest.raises(ValueError):service.prepare(p,d,parents,snapshots)


def test_model_change_marks_historical_build_noncurrent(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,models,_=sample(tmp_path,database_url)
    identity=service.publish(manager.stages,claim['id'],claim['owner_token'],service.embed(plan))
    models.version='next-embedding'
    assert not service.metadata(p,d,identity)['current']


def test_wrong_stage_policy_cannot_use_another_build_manifest(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    manager.stages.park(claim['id'],claim['owner_token'])
    wrong=manager.stages.ensure(claim['job_id'],'embedding',claim['scope'],source_revision=plan['input_revision'],
        policy_version='wrong-policy',dependencies=claim['dependencies'])
    taken=manager.stages.claim(claim['job_id'],'embedding')
    assert taken['id']==wrong['id']
    with pytest.raises(ValueError):service.publish(manager.stages,taken['id'],taken['owner_token'],service.embed(plan))


def test_completed_old_owner_replay_is_refused_after_resume(tmp_path,database_url):
    db,p,d,service,plan,manager,claim,_,_=sample(tmp_path,database_url)
    computed=service.embed(plan)
    service.publish(manager.stages,claim['id'],claim['owner_token'],computed)
    manager.pause(p,d);manager.resume(p,d,wake=False)
    with pytest.raises(StageSuperseded):service.publish(manager.stages,claim['id'],claim['owner_token'],computed)


def test_real_local_tokenizers_count_without_loading_inference_weights(tmp_path):
    if os.getenv('DDA_TEST_LOCAL_MODELS')!='1':pytest.skip('Enable local tokenizer integration')
    from dd_agent.config import Settings
    from dd_agent.token_budget import LocalTokenBudget
    budget=LocalTokenBudget(Settings(data_dir=tmp_path))
    values=budget.counts(['注册资本2000万元。','😀'*700])
    assert len(values)==2 and all(type(v[k]) is int and v[k]>0 for v in values for k in ('embedding','reranker'))
    assert budget.profile()==budget.profile()


def test_real_parent_child_embedding_pgvector_publication(tmp_path,database_url):
    if os.getenv('DDA_TEST_LOCAL_MODELS')!='1':pytest.skip('Enable real parent index inference')
    from dd_agent.retrieval import LocalModels,valid_vector
    from dd_agent.token_budget import LocalTokenBudget
    from dd_agent.model_runtime import close_runtime
    db,p,d,service,old,manager,_,_,_=sample(tmp_path,database_url)
    service.models=LocalModels(service.settings);service.budget=LocalTokenBudget(service.settings)
    snapshots=AssemblyStore(db).snapshots(p,d,[1,2],ready_pages={1,2})
    try:
        plan=service.prepare(p,d,old['parents'],snapshots)
        task=service.register(manager.stages,manager.latest(p,d)['id'],plan)
        taken=manager.stages.claim(task['job_id'],'embedding')
        computed=service.embed(plan)
        identity=service.publish(manager.stages,taken['id'],taken['owner_token'],computed)
        assert service.metadata(p,d,identity)['current']
        with db.connect() as conn:
            row=conn.execute('''SELECT vector_dims(embedding) AS dimensions,
                embedding <=> %s::public.vector AS distance FROM dd_agent.parent_index_children
                WHERE build_id=%s AND id=%s''',(valid_vector(computed['vectors'][0]),identity,computed['children'][0]['id'])).fetchone()
        assert row['dimensions']==1024 and abs(row['distance'])<1e-5
    finally:close_runtime()
