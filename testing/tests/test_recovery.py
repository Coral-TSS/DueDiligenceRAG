"""Orphan detection must use a real process lease, never timestamps alone."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from psycopg.types.json import Jsonb

from dd_agent.config import ROOT
from dd_agent.pipeline import JobManager
from test_retrieval import setup


def process(tmp_path,settings,job,mode,calls=None):
    manifest=tmp_path/(mode+'-'+os.urandom(4).hex()+'.json')
    manifest.write_text(json.dumps(dict(database_url=settings.database_url,data_dir=str(settings.data_dir),
        job_id=str(job['id']),mode=mode,calls=str(calls))),encoding='utf-8')
    env={**os.environ,'PYTHONPATH':os.pathsep.join([str(ROOT/'src'),str(ROOT/'testing/tests')]),
         'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    p=subprocess.Popen([sys.executable,str(ROOT/'testing/scripts/recovery/worker_fixture.py'),str(manifest)],
        env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    assert p.stdout.readline().strip()=='READY'
    return p


def finish(p):
    if p.poll() is None:p.kill()
    p.communicate(timeout=10)


def test_live_old_worker_is_not_recovered_but_actual_exit_is_visible(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    evidence.index(p,d,[1])
    manager=JobManager(evidence.settings);job=manager.enqueue(p,d,wake=False)
    owner=process(tmp_path,evidence.settings,job,'hold')
    try:
        live=manager.latest(p,d)
        assert live['execution_state']=='running' and live['worker_present']
        # A second manager must not change a live owner's task or execute it.
        competitor=JobManager(evidence.settings,pipeline_factory=lambda:pytest.fail('Duplicate worker'))
        competitor.drain()
        assert competitor.latest(p,d)['status']=='running'
        assert manager.pause(p,d)['status']=='pause_requested'
        finish(owner)
        stale=manager.latest(p,d)
        assert stale['status']=='pause_requested'  # Status GET remains read-only.
        assert stale['execution_state']=='interrupted' and not stale['worker_present']
        resumed=manager.resume(p,d,wake=False)
        assert resumed['id']==job['id'] and resumed['status']=='queued'
        assert evidence.index(p,d,[1])['skipped_pages']==[1]
    finally:finish(owner)


@pytest.mark.parametrize('status',['running','pause_requested'])
def test_startup_settles_orphan_without_processing_saved_material(tmp_path,database_url,status):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    evidence.index(p,d,[1])
    manager=JobManager(evidence.settings,pipeline_factory=lambda:pytest.fail('Orphans must wait for resume'))
    job=manager.enqueue(p,d,wake=False)
    saved=dict(processed_pages=1,total_pages=3,current_page=2,stage='vision',failed_pages=[])
    with app.state.database.connect() as conn:
        conn.execute('UPDATE dd_agent.processing_jobs SET status=%s,progress=%s WHERE id=%s',
                     (status,Jsonb(saved),job['id']))
    manager.drain()
    result=manager.latest(p,d)
    assert result['status']=='paused' and result['progress']['processed_pages']==1
    assert result['progress']['recovery']['reason']=='worker_lost'
    assert result['progress']['recovery']['previous_status']==status
    assert evidence.index(p,d,[1])['skipped_pages']==[1]
    assert len(app.state.database.pages(d))==2


@pytest.mark.parametrize('stop_at',['checkpoint','before_batch'])
def test_shutdown_checkpoint_settles_pause_instead_of_leaving_running(tmp_path,database_url,stop_at):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    class Work:
        def run(self,project_id,document_id,pages,checkpoint=None):
            manager._stop.set()
            checkpoint('embedding',1)
    def factory():
        if stop_at=='before_batch':manager._stop.set()
        return Work()
    manager=JobManager(evidence.settings,pipeline_factory=factory)
    manager.enqueue(p,d,wake=False);manager.drain()
    result=manager.latest(p,d)
    assert result['status']=='paused' and result['error_code']==''
    assert result['progress']['recovery']['reason']=='service_stopped'


def test_two_processes_resume_once_and_reuse_saved_vectors(tmp_path,database_url):
    app,p,d,evidence,_=setup(tmp_path,database_url)
    app.state.database.set_reading(d,2,'scan-v1')
    evidence.index(p,d,[1])
    manager=JobManager(evidence.settings);job=manager.enqueue(p,d,wake=False)
    calls=tmp_path/'calls.jsonl'
    processes=[process(tmp_path,evidence.settings,job,'drain',calls) for _ in range(2)]
    try:
        for child in processes:child.stdin.write('GO\n');child.stdin.flush()
        for child in processes:
            out,err=child.communicate(timeout=20)
            assert child.returncode==0,out+err
        results=[json.loads(s) for s in calls.read_text().splitlines()]
        assert len(results)==1
        assert results[0]['skipped_pages']==[1] and results[0]['indexed_pages']==[2]
        assert manager.latest(p,d)['status']=='completed'
    finally:
        for child in processes:finish(child)


def test_progress_view_distinguishes_saved_batches_from_recheck_position():
    script=ROOT/'src/dd_agent/static/progress.js'
    assert script.is_file(),'Progress view is missing'
    node=ROOT/'.local/harness/runtime/node-v22.19.0-win-x64/node.exe'
    js="""const {processingView}=require(process.argv[1]);
    const job={status:'running',execution_state:'running',worker_present:true,
        progress:{processed_pages:15,total_pages:649,stage:'reading',current_page:1}};
    console.log(JSON.stringify([processingView(job),processingView({...job,execution_state:'interrupted'})]));"""
    result=subprocess.run([str(node),'-e',js,str(script)],capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
    active,interrupted=json.loads(result.stdout)
    assert '已保存整批进度 15/649 页' in active['text'] and '检查位置' in active['text']
    assert active['active'] and not active['resumeAvailable']
    assert not interrupted['active'] and interrupted['resumeAvailable']
    assert '执行已中断' in interrupted['text'] and '15/649' in interrupted['text']
