"""有界跨页窗口协调器；调度器按受控材料名单选择，也支持内部注入。

数据库页面是唯一文字来源，tail／receipt／handoff 是恢复位置。此处不做 OCR，
不在队列里复制整卷正文，也不因过期来源自动重启整份组装。
"""
from time import monotonic

from .assembly import VERSION,digest
from .assembly_readiness import resolved_prefix
from .assembly_store import AssemblyStore,AssemblyStale,AssemblyConflict
from .assembly_work import AssemblyWork
from .assembly_handoff import ReceiptHandoff
from .parent_work import ParentWork
from .control import ProcessingPaused,ProcessingStopped
from .stage_publication import StagePaused,StageSuperseded
from .performance import timed


class AssemblyIngestion:
    def __init__(self,index,stages,*,window_pages=None,max_pending=4):
        window_pages=index.settings.stage_window_pages if window_pages is None else window_pages
        if type(window_pages) is not int or not 1<=window_pages<=20 or type(max_pending) is not int or not 1<=max_pending<=8:
            raise ValueError('组装窗口须为 1–20 页，未结束工作包上限须为 1–8。')
        self.index,self.stages,self.database=index,stages,index.database
        self.window,self.max_pending=window_pages,max_pending
        self.store=AssemblyStore(self.database);self.work=AssemblyWork(self.store,stages)
        self.bridge=ReceiptHandoff(index,stages);self.consumer=ParentWork(index,stages)
        self.version=None;self.halt=None;self.ready_since=None
        self.progress=dict(enabled=True,terminal=False,closed=False,saved_pages=0,generation=0,
            covered_pages=0,gap_pages=0,failed=0,waiting='',active_work=0,handoff_pending=False,worked=False)

    def _context(self,job_id,epoch,*,active=False):
        with self.database.connect() as conn:
            parent=self.stages._parent(conn,job_id)
            if parent['run_epoch']!=epoch:raise ProcessingStopped()
            if active:AssemblyWork._active(parent,epoch)
            document=self.store._document(conn,parent['project_id'],parent['document_id'])
            checkpoint=self.store._checkpoint(conn,parent['document_id'])
            reset=self.bridge._latest_reset(conn,parent['document_id'])
        state=checkpoint['state']
        if state:
            if state.get('version')!=VERSION or state.get('checksum')!=digest({k:v for k,v in state.items() if k!='checksum'}):
                raise ValueError('组装检查点损坏。')
            if (state['project_id'],state['document_id'])!=(str(parent['project_id']),str(parent['document_id'])):
                raise ValueError('组装检查点归属不符。')
            if not 1<=state['next_page']<=document['page_count']+1:
                raise AssemblyStale('材料页数或组装范围已改变。')
            if state['closed'] and state['next_page']!=document['page_count']+1:
                raise AssemblyStale('旧封口不覆盖当前材料。')
        return parent,document,checkpoint,reset

    def _summary(self,context):
        parent,document,checkpoint,reset=context
        with self.database.connect() as conn:
            active=conn.execute('''SELECT count(*) AS n FROM dd_agent.stage_jobs
                WHERE job_id=%s AND run_epoch=%s AND stage='embedding' AND scope ? 'parent_build'
                AND state IN ('pending','running')''',(parent['id'],parent['run_epoch'])).fetchone()['n']
            pending=self.bridge._next(conn,parent,self.version,payload=False) is not None
            stale=conn.execute('''SELECT count(*) AS n FROM dd_agent.assembly_handoffs
                WHERE job_id=%s AND run_epoch=%s AND index_version=%s AND document_id=%s
                AND assembly_policy=%s AND receipt_generation>=%s AND state='stale'
                AND dd_agent.assembly_receipt_current(document_id,assembly_policy,receipt_generation) ''',
                (parent['id'],parent['run_epoch'],self.version,parent['document_id'],VERSION,reset)).fetchone()['n']
        state=checkpoint['state'] or {}
        return dict(enabled=True,saved_pages=state.get('next_page',1)-1,generation=checkpoint['generation'],
            total_pages=document['page_count'],closed=state.get('closed',False),active_work=active,
            handoff_pending=pending,covered_pages=0,gap_pages=0,failed=stale,terminal=False,waiting='',worked=False)

    def _prefix(self,context,vision_policy,reading_done):
        parent,document,checkpoint,_=context
        start=(checkpoint['state'] or {}).get('next_page',1)
        return resolved_prefix(self.database,parent,document,start,self.window,vision_policy,reading_done=reading_done)

    def _coverage(self,context):
        """只在封口／交接／工作包排空后执行，SQL 返回固定的标量结果。

        input_dependencies 包含空页和缺口；不能只查贡献了向量的页面。
        历史元数据缺失也不能据 parent 文本推断整卷已覆盖。
        """
        parent,document,_,reset=context
        with self.database.connect() as conn:
            return conn.execute('''WITH batches AS (
                SELECT b.result FROM dd_agent.assembly_batches b JOIN dd_agent.assembly_live_receipts o
                  ON o.document_id=b.document_id AND o.assembly_policy=b.policy_version AND o.receipt_generation=b.generation
                WHERE b.document_id=%s AND b.policy_version=%s AND b.generation>=%s AND NOT o.is_reset
            ), deps AS (SELECT value AS dep FROM batches
                CROSS JOIN LATERAL jsonb_array_elements(coalesce(result->'input_dependencies','[]'::jsonb))),
            checked AS (SELECT dep,(dep->>'page_number')::int AS number,
                coalesce(dep->>'project_id'=%s AND dep->>'document_id'=%s AND CASE
                WHEN dep->>'readiness'='missing' THEN e.page_number IS NULL AND dep->>'source_revision'=%s
                ELSE e.page_number IS NOT NULL AND e.source_revision=dep->>'source_revision'
                  AND e.verification IS NOT DISTINCT FROM dep->>'quality'
                  AND CASE dep->>'readiness' WHEN 'ready' THEN e.status='succeeded'
                      WHEN 'failed' THEN e.status='failed' WHEN 'pending' THEN true ELSE false END END,false) AS valid
                FROM deps LEFT JOIN dd_agent.effective_document_pages e
                  ON e.document_id=%s AND e.page_number=(dep->>'page_number')::int)
            SELECT count(DISTINCT number) AS covered_pages,
                count(*) FILTER(WHERE dep->>'readiness'<>'ready') AS gap_pages,
                coalesce(bool_or(NOT valid OR number<1 OR number>%s),false) AS stale,
                (SELECT count(*) FROM batches WHERE NOT (result ? 'input_dependencies')) AS legacy_missing FROM checked''',
                (parent['document_id'],VERSION,reset,str(parent['project_id']),str(parent['document_id']),
                 'missing:'+document['pipeline_version'],parent['document_id'],document['page_count'])).fetchone()

    def _result(self,context,*,waiting='',worked=False,blocked=False):
        result=self._summary(context)
        if result['failed']:waiting='stale_sources';blocked=True
        key=(str(context[0]['id']),context[0]['run_epoch'],context[3],context[2]['generation'])
        if blocked:self.halt=(key,waiting)
        if self.halt and self.halt[0]==key:
            result.update(terminal=True,failed=max(1,result['failed']),waiting=self.halt[1],worked=worked)
        else:
            result.update(waiting=waiting,worked=worked)
            if result['closed'] and not result['handoff_pending'] and not result['active_work']:
                coverage=self._coverage(context)
                result.update(covered_pages=coverage['covered_pages'],gap_pages=coverage['gap_pages'])
                if coverage['stale']:result.update(failed=1,waiting='stale_sources')
                elif coverage['legacy_missing']:result.update(failed=1,waiting='legacy_receipt_unverified')
                elif coverage['covered_pages']!=context[1]['page_count']:result.update(failed=1,waiting='incomplete_coverage')
                else:result['failed']=coverage['gap_pages']
                result['terminal']=True
        self.progress=result
        return dict(result)

    def status(self,job_id,*,expected_epoch):
        """暂停时也可恢复持久进度；此入口不组装、不登记、不计算模型。"""
        if self.version is None:self.version=self.index.version()
        try:return self._result(self._context(job_id,expected_epoch))
        except AssemblyStale:
            self.progress=dict(self.progress,terminal=True,failed=1,waiting='stale_sources',worked=False)
            return dict(self.progress)

    @timed('assembly_ingestion.step')
    def step(self,job_id,*,expected_epoch,vision_policy,reading_done=False,flush=False,checkpoint=None):
        if type(expected_epoch) is not int or expected_epoch<1 or not isinstance(vision_policy,str) or not vision_policy:
            raise ValueError('组装需要当前执行代次和视觉策略。')
        if type(reading_done) is not bool or type(flush) is not bool:raise ValueError('冲刷与读取结束必须明确。')
        version=self.index.version()
        if self.version is None:self.version=version
        try:context=self._context(job_id,expected_epoch,active=True)
        except AssemblyStale:return self.status(job_id,expected_epoch=expected_epoch)
        key=(str(context[0]['id']),expected_epoch,context[3],context[2]['generation'])
        if self.halt and self.halt[0]!=key:self.halt=None
        if version!=self.version:return self._result(context,waiting='index_version_changed',blocked=True)
        current=self._result(context)
        if current['terminal']:return current
        worked=False

        def handoff(context):
            summary=self._summary(context)
            free=min(4,self.max_pending-summary['active_work'])
            if not summary['handoff_pending'] or free<=0:return False
            result=self.bridge.reconcile(job_id,expected_epoch=expected_epoch,limit=free,checkpoint=checkpoint)
            return result['processed']>0

        if checkpoint:checkpoint('assembly',max(1,current['saved_pages']))
        worked=handoff(context)
        context=self._context(job_id,expected_epoch,active=True)
        current=self._result(context,worked=worked)
        if current['terminal']:return current
        if current['active_work']>=self.max_pending:return self._result(context,waiting='backpressure',worked=worked)
        if current['handoff_pending']:return self._result(context,waiting='handoff',worked=worked)
        if current['closed']:return current
        numbers,ready,reason=self._prefix(context,vision_policy,reading_done)
        if reason in ('assembly_input_too_large','vision_artifact_missing'):
            return self._result(context,waiting=reason,blocked=True,worked=worked)
        total=context[1]['page_count'];start=(context[2]['state'] or {}).get('next_page',1)
        closing=reading_done and start==total+1
        if not numbers and not closing:return self._result(context,waiting=reason or 'vision',worked=worked)
        if numbers and len(numbers)<self.window and not (flush or reading_done or reason=='character_budget'):
            if self.ready_since is None:self.ready_since=monotonic()
            if monotonic()-self.ready_since<self.index.settings.stage_flush_seconds:
                return self._result(context,waiting='window',worked=worked)
        final=reading_done and (closing or numbers[-1]==total)
        pages=self.store.snapshots(context[0]['project_id'],context[0]['document_id'],numbers,ready_pages=ready) if numbers else []
        task=None
        try:
            plan=self.store.prepare(context[0]['project_id'],context[0]['document_id'],pages,
                expected_generation=context[2]['generation'],final=final)
            task=self.work.register(job_id,plan,expected_epoch=expected_epoch)
            claim=self.stages.claim(job_id,'assembly',task_id=task['id'])
            if claim is None:
                with self.database.connect() as conn:
                    state=conn.execute('SELECT state FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()['state']
                return self._result(context,waiting='assembly_claimed',blocked=state in ('failed','cancelled'),worked=worked)
            self.work.run(claim['id'],claim['owner_token'],plan,checkpoint=checkpoint)
        except (ProcessingPaused,ProcessingStopped,StagePaused):raise
        except AssemblyConflict:
            return self._result(self._context(job_id,expected_epoch),waiting='assembly_conflict',worked=worked)
        except (AssemblyStale,StageSuperseded):
            return self._result(self._context(job_id,expected_epoch),waiting='stale_sources',blocked=True,worked=worked)
        except Exception:
            # AssemblyWork 已回滚本次提交，并将领取归还或用尽失败预算。
            # 重用相同持久任务的 failures，不因重新准备窗口而重置重试次数。
            state=None
            if task is not None:
                with self.database.connect() as conn:
                    row=conn.execute('SELECT state FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()
                    state=row['state'] if row else None
            retry=state=='pending'
            return self._result(self._context(job_id,expected_epoch),
                waiting='assembly_retry' if retry else 'assembly_failed',blocked=not retry,worked=worked)
        # 保存后先刷新可恢复进度，再响应暂停；中断不能显示成零页。
        context=self._context(job_id,expected_epoch)
        self._result(context,worked=True)
        self.ready_since=None
        if checkpoint:checkpoint('assembly_saved',numbers[-1] if numbers else total)
        handoff(context)
        return self._result(self._context(job_id,expected_epoch),worked=True)
