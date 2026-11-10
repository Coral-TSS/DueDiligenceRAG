"""Local processing orchestration and a resumable, database-backed queue."""
# 阅读入口：这是“处理材料”的总调度，不是让大模型规划报告的 Agent。
# HTTP 的自动处理按钮 -> JobManager.enqueue() -> 后台 drain() -> Pipeline.run()。
# CLI 的 process-pages 则直接调用 Pipeline.run()，不创建 processing_jobs 队列任务。
# 建议先看 run() 理解三个阶段，再看 drain() 理解批次、保存和恢复。
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from .automatic import AutomaticProcessor, AutomaticError
from .config import Settings
from .db import Database
from .reading import Reader, PdfWorker
from .retrieval import EvidenceService
from .storage import Storage
from .control import ProcessingPaused, ProcessingStopped
from .review import ReviewConflict
from .performance import observed, timed, count


def _merge_stage(left, right):
    """把同一阶段的多段页结果拼起来。单段调用保持原对象。"""
    merged=dict(right)
    for key in ('processed_pages','skipped_pages','failed_pages','indexed_pages'):
        if key in left or key in right:
            merged[key]=sorted(set(left.get(key,[])) | set(right.get(key,[])))
    return merged
from .stage_jobs import StageJobs


class ProcessingPipeline:
    # reader / automatic / evidence 可以注入，测试因此能只替换昂贵推理，保留真实数据库。
    def __init__(self, settings=None, reader=None, automatic=None, evidence=None, database=None, assembly_ingestion=None):
        self.settings = settings or Settings.from_env()
        database = database if database is not None else Database(self.settings.database_url)
        self.database=database
        self.reader = reader or Reader(self.settings, database=database)
        self.automatic = automatic or AutomaticProcessor(self.settings, database=database)
        self.evidence = evidence or EvidenceService(self.settings, database=database)
        # 显式注入供内部联调；生产驱动须等调度器提供具体 job 后按名单选择。
        self.assembly_ingestion=assembly_ingestion

    def assembly_for(self,job,stages):
        """按本进程配置为当前材料选择驱动，不在构造时读取权重或正文。

        每次调度创建新实例，恢复位置来自数据库而非旧 Python 对象。
        off 仅关闭新组装／修复；已登记包仍受原消费者及代次机制管理，
        不删除已发布向量、尾段或收据，也不切换当前页级检索。
        """
        if self.assembly_ingestion is not None:return self.assembly_ingestion
        mode=self.settings.parent_index_mode
        selected=mode=='on' or (mode=='shadow' and str(job['document_id']) in self.settings.parent_index_documents)
        if not selected:
            return None
        from .parent_index import ParentIndex
        from .repair_ingestion import RepairingAssemblyIngestion
        index=ParentIndex(self.settings,database=self.database,models=self.evidence.models)
        return RepairingAssemblyIngestion(index,stages)

    @observed('pipeline')
    def run(self, project_id, document_id, pages, checkpoint=None):
        # 整批先读完，再嵌入，再视觉，最后按新文本再嵌入。同一阶段的多段 20 页共用已加载的模型。
        # checkpoint 在页／区域安全边界协作暂停。
        if not pages:
            raise ValueError('请指定有效页码。')
        groups=[list(pages)[i:i+20] for i in range(0,len(list(pages)),20)]
        kwargs = {'checkpoint':checkpoint} if checkpoint else {}
        reading=None
        for group in groups:
            if checkpoint:checkpoint('reading',group[0])
            reading=self.reader.read(project_id, document_id, group, **kwargs)
        count('ingest.index_first')
        for group in groups:
            if checkpoint:checkpoint('embedding',group[0])
            self.evidence.index(project_id, document_id, group, **kwargs)
        automatic=None
        for group in groups:
            if checkpoint:checkpoint('vision',group[0])
            part=self.automatic.process(project_id, document_id, group, **kwargs)
            automatic=part if automatic is None else _merge_stage(automatic, part)
        index=None
        for group in groups:
            if checkpoint:checkpoint('embedding',group[0])
            part=self.evidence.index(project_id, document_id, group, **kwargs)
            index=part if index is None else _merge_stage(index, part)
        return dict(reading=reading, automatic=automatic, index=index)


class JobManager:
    """One local worker; PostgreSQL lease also excludes other app processes.

    A lost lease leaves an interrupted job paused until an explicit resume.
    Resumed batches recheck versions and reuse saved work.
    """
    def __init__(self, settings=None, pipeline_factory=None, database=None):
        self.settings = settings or Settings.from_env()
        self.database = database if database is not None else Database(self.settings.database_url)
        self.stages = StageJobs(self.database)
        self.factory = pipeline_factory or (lambda: ProcessingPipeline(self.settings, database=self.database))
        # 三类锁不要混淆：
        # 1. _mutex：仅防止本进程重复启动后台线程。
        # 2. PostgreSQL 74332103：跨应用进程的队列执行权，贯穿 drain()。
        # 3. PostgreSQL 74332104：短事务内串行化入队／暂停／继续操作。
        # 模型内存竞争另由 retrieval.model_slot() 的文件锁控制。
        self._mutex = Lock()
        self._thread = None
        self._stop = Event()

    def _document(self, project_id, document_id):
        doc = self.database.document(project_id, document_id)
        if doc is None or doc['status'] != 'registered':
            raise AutomaticError('材料不存在、不属于当前项目，或格式暂不支持自动处理。')
        return doc

    def enqueue(self, project_id, document_id, wake=True):
        self._document(project_id, document_id)
        with self.database.connect() as conn:
            # 暂停任务也属于已有活动任务，不能通过再次点击“自动处理”绕开暂停。
            conn.execute('SELECT pg_advisory_xact_lock(74332104)')
            row = conn.execute("SELECT * FROM dd_agent.processing_jobs WHERE document_id=%s AND status IN ('queued','running','pause_requested','paused') FOR UPDATE", (document_id,)).fetchone()
            if row is None:
                row = conn.execute("INSERT INTO dd_agent.processing_jobs(id,project_id,document_id,status) VALUES (%s,%s,%s,'queued') RETURNING *", (uuid4(),project_id,document_id)).fetchone()
            self.stages.seed(row['id'], conn=conn)
        if wake:self.wake()
        return row

    def latest(self, project_id, document_id):
        self._document(project_id, document_id)
        with self.database.connect() as conn:
            row=conn.execute('SELECT * FROM dd_agent.processing_jobs WHERE document_id=%s ORDER BY created_at DESC,id DESC LIMIT 1', (document_id,)).fetchone()
            if row:
                # worker_present 表示队列执行锁被占用，不是精确到单页的模型心跳。
                # execution_state 是这次查询派生的展示状态，不是新的数据库状态枚举。
                # A transaction-scoped probe never steals a live worker's lease.
                # GET only reports the interruption; startup/control settle it.
                row['worker_present']=not self._lease_available(conn)
                row['execution_state']='interrupted' if row['status'] in ('running','pause_requested') and not row['worker_present'] else row['status']
            return row

    @staticmethod
    def _lease_available(conn):
        # try 是非阻塞探测：拿不到锁就立即返回 False，不等待耗时模型结束。
        # True 表示当前可以获得执行权；探测锁随本次查询事务结束自动释放。
        return conn.execute('SELECT pg_try_advisory_xact_lock(74332103) AS acquired').fetchone()['acquired']

    @staticmethod
    def _recover_orphans(conn, document_id=None):
        # 只有拿到执行权，才能确认旧执行者已不再持有队列锁。
        # 不直接重跑旧 running：保留成果并暂停，用户明确继续后复用原任务。
        # document_id 有值时只收束该材料；启动 drain() 时则检查整个队列。
        # Caller must hold the queue lease. Never infer abandonment from age.
        query='''UPDATE dd_agent.processing_jobs SET status='paused',
            progress=jsonb_set(progress,'{recovery}',jsonb_build_object(
                'reason','worker_lost','previous_status',status,'recovered_at',clock_timestamp())),
            updated_at=clock_timestamp() WHERE status IN ('running','pause_requested')'''
        conn.execute(query+(' AND document_id=%s' if document_id is not None else ''),
                     (document_id,) if document_id is not None else ())

    @timed('job.save')
    def _save(self, job, status, progress, error=''):
        # 推理期间用户可能刚请求暂停。不能用旧线程的 running 进度覆盖新暂停状态。
        # CASE 保护的是状态；进度仍保存，避免丢掉刚完成的页／区域信息。
        with self.database.connect() as conn:
            conn.execute('''UPDATE dd_agent.processing_jobs SET status=CASE
                WHEN status='paused' THEN 'paused'
                WHEN status='pause_requested' AND %s NOT IN ('paused','failed') THEN 'pause_requested'
                ELSE %s END,progress=%s,error_code=%s,updated_at=clock_timestamp() WHERE id=%s AND run_epoch=%s''',
                (status,status,Jsonb(progress),error,job['id'],job['run_epoch']))

    def _control(self, project_id, document_id, resume=False):
        self._document(project_id,document_id)
        with self.database.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(74332104)')
            if self._lease_available(conn):
                # 没有执行者时，“等待当前页保存”的残留请求可以直接收束。
                self._recover_orphans(conn,document_id)
            row=conn.execute('SELECT * FROM dd_agent.processing_jobs WHERE document_id=%s ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE',(document_id,)).fetchone()
            if row is None:raise AutomaticError('尚无处理任务，请先启动自动处理。')
            status=row['status']
            new_epoch = bool(resume and status=='paused')
            if resume:
                # 活执行者尚未确认暂停时不能抢先继续，否则会混淆在途结果。
                if status=='pause_requested':raise ReviewConflict('正在保存当前结果，请等待“已暂停”后继续。')
                if status=='paused':status='queued'
            elif status in ('queued','running'):
                # 排队时没有在途计算，可立即暂停；运行时只能发出暂停请求。
                status='paused' if status=='queued' else 'pause_requested'
            result = conn.execute('''UPDATE dd_agent.processing_jobs SET status=%s,
                progress=CASE WHEN %s THEN progress-'recovery' ELSE progress END,
                run_epoch=run_epoch+%s,stage_cursor=CASE WHEN %s THEN 0 ELSE stage_cursor END,
                updated_at=clock_timestamp() WHERE id=%s RETURNING *''',(status,resume,int(new_epoch),new_epoch,row['id'])).fetchone()
            if new_epoch:
                self.stages.invalidate_older(conn,result['id'],result['run_epoch'])
                self.stages.seed(result['id'],conn=conn)
            return result

    def pause(self, project_id, document_id):
        return self._control(project_id,document_id)

    def resume(self, project_id, document_id, wake=True):
        row=self._control(project_id,document_id,resume=True)
        if wake:self.wake()
        return row

    @timed('job.checkpoint')
    def _checkpoint(self, job, progress, stage, number):
        # 回调抛出的两种控制异常由 drain() 单独处理，不能当成 OCR／索引失败。
        # current_page 是当前检查位置；不等于完成数，继续时可能回到较早页面。
        with self.database.connect() as conn:
            row=conn.execute('SELECT status,run_epoch FROM dd_agent.processing_jobs WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
            if row is None or row['run_epoch']!=job['run_epoch']:raise ProcessingStopped()
            if row['status'] in ('pause_requested','paused'):raise ProcessingPaused()
            if self._stop.is_set():raise ProcessingStopped()
            progress.update(stage=stage,current_page=number)
            conn.execute('UPDATE dd_agent.processing_jobs SET progress=%s,updated_at=clock_timestamp() WHERE id=%s',(Jsonb(progress),job['id']))

    def control_stage(self, project_id, document_id, stage, *, pause):
        if stage not in ('reading','vision','embedding'): raise ValueError('阶段无效。')
        self._document(project_id,document_id)
        with self.database.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(74332104)')
            row=conn.execute('SELECT * FROM dd_agent.processing_jobs WHERE document_id=%s ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE',(document_id,)).fetchone()
            if row is None or row['status'] not in ('queued','running','pause_requested','paused'):raise ReviewConflict('没有可控制的活动任务。')
            stages=set(row['paused_stages'])
            if pause:stages.add(stage)
            else:stages.discard(stage)
            return conn.execute('UPDATE dd_agent.processing_jobs SET paused_stages=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *',(sorted(stages),row['id'])).fetchone()

    def drain(self):
        with self.database.lease() as lease:
            # session 级锁跟随此数据库连接，即使 commit() 也不会释放。
            # 连接／进程退出后数据库会释放它，其他服务才有机会收束或执行任务。
            if not lease.execute('SELECT pg_try_advisory_lock(74332103) AS acquired').fetchone()['acquired']:return
            try:
                self._recover_orphans(lease)
                lease.commit()
                while not self._stop.is_set():
                    job = lease.execute("SELECT * FROM dd_agent.processing_jobs WHERE status IN ('queued','running','pause_requested') ORDER BY created_at,id LIMIT 1").fetchone()
                    lease.commit()
                    if job is None:return
                    self._run_job(job)
            finally:
                lease.execute('SELECT pg_advisory_unlock(74332103)')

    @observed('job')
    def _run_job(self, job):
        # 只在实际领取任务后记录；空轮询不产生性能文件。
        # age 包含历史暂停时间，不冒充纯排队等待时间。
        count('job.queue_age_seconds', max(0., (datetime.now(timezone.utc)-job['created_at']).total_seconds()))
        pipeline=self.factory()
        if isinstance(pipeline,ProcessingPipeline):
            return self._run_staged(job,pipeline)
        progress = {'processed_pages':0,'total_pages':0,'failed_pages':[],**job['progress']}
        # processed_pages 是已保存的整批处理检查点，不是有效向量覆盖。
        # 例如批次停在 15，但第 16–20 页基础文字可能已单独保存。
        try:
            self._checkpoint(job,progress,'preflight',progress['processed_pages']+1)
            doc = self._document(job['project_id'],job['document_id'])
            total = self.database.reading(doc['id']).get('page_count')
            if not total:
                path = Storage(self.settings.data_dir,self.settings.max_upload_bytes).path(doc['storage_key'])
                total = PdfWorker(self.settings).inspect(path)['page_count']
            progress['total_pages'] = total
            self._save(job,'running',progress)
            # Legacy injected run-only diagnostic adapters retain their existing contract.
            # Recheck earlier batches too: source/model changes while
            # paused must invalidate caches. Unchanged pages skip work.
            for start in range(1,total+1,5):
                # 继续时从前页重检：阅读／增强／索引各层自己按版本跳过。
                # 不能只从 processed_pages+1 开始，否则暂停期间变更的旧页会漏重建。
                if self._stop.is_set():raise ProcessingStopped()
                pages = list(range(start,min(start+5,total+1)))
                self._checkpoint(job,progress,'reading',start)
                result = pipeline.run(job['project_id'],doc['id'],pages,
                    checkpoint=lambda stage,number:self._checkpoint(job,progress,stage,number))
                failed = set(progress['failed_pages'])
                # 这里只累计任务失败页，不是对全材料“缺失／未知”的完整统计。
                for stage in (result['automatic'],result['index']):failed.update(stage.get('failed_pages',[]))
                progress.update(processed_pages=max(progress['processed_pages'],pages[-1]),failed_pages=sorted(failed))
                self._save(job,'running',progress)
                self._checkpoint(job,progress,'batch_saved',pages[-1])
            self._checkpoint(job,progress,'completed',total)
            self._save(job,'partial' if progress['failed_pages'] else 'completed',progress)
            # A pause can race with the final save.
            with self.database.connect() as conn:
                conn.execute("UPDATE dd_agent.processing_jobs SET status='paused' WHERE id=%s AND run_epoch=%s AND status='pause_requested'",(job['id'],job['run_epoch']))
        except ProcessingPaused:
            self._save(job,'paused',progress)
        except ProcessingStopped:
            # 服务正常关闭也在安全点保存暂停，重启不默默重跑整个材料。
            progress['recovery']=dict(reason='service_stopped',previous_status='running')
            self._save(job,'paused',progress)
            return
        except Exception:
            # Never persist credentials, raw exception text or client documents.
            self._save(job,'failed',progress,'local_processing_failed')

    def _run_staged(self, job, pipeline):
        from .office import _extension, _PACKAGE, _LEGACY
        doc = self._document(job['project_id'], job['document_id'])
        if any(_extension(name) in _PACKAGE or _extension(name) in _LEGACY for name in doc.get('filenames') or []):
            try:
                self._save(job, 'running', {})
                outcome = pipeline.evidence.index_office(job['project_id'], doc['id'])
                if outcome.get('unsupported'):
                    # 这一份失败就结束，不重试，也不要让队列里的其他材料一起失败。
                    self._save(job, 'failed', outcome, outcome.get('error_code') or 'legacy_unsupported')
                else:
                    self._save(job, 'partial' if outcome['failed_units'] else 'completed', outcome)
            except Exception:
                self._save(job, 'failed', {}, 'local_processing_failed')
            return
        from .stage_scheduler import StageScheduler
        scheduler=StageScheduler(self,job,pipeline)
        try:
            progress=scheduler.run()
            self._save(job,'partial' if progress['failed_pages'] or progress.get('failed_work_count',0)
                or progress.get('failed_assembly_count',0) else 'completed',progress)
        except ProcessingPaused:
            self._save(job,'paused',scheduler.snapshot())
        except ProcessingStopped:
            progress=scheduler.snapshot();progress['recovery']=dict(reason='service_stopped',previous_status='running')
            self._save(job,'paused',progress)
        except Exception:
            self._save(job,'failed',scheduler.snapshot(),'local_processing_failed')
        finally:
            with self.database.connect() as conn:
                conn.execute("UPDATE dd_agent.processing_jobs SET status='paused' WHERE id=%s AND run_epoch=%s AND status='pause_requested'",(job['id'],job['run_epoch']))

    def wake(self):
        with self._mutex:
            if self._thread and self._thread.is_alive():return
            self._stop.clear()
            self._thread = Thread(target=self._work,daemon=True,name='local-document-processor')
            self._thread.start()

    def _work(self):
        # 它是同进程中的后台线程；关浏览器页面不会停止它，退出应用进程则会。
        # Periodic wake also handles a competing process releasing its lease.
        while not self._stop.is_set():
            try:self.drain()
            except (psycopg.Error,OSError):pass
            self._stop.wait(3)

    def close(self):
        # 发出协作式停止信号，不在任意计算指令上冻结模型；短 join 不是强杀子进程。
        self._stop.set()
        if self._thread:self._thread.join(timeout=1)
