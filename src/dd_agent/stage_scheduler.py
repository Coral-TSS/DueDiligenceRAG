"""Bounded producer/consumer coordinator; the parent queue lease remains with JobManager.

Only native extraction may overlap a heavy downstream operation. OCR, vision and
embedding share one admission lock until RG-05e/f supplies measured model budgets.
All images/text remain in their existing durable stores; queues contain page IDs.
"""
from contextlib import contextmanager, nullcontext
from contextvars import copy_context
from threading import Event, Lock, RLock, Thread
from time import monotonic

from .control import ProcessingPaused, ProcessingStopped, ProcessingDeferred
from .model_admission import request_scope
from .performance import count
from .stage_publication import bind_publications, StagePaused, StageSuperseded
from .assembly_store import AssemblyStale


class StageScheduler:
    def __init__(self, manager, job, pipeline):
        self.manager,self.job,self.pipeline=manager,job,pipeline
        self.db,self.store,self.settings=manager.database,manager.stages,manager.settings
        self.lock=RLock();self.persist_lock=Lock();self.heavy=Lock()
        self.stop=Event();self.reader_done=Event();self.producer=None;self.exception=None
        self.states={s:{} for s in ('reading','vision','embedding')}
        self.waiting={s:'' for s in self.states};self.current={};self.paused=[]
        self.total=0;self.last_persist=0.;self.ready_since=None
        # index_first 进入视觉阶段后，即使视觉写回触发了新的向量任务，也先把视觉跑完。
        self._vision_pass=False
        # 工作包可能跨多页，同一页也可贡献多个工作包。它们不能挤占页号状态。
        self.parent_counts={s:0 for s in ('pending','running','succeeded','failed','cancelled')}
        self.parent_worker=None
        # 默认流水线在得知材料身份后才选择驱动，避免对名单外材料加载 tokenizer。
        choose=getattr(pipeline,'assembly_for',None)
        self.ingestion=choose(job,self.store) if choose else getattr(pipeline,'assembly_ingestion',None)
        self.assembly_progress=dict(self.ingestion.progress) if self.ingestion else {}

    def _control(self, stage=None, number=None):
        with self.db.connect() as conn:
            row=conn.execute('SELECT status,run_epoch,paused_stages FROM dd_agent.processing_jobs WHERE id=%s',(self.job['id'],)).fetchone()
        if not row or row['run_epoch']!=self.job['run_epoch']:raise ProcessingStopped()
        with self.lock:
            self.paused=row['paused_stages']
            if stage and number:self.current[stage]=number
        if row['status'] in ('paused','pause_requested'):raise ProcessingPaused()
        if self.manager._stop.is_set():raise ProcessingStopped()
        if stage in row['paused_stages']:raise StagePaused()

    def _mark(self, stage, number, state):
        with self.lock:self.states[stage][number]=state

    def _failed(self):
        return {n for rows in self.states.values() for n,state in rows.items() if state=='failed'}

    def snapshot(self):
        with self.lock:
            stages={}
            for stage,rows in self.states.items():
                stages[stage]={state:sum(value==state for value in rows.values()) for state in ('pending','running','succeeded','failed','cancelled')}
                ranges=[]
                for n in sorted(n for n,state in rows.items() if state=='succeeded'):
                    if ranges and n==ranges[-1][1]+1:ranges[-1][1]=n
                    else:ranges.append([n,n])
                stages[stage].update(ready_ranges=ranges[:20],ranges_truncated=len(ranges)>20)
                stages[stage].update(paused=stage in self.paused,waiting_reason=self.waiting[stage],current_page=self.current.get(stage))
            indexed={n for n,s in self.states['embedding'].items() if s=='succeeded'}
            return dict(scheduler='stages-v1',stages=stages,total_pages=self.total,
                parent_index_mode='shadow' if self.ingestion is not None else 'off',
                processed_pages=len(indexed),failed_pages=sorted(self._failed()),
                parent_work=dict(self.parent_counts),
                failed_work_count=self.parent_counts['failed']+self.parent_counts['cancelled'],
                assembly=dict(self.assembly_progress) if self.ingestion else None,
                failed_assembly_count=self.assembly_progress.get('failed',0),
                current_page=max(self.current.values(),default=0),stage='staged',
                window_pages=self.settings.stage_window_pages,backlog_limit=self.settings.stage_backlog_pages)

    def _persist(self, force=False):
        # Display snapshots may coalesce; page SQL and stage completion never do.
        with self.persist_lock:
            if force or monotonic()-self.last_persist>=1:
                self.manager._save(self.job,'running',self.snapshot());self.last_persist=monotonic()

    def checkpoint(self, stage, number):
        name='reading' if stage in ('reading','ocr') else 'vision' if stage.startswith('vision') else 'embedding'
        self._control(name,number);self._persist()

    @contextmanager
    def _admission(self, stage, tasks, heavy=True):
        acquired=False
        try:
            if heavy:
                while not acquired:
                    self._control(stage)
                    acquired=self.heavy.acquire(timeout=.1)
                    if not acquired:
                        with self.lock:self.waiting[stage]='model_resource'
                        self._persist()
            self._control(stage)
            with self.lock:self.waiting[stage]=''
            yield
        finally:
            if acquired:self.heavy.release()

    def _refresh_parent_work(self):
        # 固定最多五行聚合结果，不为恢复进度读取清单、向量或全卷正文。
        with self.db.connect() as conn:
            rows=conn.execute('''SELECT state,count(*) AS n FROM dd_agent.stage_jobs
                WHERE job_id=%s AND run_epoch=%s AND stage='embedding'
                AND scope ? 'parent_build' AND dd_agent.parent_build_origin_current(scope->>'parent_build') GROUP BY state''',
                (self.job['id'],self.job['run_epoch'])).fetchall()
        with self.lock:
            self.parent_counts={s:0 for s in self.parent_counts}
            for row in rows:self.parent_counts[row['state']]=row['n']

    def _parent_work_done(self):
        with self.lock:
            return not (self.parent_counts['pending'] or self.parent_counts['running'])

    def _mark_task(self,task):
        if 'parent_build' not in task['scope']:
            self._mark(task['stage'],task['scope']['pages'][0],task['state'])

    def _refresh(self, tasks):
        if not tasks:return
        with self.db.connect() as conn:
            rows=conn.execute('SELECT stage,scope,state FROM dd_agent.stage_jobs WHERE id=ANY(%s)',([t['id'] for t in tasks],)).fetchall()
        for row in rows:self._mark_task(row)
        if any('parent_build' in t['scope'] for t in tasks):self._refresh_parent_work()

    def _claims(self, stage, count):
        result=[]
        try:
            for _ in range(count):
                self._control(stage)
                task=self.store.claim(self.job['id'],stage)
                if task is None:break
                result.append(task);self._mark_task(task)
        except (StagePaused,ProcessingPaused,ProcessingStopped):
            for task in result:self.store.park(task['id'],task['owner_token'])
            self._refresh(result)
            raise
        return result

    def _execute(self, stage, tasks):
        # 先保持原页级批处理，再逐个执行有界工作包。每包单独归还重模型许可，
        # 不把 parent_build 放进按首个页号建字典的旧 publication binding。
        pages=[t for t in tasks if 'parent_build' not in t['scope']]
        packets=[t for t in tasks if 'parent_build' in t['scope']]
        try:
            if packets:
                self._refresh_parent_work()
                self._persist(force=True)
            if pages:self._execute_unit(stage,pages)
            for task in packets:self._execute_unit(stage,[task],parent=True)
        except (ProcessingPaused,ProcessingStopped):
            # 全局暂停可能在第一个包退出，尚未执行的领取也必须耐久归还。
            for task in tasks:self.store.park(task['id'],task['owner_token'])
            self._refresh(tasks)
            raise

    def _parent_consumer(self):
        if self.parent_worker is None:
            from .parent_index import ParentIndex
            from .parent_work import ParentWork
            self.parent_worker=getattr(self.pipeline,'parent_work',None)
            if self.parent_worker is None and self.ingestion is not None:
                self.parent_worker=self.ingestion.consumer
            if self.parent_worker is None:
                index=ParentIndex(self.settings,database=self.db,models=self.pipeline.evidence.models)
                self.parent_worker=ParentWork(index,self.store)
        return self.parent_worker

    def _execute_unit(self, stage, tasks, *, parent=False):
        if not tasks:return
        pages=sorted(t['scope']['pages'][0] for t in tasks)
        try:
            heavy=stage!='reading' or any(self.routes[n]!='text' for n in pages)
            publication=nullcontext() if parent else bind_publications(self.store,stage,tasks)
            with self._admission(stage,tasks,heavy),publication,request_scope('background',defer_busy=True):
                if parent:
                    if stage!='embedding':raise ValueError('工作包只支持向量阶段。')
                    task=tasks[0]
                    self._parent_consumer().run(task['id'],task['owner_token'],checkpoint=self.checkpoint)
                    return  # ParentWork 已原子发布并完成，不再用页号假完成。
                elif stage=='reading':
                    self.pipeline.reader.read(self.job['project_id'],self.job['document_id'],pages,
                        prepared=self.prepared,checkpoint=self.checkpoint)
                    rows=self.db.page_status(self.job['document_id'],self.prepared[1],pages)
                    failures={r['page_number'] for r in rows if r['status']!='succeeded'}
                    successes={r['page_number'] for r in rows if r['status']=='succeeded'}
                    failures.update(set(pages)-successes)
                elif stage=='vision':
                    result=self.pipeline.automatic.process(self.job['project_id'],self.job['document_id'],pages,checkpoint=self.checkpoint)
                    failures=set(result['failed_pages'])
                else:
                    result=self.pipeline.evidence.index(self.job['project_id'],self.job['document_id'],pages,checkpoint=self.checkpoint)
                    failures=set(result['failed_pages'])
            for task in tasks:
                if task['scope']['pages'][0] in failures:
                    self.store.fail(task['id'],task['owner_token'],category='permanent' if stage=='reading' else 'transient',error_code='stage_page_failed',retry_after=.2)
                else:
                    # Skipped valid business artifacts have no new SQL publication to retire the claim.
                    self.store.complete(task['id'],task['owner_token'],result_ref=str(task['scope']['pages'][0]))
        except ProcessingDeferred:
            for task in tasks:self.store.park(task['id'],task['owner_token'],reason='deferred',retry_after=.2)
            with self.lock:self.waiting[stage]='model_resource'
            count('resource.deferred')
        except StagePaused:
            for task in tasks:self.store.park(task['id'],task['owner_token'])
            with self.lock:self.waiting[stage]='paused'
        except (ProcessingPaused,ProcessingStopped):
            for task in tasks:self.store.park(task['id'],task['owner_token'])
            raise
        except (StageSuperseded,AssemblyStale):
            for task in tasks:self.store.fail(task['id'],task['owner_token'],category='stale',error_code='source_changed')
        except Exception:
            for task in tasks:self.store.fail(task['id'],task['owner_token'],category='transient',error_code='stage_execution_failed',retry_after=.2)
        finally:
            self._refresh(tasks);self._persist()

    def _backlog(self):
        with self.lock:
            saved={n for n,s in self.states['reading'].items() if s=='succeeded'}
            indexed={n for n,s in self.states['embedding'].items() if s=='succeeded'}
            return len(saved-indexed-self._failed())

    def _produce(self):
        next_page=1
        try:
            while not self.stop.is_set():
                try:
                    self._control('reading')
                    room=self.settings.stage_backlog_pages-self._backlog()
                    if room<=0:
                        with self.lock:self.waiting['reading']='backpressure'
                        self._persist();self.stop.wait(.1);continue
                    with self.lock:
                        pending=any(s in ('pending','running') for s in self.states['reading'].values())
                    if not pending and next_page<=self.total:
                        pages=list(range(next_page,min(self.total+1,next_page+min(room,self.settings.stage_window_pages))))
                        actual={r['page_number'] for r in self.db.page_status(self.job['document_id'],self.prepared[1],pages) if r['status']=='succeeded'}
                        for n in pages:
                            task=self.store.ensure(self.job['id'],'reading',{'pages':[n]},source_revision=self.document['sha256'],
                                policy_version=self.prepared[1],reuse=n in actual)
                            self._mark('reading',n,task['state'])
                        next_page=pages[-1]+1
                    tasks=self._claims('reading',min(room,self.settings.stage_window_pages))
                    if tasks:self._execute('reading',tasks)
                    with self.lock:
                        pending=any(s in ('pending','running') for s in self.states['reading'].values())
                    if next_page>self.total and not pending:break
                    if not tasks:self.stop.wait(.1)
                except StagePaused:
                    with self.lock:self.waiting['reading']='paused'
                    self._persist();self.stop.wait(.1)
        except BaseException as exc:
            self.exception=exc;self.stop.set()
        finally:self.reader_done.set()

    def _reconcile(self):
        for _ in range(4):
            result=self.store.reconcile(self.job['id'],self.policies)
            for task in result['tasks']:
                self._mark_task(task)
            if not result['has_more']:break
        self._refresh_parent_work()

    def _advance_assembly(self,flush):
        if self.ingestion is None:return False
        try:
            self._control('embedding')
            result=self.ingestion.step(self.job['id'],expected_epoch=self.job['run_epoch'],
                vision_policy=self.policies['vision'],reading_done=self.reader_done.is_set(),
                flush=flush,checkpoint=self.checkpoint)
            return result['worked']
        except StagePaused:
            self.ingestion.progress=dict(self.ingestion.progress,waiting='paused',worked=False)
            return False
        except (ProcessingPaused,ProcessingStopped):
            # 一个收据交接可能刚提交后才收到暂停，读取数据库更新真实保存位置。
            try:self.ingestion.status(self.job['id'],expected_epoch=self.job['run_epoch'])
            except ProcessingStopped:pass
            raise
        finally:
            with self.lock:self.assembly_progress=dict(self.ingestion.progress)
            self._refresh_parent_work()

    def _stage_open(self, stage):
        return any(state in ('pending','running') for state in self.states[stage].values())

    def _deferred_phase(self):
        """先嵌入已读页；整卷 OCR 结束且基础索引清空后，再集中跑视觉。"""
        with self.lock:
            reading_open=self._stage_open('reading')
            embedding_open=self._stage_open('embedding') or bool(self.parent_counts['pending'] or self.parent_counts['running'])
            vision_open=self._stage_open('vision')
            vision_paused='vision' in self.paused
        if not self.reader_done.is_set() or reading_open or vision_paused:
            return 'embed'
        if self._vision_pass:
            if vision_open:
                return 'vision'
            self._vision_pass=False
            return 'embed'
        if vision_open and not embedding_open:
            self._vision_pass=True
            return 'vision'
        return 'embed'

    def _deferred_settled(self):
        if not self.reader_done.is_set():
            return False
        with self.lock:
            if any(self._stage_open(stage) for stage in ('reading','vision','embedding')):
                return False
            if self.parent_counts['pending'] or self.parent_counts['running']:
                return False
            read_ok=sum(state=='succeeded' for state in self.states['reading'].values())
            return len(self.states['vision'])>=read_ok

    def run(self):
        count('ingest.index_first')
        self._control();self._persist(force=True)
        self.document=self.db.document(self.job['project_id'],self.job['document_id'])
        preflight=self.store.seed(self.job['id'])
        self.prepared=self.pipeline.reader.prepare(self.job['project_id'],self.job['document_id'],activate=False)
        with self.store.parent_scope(self.job['id'],self.job['run_epoch']) as conn:
            self.db.set_reading(self.job['document_id'],self.prepared[0]['page_count'],self.prepared[1],conn=conn)
        self.total=self.prepared[0]['page_count'];self.routes={r['page_number']:r['route'] for r in self.prepared[0]['pages']}
        if preflight['state']!='succeeded':
            claim=self.store.claim(self.job['id'],'preflight')
            if claim:self.store.complete(claim['id'],claim['owner_token'],result_ref=self.prepared[1])
        self._control()
        self.policies={'vision':self.pipeline.automatic.version(),'embedding':self.pipeline.evidence.version()}
        if self.ingestion is not None:
            self.assembly_progress=self.ingestion.status(self.job['id'],expected_epoch=self.job['run_epoch'])
        context=copy_context()
        self.producer=Thread(target=lambda:context.run(self._produce),name='page-reading-producer',daemon=True)
        self.producer.start();self._persist(force=True)
        try:
            while True:
                self._control()
                if self.exception:raise self.exception
                self._reconcile()
                with self.lock:
                    pending=sum(s=='pending' for stage in ('vision','embedding') for s in self.states[stage].values())+self.parent_counts['pending']
                if pending and self.ready_since is None:self.ready_since=monotonic()
                flush=pending>=self.settings.stage_window_pages or self.reader_done.is_set() or (self.ready_since is not None and monotonic()-self.ready_since>=self.settings.stage_flush_seconds)
                worked=False
                if flush:
                    # 基础索引完成前不加载视觉模型，避免 OCR／视觉／向量来回换入显存。
                    phase=self._deferred_phase()
                    stages=('vision',) if phase=='vision' else ('embedding',)
                    for stage in stages:
                        try:
                            tasks=self._claims(stage,self.settings.stage_window_pages)
                            if tasks:self._execute(stage,tasks);worked=True
                        except StagePaused:
                            with self.lock:self.waiting[stage]='paused'
                    if worked:self.ready_since=None
                worked=self._advance_assembly(flush) or worked
                with self.lock:
                    succeeded={n for n,s in self.states['embedding'].items() if s=='succeeded'}
                    assembly_done=self.ingestion is None or self.assembly_progress.get('terminal',False)
                    done=self.reader_done.is_set() and len(succeeded|self._failed())==self.total and self._parent_work_done() and assembly_done and self._deferred_settled()
                if done:
                    # An empty claim is not completion: all physical pages need a durable outcome.
                    with self.db.connect() as conn:
                        conn.execute('''UPDATE dd_agent.stage_jobs SET state='cancelled',error_code='upstream_failed'
                            WHERE job_id=%s AND run_epoch=%s AND state='pending'
                            AND EXISTS (SELECT 1 FROM dd_agent.stage_jobs p WHERE p.id=ANY(dd_agent.stage_jobs.depends_on) AND p.state IN ('failed','cancelled'))''',
                            (self.job['id'],self.job['run_epoch']))
                    self._control();break
                self._persist()
                if not worked:self.stop.wait(.1)
        finally:
            self.stop.set();self.producer.join()  # paused is reported only after every in-flight page/region stopped
        if self.exception:raise self.exception
        self._persist(force=True);return self.snapshot()
