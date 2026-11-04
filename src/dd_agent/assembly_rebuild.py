"""局部重算的持久准备：不修改当前收据、检查点、交接或向量。

先扫描不可变基线的输入元数据，再逐窗口纯组装。完整 tail checksum 再次
一致才停止传播；分散变化分别形成区域，不重算夹在中间的未变窗口。
所有待替换结果留在 parts，未来发布器须再次核对基线和来源后原子接纳。
"""
from uuid import uuid4

from psycopg.types.json import Jsonb

from .assembly import VERSION,MAX_STATE_BYTES,assemble,canonical,digest
from .assembly_store import AssemblyStore,AssemblyStale,AssemblyConflict
from .assembly_work import AssemblyWork
from .assembly_readiness import resolved_prefix
from .control import ProcessingPaused,ProcessingStopped
from .stage_publication import StagePaused,StageSuperseded
from .performance import timed


def check_state(state,project,document):
    if state is None:return
    if (len(canonical(state).encode('utf-8'))>MAX_STATE_BYTES or state.get('version')!=VERSION
        or state.get('checksum')!=digest({k:v for k,v in state.items() if k!='checksum'})
        or (state.get('project_id'),state.get('document_id'))!=(str(project),str(document))):
        raise ValueError('重建基线尾段损坏或归属不符。')


class AssemblyRebuild:
    def __init__(self,stages):
        self.stages,self.database=stages,stages.database
        self.store=AssemblyStore(self.database)

    def _parent(self,conn,job_id,epoch,*,exclusive=True):
        if type(epoch) is not int or epoch<1:raise ValueError('执行代次必须为正整数。')
        parent=self.stages._parent(conn,job_id,exclusive=exclusive)
        AssemblyWork._active(parent,epoch)
        return parent

    @staticmethod
    def _sources_current(conn,plan):
        return conn.execute('''SELECT NOT EXISTS (
            SELECT 1 FROM dd_agent.assembly_rebuild_sources s
            LEFT JOIN dd_agent.effective_document_pages e ON e.document_id=%s AND e.page_number=s.page_number
            WHERE s.plan_id=%s AND NOT CASE WHEN s.status='missing'
                THEN e.page_number IS NULL AND s.revision=%s
                ELSE e.page_number IS NOT NULL AND e.status=s.status AND e.source_revision=s.revision
                    AND e.verification IS NOT DISTINCT FROM s.quality END) AS valid''',
            (plan['document_id'],plan['id'],'missing:'+plan['reading_version'])).fetchone()['valid']

    @staticmethod
    def _history_current(conn,plan):
        return conn.execute('''SELECT NOT EXISTS (
            SELECT 1 FROM dd_agent.assembly_rebuild_members m JOIN dd_agent.assembly_batches b
                ON b.document_id=m.document_id AND b.policy_version=m.policy_version AND b.generation=m.receipt_generation
            WHERE m.plan_id=%s AND (b.result->'input_dependencies' IS DISTINCT FROM m.input_dependencies
                OR b.result->'state'->>'checksum' IS DISTINCT FROM m.state_checksum
                OR (b.result->'state'->>'next_page')::int IS DISTINCT FROM m.next_page
                OR (b.result->'state'->>'closed')::boolean IS DISTINCT FROM m.closed)) AS valid''',(plan['id'],)).fetchone()['valid']

    def _guard(self,conn,job_id,plan_id,epoch,*,resume=False,exclusive=True):
        # 每个耐久写入前重新核对，而非相信调用方刚读过的进度。
        # resume 只允许绑定父任务的新代次；它不能挽救已经变化的来源／基线。
        parent=self._parent(conn,job_id,epoch,exclusive=exclusive)
        document=self.store._document(conn,parent['project_id'],parent['document_id'],lock=True)
        plan=conn.execute('SELECT * FROM dd_agent.assembly_rebuild_plans WHERE id=%s AND job_id=%s FOR UPDATE',
            (plan_id,job_id)).fetchone()
        if plan is None:raise ValueError('重建清单不存在或不属于当前任务。')
        if not resume and plan['run_epoch']!=epoch:raise StageSuperseded('重建清单的执行代次已失效。')
        if plan['status']=='adopted':
            # 只恢复历史确认的访问代次，绝不再次切换或复活旧谱系。
            if resume:
                conn.execute('UPDATE dd_agent.assembly_rebuild_plans SET run_epoch=%s,updated_at=clock_timestamp() WHERE id=%s',(epoch,plan_id))
                plan=dict(plan,run_epoch=epoch)
            return parent,document,plan
        checkpoint=self.store._checkpoint(conn,parent['document_id'])
        error=''
        if (checkpoint['generation'],document['pipeline_version'],document['page_count'])!=(
            plan['base_generation'],plan['reading_version'],plan['page_count']):error='baseline_changed'
        elif not self._sources_current(conn,plan):error='source_changed'
        elif not self._history_current(conn,plan):error='history_changed'
        if error:
            conn.execute("UPDATE dd_agent.assembly_rebuild_plans SET status='superseded',error_code=%s,updated_at=clock_timestamp() WHERE id=%s",(error,plan_id))
            plan=dict(plan,status='superseded',error_code=error)
        if resume:
            conn.execute('UPDATE dd_agent.assembly_rebuild_plans SET run_epoch=%s,updated_at=clock_timestamp() WHERE id=%s',(epoch,plan_id))
            plan=dict(plan,run_epoch=epoch)
        return parent,document,plan

    @staticmethod
    def _summary(conn,plan_id,*,waiting=''):
        row=conn.execute('''SELECT p.id,p.status,p.error_code,p.scan_cursor,p.prepared_cursor,p.base_generation,
            (SELECT count(*) FROM dd_agent.assembly_rebuild_members m WHERE m.plan_id=p.id) AS receipts,
            (SELECT count(*) FROM dd_agent.assembly_rebuild_members m WHERE m.plan_id=p.id AND dirty) AS dirty_receipts,
            (SELECT count(*) FROM dd_agent.assembly_rebuild_sources s WHERE s.plan_id=p.id) AS source_pages,
            (SELECT count(*) FROM dd_agent.assembly_rebuild_parts r WHERE r.plan_id=p.id) AS recomputed_receipts,
            (SELECT count(DISTINCT region) FROM dd_agent.assembly_rebuild_parts r WHERE r.plan_id=p.id) AS regions
            FROM dd_agent.assembly_rebuild_plans p WHERE p.id=%s''',(plan_id,)).fetchone()
        return dict(row,id=str(row['id']),kept_receipts=row['receipts']-row['recomputed_receipts'],waiting=waiting)

    @timed('assembly_rebuild.start')
    def start(self,job_id,*,expected_epoch,vision_policy):
        if not isinstance(vision_policy,str) or not 1<=len(vision_policy)<=256:raise ValueError('视觉策略必须明确。')
        with self.database.connect() as conn:
            parent=self._parent(conn,job_id,expected_epoch,exclusive=False)
            document=self.store._document(conn,parent['project_id'],parent['document_id'])
            checkpoint=self.store._checkpoint(conn,parent['document_id'])
            reset=conn.execute('''SELECT coalesce(max(receipt_generation),0) AS n FROM dd_agent.assembly_receipt_outbox
                WHERE document_id=%s AND assembly_policy=%s AND is_reset''',(parent['document_id'],VERSION)).fetchone()['n']
        state=checkpoint['state']
        check_state(state,parent['project_id'],parent['document_id'])
        if not state or not state['closed'] or state['next_page']!=document['page_count']+1:
            raise ValueError('局部重建准备需要已封口且页数一致的基线。')
        identity=uuid4()
        with self.database.connect() as conn:
            self._parent(conn,job_id,expected_epoch)
            current=self.store._document(conn,parent['project_id'],parent['document_id'],lock=True)
            if (self.store._checkpoint(conn,parent['document_id'])['generation'],current['pipeline_version'],current['page_count'])!=(
                checkpoint['generation'],document['pipeline_version'],document['page_count']):raise AssemblyConflict('基线已推进，请重新建立清单。')
            conn.execute('''INSERT INTO dd_agent.assembly_rebuild_plans
                (id,job_id,project_id,document_id,run_epoch,policy_version,vision_policy,base_generation,
                 reset_generation,reading_version,page_count) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (identity,job_id,parent['project_id'],parent['document_id'],expected_epoch,VERSION,vision_policy,
                 checkpoint['generation'],reset,document['pipeline_version'],document['page_count']))
            return self._summary(conn,identity)

    @staticmethod
    def _snapshots(conn,document,reading_version,numbers):
        rows={r['page_number']:r for r in conn.execute('''SELECT page_number,source_revision,status,verification
            FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=ANY(%s)''',(document,list(numbers))).fetchall()}
        return {n:dict(page_number=n,revision=rows[n]['source_revision'],status=rows[n]['status'],quality=rows[n]['verification'])
            if n in rows else dict(page_number=n,revision='missing:'+reading_version,status='missing',quality='unverified') for n in numbers}

    @staticmethod
    def _matches(dep,current):
        status=dep['readiness']
        return (dep['source_revision']==current['revision'] and dep['quality']==current['quality']
            and (status=='pending' and current['status']!='missing' or status=='missing' and current['status']=='missing'
                or status=='ready' and current['status']=='succeeded' or status=='failed' and current['status']=='failed'))

    @staticmethod
    def _meta(row):
        return dict(inputs=row['inputs'],state_checksum=row['state_checksum'],next_page=row['next_page'],closed=row['closed'])

    @timed('assembly_rebuild.scan')
    def scan(self,job_id,plan_id,*,expected_epoch,limit=4):
        """只扫输入修订元数据；两段事务之间用 scan_cursor 防止重复登记。"""
        if type(limit) is not int or not 1<=limit<=20:raise ValueError('每轮扫描限制为 1–20 份收据。')
        with self.database.connect() as conn:
            parent,document,plan=self._guard(conn,job_id,plan_id,expected_epoch)
            if plan['status']!='scanning':return self._summary(conn,plan_id)
            rows=conn.execute('''SELECT b.generation,o.position,result->'input_dependencies' AS inputs,
                result->'state'->>'checksum' AS state_checksum,(result->'state'->>'next_page')::int AS next_page,
                (result->'state'->>'closed')::boolean AS closed FROM dd_agent.assembly_batches b
                JOIN dd_agent.assembly_live_receipts o ON o.document_id=b.document_id AND o.assembly_policy=b.policy_version AND o.receipt_generation=b.generation
                WHERE b.document_id=%s AND b.policy_version=%s AND o.position>%s AND b.generation<=%s
                AND b.generation>%s AND NOT o.is_reset ORDER BY o.position LIMIT %s''',
                (plan['document_id'],VERSION,plan['scan_cursor'],plan['base_generation'],plan['reset_generation'],limit)).fetchall()
        # 元数据检查和摘要在持锁事务外；单份至多 20 输入页，单轮至多 400 页。
        error='';numbers=set()
        for row in rows:
            deps=row['inputs']
            if deps is None:error='legacy_receipt_unverified';break
            if (not isinstance(deps,list) or len(deps)>20 or not isinstance(row['state_checksum'],str)
                or len(row['state_checksum'])!=64 or type(row['next_page']) is not int or type(row['closed']) is not bool):
                error='invalid_history';break
            for dep in deps:
                if (not isinstance(dep,dict) or (dep.get('project_id'),dep.get('document_id'))!=(str(parent['project_id']),str(parent['document_id']))
                    or type(dep.get('page_number')) is not int or not 1<=dep['page_number']<=plan['page_count']
                    or dep.get('readiness') not in ('ready','missing','pending','failed')
                    or not isinstance(dep.get('source_revision'),str) or not isinstance(dep.get('quality'),str)):
                    error='invalid_history';break
                numbers.add(dep['page_number'])
            row['meta_hash']=digest(self._meta(row))
        with self.database.connect() as conn:
            parent,document,current=self._guard(conn,job_id,plan_id,expected_epoch)
            if current['status']!='scanning' or current['scan_cursor']!=plan['scan_cursor']:return self._summary(conn,plan_id)
            if error:
                conn.execute("UPDATE dd_agent.assembly_rebuild_plans SET status='blocked',error_code=%s WHERE id=%s",(error,plan_id))
                return self._summary(conn,plan_id)
            sources=self._snapshots(conn,plan['document_id'],plan['reading_version'],numbers)
            previous=conn.execute('''SELECT next_page,closed FROM dd_agent.assembly_rebuild_members
                WHERE plan_id=%s ORDER BY position DESC LIMIT 1''',(plan_id,)).fetchone()
            expected=previous['next_page'] if previous else 1;closed=previous['closed'] if previous else False
            for row in rows:
                inputs=[d['page_number'] for d in row['inputs']]
                if closed or inputs!=list(range(expected,row['next_page'])):
                    error='incomplete_coverage';break
                expected=row['next_page'];closed=row['closed']
            if error:
                conn.execute("UPDATE dd_agent.assembly_rebuild_plans SET status='blocked',error_code=%s WHERE id=%s",(error,plan_id))
                return self._summary(conn,plan_id)
            for n,source in sources.items():
                conn.execute('''INSERT INTO dd_agent.assembly_rebuild_sources(plan_id,page_number,revision,status,quality)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (plan_id,n,source['revision'],source['status'],source['quality']))
            for row in rows:
                dirty=any(not self._matches(d,sources[d['page_number']]) for d in row['inputs'])
                conn.execute('''INSERT INTO dd_agent.assembly_rebuild_members
                    (plan_id,document_id,policy_version,receipt_generation,position,input_dependencies,meta_hash,state_checksum,next_page,closed,dirty)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (plan_id,plan['document_id'],VERSION,row['generation'],row['position'],Jsonb(row['inputs']),row['meta_hash'],
                     row['state_checksum'],row['next_page'],row['closed'],dirty))
            cursor=rows[-1]['position'] if rows else plan['scan_cursor']
            more=conn.execute('''SELECT 1 FROM dd_agent.assembly_live_receipts WHERE document_id=%s AND assembly_policy=%s
                AND position>%s AND receipt_generation<=%s AND NOT is_reset LIMIT 1''',(plan['document_id'],VERSION,cursor,plan['base_generation'])).fetchone()
            status='scanning'
            if not more:
                summary=self._summary(conn,plan_id)
                if expected!=plan['page_count']+1 or not closed or summary['source_pages']!=plan['page_count']:
                    status='blocked';error='incomplete_coverage'
                else:status='ready' if summary['dirty_receipts'] else 'clean'
            conn.execute('''UPDATE dd_agent.assembly_rebuild_plans SET scan_cursor=%s,status=%s,error_code=%s,
                updated_at=clock_timestamp() WHERE id=%s''',(cursor,status,error,plan_id))
            return self._summary(conn,plan_id)

    def resume(self,job_id,plan_id,*,expected_epoch):
        with self.database.connect() as conn:
            self._guard(conn,job_id,plan_id,expected_epoch,resume=True)
            return self._summary(conn,plan_id)

    def _blocked(self,job_id,plan_id,epoch,code):
        with self.database.connect() as conn:
            _,_,plan=self._guard(conn,job_id,plan_id,epoch)
            if plan['status']!='superseded':
                conn.execute("UPDATE dd_agent.assembly_rebuild_plans SET status='blocked',error_code=%s WHERE id=%s",(code,plan_id))
            return self._summary(conn,plan_id)

    @staticmethod
    def _save_part(conn,plan,member,region,before,result,seal,task_id,converged,reason):
        conn.execute('''INSERT INTO dd_agent.assembly_rebuild_parts
            (plan_id,receipt_generation,region,before_checksum,converged,stop_reason,result,seal,stage_job_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (plan['id'],member['receipt_generation'],region,before.get('checksum') if before else None,
             converged,reason,Jsonb(result),seal,task_id))
        more=conn.execute('''SELECT 1 FROM dd_agent.assembly_rebuild_members
            WHERE plan_id=%s AND position>%s AND dirty LIMIT 1''',(plan['id'],member['position'])).fetchone()
        status='prepared' if converged and not more else 'preparing'
        conn.execute('''UPDATE dd_agent.assembly_rebuild_plans SET prepared_cursor=%s,status=%s,
            updated_at=clock_timestamp() WHERE id=%s''',(member['position'],status,plan['id']))

    @timed('assembly_rebuild.prepare')
    def prepare_next(self,job_id,plan_id,*,expected_epoch,checkpoint=None):
        """每次准备一个窗口。保持旧活动谱系，直至后续发布器接纳清单。

        上一结果未收敛时必须顺序传播，即使下一窗口自身未脏；收敛后才可
        跳到下一个脏窗口，并从其原前驱恢复上下文。不能只比较父块数量或
        文本，因为相同文字可能来自不同页修订，完整尾段 checksum 才可靠。
        """
        with self.database.connect() as conn:
            parent,document,plan=self._guard(conn,job_id,plan_id,expected_epoch)
            if plan['status'] not in ('ready','preparing'):return self._summary(conn,plan_id)
            last=conn.execute('''SELECT p.receipt_generation,p.region,p.converged,p.result->'state' AS state
                FROM dd_agent.assembly_rebuild_parts p JOIN dd_agent.assembly_rebuild_members m USING(plan_id,receipt_generation)
                WHERE p.plan_id=%s ORDER BY m.position DESC LIMIT 1''',(plan_id,)).fetchone()
            propagating=last is not None and not last['converged']
            member=conn.execute('''SELECT * FROM dd_agent.assembly_rebuild_members WHERE plan_id=%s
                AND position>%s AND (%s OR dirty) ORDER BY position LIMIT 1''',
                (plan_id,plan['prepared_cursor'],propagating)).fetchone()
            if member is None:raise ValueError('重建区域缺少后续基线窗口。')
            old=conn.execute('''SELECT result->'state' AS state FROM dd_agent.assembly_batches
                WHERE document_id=%s AND policy_version=%s AND generation=%s''',
                (plan['document_id'],VERSION,member['receipt_generation'])).fetchone()['state']
            if propagating:before=last['state'];region=last['region']
            else:
                previous=conn.execute('''SELECT b.result->'state' AS state FROM dd_agent.assembly_rebuild_members m
                    JOIN dd_agent.assembly_batches b ON b.document_id=m.document_id AND b.policy_version=m.policy_version
                        AND b.generation=m.receipt_generation
                    WHERE m.plan_id=%s AND m.position<%s ORDER BY m.position DESC LIMIT 1''',
                    (plan_id,member['position'])).fetchone()
                before=previous['state'] if previous else None;region=last['region']+1 if last else 1
        # 正文、组装与摘要均在保护事务外；只能复用冻结来源，不能替代尚未完成的视觉。
        check_state(before,parent['project_id'],parent['document_id']);check_state(old,parent['project_id'],parent['document_id'])
        numbers=[d['page_number'] for d in member['input_dependencies']]
        if numbers:
            selected,ready,reason=resolved_prefix(self.database,parent,document,numbers[0],len(numbers),plan['vision_policy'],reading_done=True)
            if selected!=numbers:
                if reason in ('assembly_input_too_large','character_budget','vision_artifact_missing'):return self._blocked(job_id,plan_id,expected_epoch,reason)
                with self.database.connect() as conn:
                    self._guard(conn,job_id,plan_id,expected_epoch)
                    return self._summary(conn,plan_id,waiting=reason or 'vision')
            pages=self.store.snapshots(parent['project_id'],parent['document_id'],numbers,ready_pages=ready)
        else:pages=[]
        try:
            result=dict(assemble(pages,state=before,final=member['closed']),input_dependencies=[p.dependency() for p in pages])
            if len(canonical(result).encode('utf-8'))>8*1024*1024:raise ValueError('重算结果超过容量。')
        except ValueError:return self._blocked(job_id,plan_id,expected_epoch,'assembly_budget_or_state')
        seal=digest(result)
        deps=self.store._dependencies(result)
        for d in self.store._dependencies(dict(state=before)):
            if d not in deps:deps.append(d)
        stage_deps=[dict(page_number=d['page_number'],view='effective',revision=d['source_revision'],
            readiness=d['readiness'],quality=d['quality']) for d in deps]
        scope=dict(pages=sorted({d['page_number'] for d in deps}),assembly_rebuild=str(plan_id),receipt=member['receipt_generation'],region=region)
        revision=digest(dict(result=seal,before=before.get('checksum') if before else None))
        if checkpoint:checkpoint('assembly_rebuild',numbers[0] if numbers else document['page_count'])
        with self.database.connect() as conn:
            _,_,current=self._guard(conn,job_id,plan_id,expected_epoch)
            if current['status'] not in ('ready','preparing') or current['prepared_cursor']!=plan['prepared_cursor']:
                return self._summary(conn,plan_id)
            task=self.stages.ensure(job_id,'assembly',scope,source_revision=revision,policy_version='rebuild:'+VERSION,
                dependencies=stage_deps,reuse=False,conn=conn)
        claim=self.stages.claim(job_id,'assembly',task_id=task['id'])
        if claim is None:
            with self.database.connect() as conn:
                self._guard(conn,job_id,plan_id,expected_epoch)
                # 没领到也可能是重试预算耗尽，不能把终态误报为资源等待。
                terminal=conn.execute('SELECT state,error_code FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()
                if terminal['state'] in ('failed','cancelled'):
                    conn.execute("UPDATE dd_agent.assembly_rebuild_plans SET status='blocked',error_code=%s,updated_at=clock_timestamp() WHERE id=%s AND status IN ('ready','preparing')",
                        (terminal['error_code'] or 'rebuild_stage_failed',plan_id))
                    return self._summary(conn,plan_id)
                return self._summary(conn,plan_id,waiting='claim')
        converged=result['state']['checksum']==old['checksum'] or member['closed']
        reason='stable' if result['state']['checksum']==old['checksum'] else 'document_end' if member['closed'] else 'propagate'
        try:
            if checkpoint:checkpoint('assembly_rebuild',numbers[0] if numbers else document['page_count'])
            with self.stages.publish_scope(claim['id'],claim['owner_token']) as conn:
                # 已持 parent SHARE，不能升级为 UPDATE，否则另一领取可能造成锁环。
                parent,document,current=self._guard(conn,job_id,plan_id,expected_epoch,exclusive=False)
                if current['status'] not in ('ready','preparing') or current['prepared_cursor']!=plan['prepared_cursor']:
                    raise AssemblyConflict('重算清单已推进。')
                self.store._validate(conn,parent['project_id'],parent['document_id'],document,deps,pages)
                self._save_part(conn,plan,member,region,before,result,seal,claim['id'],converged,reason)
        except (ProcessingPaused,ProcessingStopped,StagePaused):
            self.stages.park(claim['id'],claim['owner_token']);raise
        except (AssemblyStale,StageSuperseded,AssemblyConflict):
            self.stages.fail(claim['id'],claim['owner_token'],category='stale',error_code='rebuild_source_changed')
        except Exception:
            self.stages.fail(claim['id'],claim['owner_token'],category='transient',error_code='rebuild_save_failed',retry_after=.2)
            with self.database.connect() as conn:
                self._guard(conn,job_id,plan_id,expected_epoch)
                return self._summary(conn,plan_id,waiting='retry')
        with self.database.connect() as conn:
            self._guard(conn,job_id,plan_id,expected_epoch)
            return self._summary(conn,plan_id)

    def manifest(self,job_id,plan_id,*,expected_epoch,after_generation=0,limit=20):
        if type(limit) is not int or not 1<=limit<=100 or type(after_generation) is not int or after_generation<0:
            raise ValueError('清单分页范围无效。')
        with self.database.connect() as conn:
            _,_,plan=self._guard(conn,job_id,plan_id,expected_epoch)
            if plan['status'] not in ('prepared','clean'):raise AssemblyStale('清单尚未完成或已过期。')
            rows=conn.execute('''SELECT m.receipt_generation,m.position,CASE WHEN p.plan_id IS NULL THEN 'keep' ELSE 'replace' END AS action,p.region
                FROM dd_agent.assembly_rebuild_members m LEFT JOIN dd_agent.assembly_rebuild_parts p
                ON p.plan_id=m.plan_id AND p.receipt_generation=m.receipt_generation
                WHERE m.plan_id=%s AND m.position>%s ORDER BY m.position LIMIT %s''',
                (plan_id,after_generation,limit+1)).fetchall()
        more=len(rows)>limit;rows=rows[:limit]
        return dict(items=rows,has_more=more,next_cursor=rows[-1]['position'] if rows else None)

    def replacement(self,job_id,plan_id,receipt_generation,*,expected_epoch):
        with self.database.connect() as conn:
            _,_,plan=self._guard(conn,job_id,plan_id,expected_epoch)
            if plan['status'] not in ('prepared','clean'):raise AssemblyStale('清单尚未完成或已过期。')
            row=conn.execute('SELECT result,seal FROM dd_agent.assembly_rebuild_parts WHERE plan_id=%s AND receipt_generation=%s',
                (plan_id,receipt_generation)).fetchone()
        if not row or digest(row['result'])!=row['seal']:raise ValueError('重算结果不存在或校验失败。')
        return row['result']
