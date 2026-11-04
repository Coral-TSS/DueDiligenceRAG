"""Optional acceptance gate: actual local inference and actual background wiring.

No customer files, credentials, business database or synthetic model outputs.
This validates the existing rollout implementation, rather than adding a new
production feature. Timing is observational, not a machine-independent SLA.
"""
from contextvars import copy_context
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from dd_agent.config import Settings
from dd_agent.db import Database
from dd_agent.db_connections import ConnectionProvider
from dd_agent.model_runtime import close_runtime
from dd_agent.performance import capture
from dd_agent.pipeline import JobManager,ProcessingPipeline
from dd_agent.retrieval import EvidenceService
from dd_agent.storage import Storage


def test_real_rollout_pause_reuse_and_interactive_competition(tmp_path,database_url,monkeypatch,request):
    if os.getenv('DDA_TEST_ROLLOUT_MODELS')!='1':
        pytest.skip('Enable bounded real rollout/model competition acceptance')
    import dd_agent.model_runtime as runtime
    output=Path(os.getenv('DDA_TEST_RUN_DIR',str(tmp_path)))/'parent-real-gate'
    output.mkdir(parents=True,exist_ok=True)
    settings=Settings(database_url=database_url,data_dir=tmp_path/'data',
        stage_window_pages=1,stage_backlog_pages=4,stage_flush_seconds=.1,search_index_mode='page')
    helper=Path(__file__).resolve().parents[1]/'scripts/performance/make_sample.py'
    source=output/'mixed-synthetic.pdf'
    subprocess.run([str(settings.pdf_python),str(helper),str(source)],check=True,timeout=60)
    body=source.read_bytes()
    provider=ConnectionProvider(database_url,max_size=settings.db_pool_size,
        timeout=settings.db_pool_timeout,max_waiting=settings.db_pool_max_waiting)
    request.addfinalizer(provider.close)
    db=Database(database_url,provider=provider);db.migrate();provider.start()
    p=db.create_project('真实模型受控合成验收')['id']
    storage=Storage(settings.data_dir,settings.max_upload_bytes)
    def register(name,content):
        return db.register(p,storage.save(p,name,io.BytesIO(content)))[0]['id']
    first=register('mixed.pdf',body)
    other=register('mixed-second.pdf',body+b'\n% independent contention sample\n')
    settings=replace(settings,parent_index_mode='shadow',parent_index_documents=(str(first),str(other)))
    manager=JobManager(settings,database=db)
    evidence=EvidenceService(settings,database=db)
    traces={};thread=None;observer=None;observer_stop=Event();queue_samples=[];pause_events=[]
    def measured(label,operation):
        folder=output/label
        with capture(folder,label,sample_memory=True):result=operation()
        traces[label]=json.loads(next(folder.glob('*.json')).read_text(encoding='utf-8'))
        return result
    def finished(document):
        row=manager.latest(p,document)
        assert row['status']=='completed',row
        a=row['progress']['assembly']
        assert a['closed'] and a['terminal'] and a['covered_pages']==2 and not a['failed']
        return row
    try:
        close_runtime()
        manager.enqueue(p,first,wake=False)
        actual=ProcessingPipeline.assembly_for
        # Observe a real persisted save boundary, then request the same public
        # pause operation used by the UI. Inference and publication stay real.
        def with_pause(pipeline,job,stages):
            driver=actual(pipeline,job,stages);step=driver.step
            def observe(*a,**kwargs):
                checkpoint=kwargs['checkpoint']
                def boundary(stage,number):
                    if stage=='assembly_saved' and not pause_events:
                        pause_events.append(number);manager.pause(p,first)
                    checkpoint(stage,number)
                return step(*a,**dict(kwargs,checkpoint=boundary))
            driver.step=observe
            return driver
        with monkeypatch.context() as patch:
            patch.setattr(ProcessingPipeline,'assembly_for',with_pause)
            measured('initial_pause',manager.drain)
        assert pause_events and manager.latest(p,first)['status']=='paused'
        manager.resume(p,first,wake=False)
        measured('resume',manager.drain);finished(first)
        # The native producer may reach the scan before or after pause. Require
        # exactly one OCR attempt across both phases, regardless of that race.
        initial_ocr=traces['initial_pause']['counters'].get('ocr.page.attempts',0)
        resumed_ocr=traces['resume']['counters'].get('ocr.page.attempts',0)
        assert initial_ocr+resumed_ocr==1
        result=measured('seed_search',lambda:evidence.search(p,'公司的注册资本是多少？'))
        assert result['results'] and any('2000' in r['text'] for r in result['results'])
        manager.enqueue(p,first,wake=False)
        measured('reuse',manager.drain);finished(first)
        assert traces['reuse']['counters'].get('ocr.page.attempts',0)==0
        assert traces['reuse']['counters'].get('worker.tokenizer_worker.starts',0)==0
        assert traces['reuse']['counters'].get('assembly.handoff.plan_reused',0)>=1
        assert traces['reuse']['counters'].get('parent_work.restored_vectors',0)>=1

        # A fresh material forces real work while the first remains searchable.
        # Probe only admission state; do not hold or manufacture a model slot.
        close_runtime();manager.enqueue(p,other,wake=False)
        failures=[]
        def background():
            try:measured('contended_background',manager.drain)
            except BaseException as exc:failures.append(exc)
        def sample_queue():
            while not observer_stop.wait(.02):
                broker=runtime._broker
                if broker:queue_samples.append(broker.admission.snapshot())
        context=copy_context()
        observer=Thread(target=sample_queue,daemon=True);observer.start()
        thread=Thread(target=lambda:context.run(background));thread.start()
        end=monotonic()+300;overlap=False
        while monotonic()<end and thread.is_alive():
            broker=runtime._broker
            if broker and broker.snapshot()['request_id'] is not None and broker.admission.snapshot()['active']:
                overlap=True;break
            sleep(.02)
        assert overlap,'No actual background model operation observed'
        result=measured('contended_search',lambda:evidence.search(p,'本公司的注册资本数额？'))
        assert result['results'] and any('2000' in r['text'] for r in result['results'])
        assert all(r['document_id'] in (first,other) for r in result['results'])
        thread.join(300)
        assert not thread.is_alive(),'Background did not finish after interactive query'
        assert not failures,failures
        finished(other)
        assert any(v['interactive']>0 for v in queue_samples),'Query never actually queued behind a model'
        assert max(v['waiting'] for v in queue_samples)<=settings.model_max_waiting
        assert traces['contended_background']['counters'].get('ocr.page.attempts',0)==1
        assert result['coverage']['indexed_pages']>=2
    finally:
        manager._stop.set()
        if thread and thread.is_alive():thread.join(300)
        observer_stop.set()
        if observer:observer.join(2)
        # Keep measurements even on failure. No raw query/DSN in this summary.
        summary=dict(sample_sha256=hashlib.sha256(body).hexdigest(),pages=2,mode='real_local_models',
            platform=platform.platform(),python=platform.python_version(),threads=settings.model_cpu_threads,
            window_pages=settings.stage_window_pages,backlog_pages=settings.stage_backlog_pages,
            queue_peak=max((v['waiting'] for v in queue_samples),default=0),
            interactive_queue_observed=any(v['interactive']>0 for v in queue_samples),
            pause_events=pause_events,traces=traces)
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        close_runtime();db.provider.close()
