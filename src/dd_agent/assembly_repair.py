"""可恢复的一步式重建编排；每次最多一个窗口／一个校验，不运行模型。

来源 stamp 只聚合版本／质量元数据。就绪探测期间变化只重新探测；冻结后
计划过期才消耗计划预算。动作所有权不按年龄回收，沿用显式 resume 的 epoch。
"""
from uuid import uuid4
from time import monotonic

from .assembly_rebuild import AssemblyRebuild
from .assembly_adoption import AssemblyAdoption
from .assembly_readiness import resolved_prefix
from .assembly_work import AssemblyWork
from .control import ProcessingPaused,ProcessingStopped
from .stage_publication import StagePaused
from .performance import timed


class AssemblyRepair:
    def __init__(self,stages):
        self.stages,self.database=stages,stages.database
        self.rebuild=AssemblyRebuild(stages);self.adoption=AssemblyAdoption(stages)
        self._deferred=None

    def _remember(self,key,result):
        # 非关键退避可以丢失：重启至多多做一次探测；游标和预算仍全在库内。
        if not result['terminal'] and result['waiting'] in ('vision','reading'):
            self._deferred=(key,monotonic()+.5,result['phase'],result['plan_id'],result['checked_pages'],result['waiting'])
        else:self._deferred=None
        return result

    def _context(self,conn,job,epoch,*,active=False,exclusive=True):
        parent=self.stages._parent(conn,job,exclusive=exclusive)
        if type(epoch) is not int or epoch<1:raise ValueError('执行代次必须为正整数。')
        if parent['run_epoch']!=epoch:raise ProcessingStopped()
        if active:AssemblyWork._active(parent,epoch)
        document=self.rebuild.store._document(conn,parent['project_id'],parent['document_id'],lock=exclusive)
        cp=self.rebuild.store._checkpoint(conn,parent['document_id'])
        return parent,document,cp

    @staticmethod
    def _stamp(conn,document):
        # 包含失败页和最新机器修订；不使用 outbox 序号冒充提交顺序。
        # SQL 扫描当前元数据并返回一个标量，成本仍随物理页数增长。
        return conn.execute('''SELECT md5(coalesce(string_agg(page_number::text||':'||source_revision||':'||status||':'||verification,
            ',' ORDER BY page_number),'')) AS stamp FROM dd_agent.effective_document_pages WHERE document_id=%s''',
            (document,)).fetchone()['stamp']

    @staticmethod
    def _summary(conn,job,*,waiting='',worked=False):
        row=conn.execute('''SELECT phase,plan_id,attempts,failures,cursor,page_count,adopted_generation,error_code
            FROM dd_agent.assembly_repair_runs WHERE job_id=%s''',(job,)).fetchone()
        if row is None:return None
        return dict(phase=row['phase'],plan_id=str(row['plan_id']) if row['plan_id'] else None,
            attempts=row['attempts'],failures=row['failures'],checked_pages=min(row['page_count'],row['cursor']-1),
            generation=row['adopted_generation'],error_code=row['error_code'],terminal=row['phase'] in ('done','blocked'),
            failed=int(row['phase']=='blocked'),waiting=waiting,worked=worked)

    def status(self,job_id,*,expected_epoch):
        with self.database.connect() as conn:
            self._context(conn,job_id,expected_epoch,exclusive=False)
            return self._summary(conn,job_id)

    @staticmethod
    def _block(conn,job,code):
        conn.execute("UPDATE dd_agent.assembly_repair_runs SET phase='blocked',error_code=%s,owner_token=NULL,updated_at=clock_timestamp() WHERE job_id=%s",(code,job))

    @staticmethod
    def _abandon(conn,plan):
        if plan:
            conn.execute("UPDATE dd_agent.assembly_rebuild_plans SET status='superseded',error_code='source_changed',updated_at=clock_timestamp() WHERE id=%s AND status<>'adopted'",(plan,))

    def _restart_probe(self,conn,row,stamp):
        self._abandon(conn,row['plan_id'])
        if row['attempts']>=3:
            self._block(conn,row['job_id'],'repair_replan_exhausted')
        else:
            conn.execute('''UPDATE dd_agent.assembly_repair_runs SET phase='probing',cursor=1,plan_id=NULL,
                source_stamp=%s,owner_token=NULL,error_code='',updated_at=clock_timestamp() WHERE job_id=%s''',(stamp,row['job_id']))

    def _acquire(self,job,epoch,policy,reading_done):
        with self.database.connect() as conn:
            parent,document,cp=self._context(conn,job,epoch,active=True)
            stamp=self._stamp(conn,parent['document_id'])
            conn.execute('''INSERT INTO dd_agent.assembly_repair_runs(job_id,project_id,document_id,run_epoch,vision_policy,
                base_generation,reading_version,page_count,source_stamp) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (job,parent['project_id'],parent['document_id'],epoch,policy,cp['generation'],document['pipeline_version'],document['page_count'],stamp))
            row=conn.execute('SELECT * FROM dd_agent.assembly_repair_runs WHERE job_id=%s FOR UPDATE',(job,)).fetchone()
            if row['run_epoch']!=epoch:
                conn.execute('UPDATE dd_agent.assembly_repair_runs SET run_epoch=%s,owner_token=NULL WHERE job_id=%s',(epoch,job))
                row=dict(row,run_epoch=epoch,owner_token=None)
            if row['owner_token'] is not None:return None,self._summary(conn,job,waiting='owner')
            if row['phase']=='blocked':return None,self._summary(conn,job)
            # commit 与本账本进度是两笔短事务。崩溃后先识别已提交的确认，
            # 不能把自己的 checkpoint 推进误判成基线被别人替换。
            if row['plan_id']:
                ack=conn.execute("SELECT generation FROM dd_agent.assembly_adoptions WHERE plan_id=%s AND status='adopted'",(row['plan_id'],)).fetchone()
                if ack and ack['generation']==cp['generation']:
                    conn.execute("UPDATE dd_agent.assembly_repair_runs SET phase='done',adopted_generation=%s WHERE job_id=%s",(ack['generation'],job))
                    row=dict(row,phase='done',adopted_generation=ack['generation'])
            if row['phase']=='done' and row['adopted_generation']==cp['generation'] and row['source_stamp']==stamp and row['vision_policy']==policy:
                return None,self._summary(conn,job)
            if row['phase']=='done':
                # 成功后新来源是新一轮；未成功计划的预算绝不因暂停／重启重置。
                conn.execute('''UPDATE dd_agent.assembly_repair_runs SET phase='probing',cursor=1,plan_id=NULL,
                    attempts=0,failures=0,adopted_generation=NULL,error_code='',base_generation=%s,reading_version=%s,
                    page_count=%s,source_stamp=%s,vision_policy=%s WHERE job_id=%s''',
                    (cp['generation'],document['pipeline_version'],document['page_count'],stamp,policy,job))
                row=dict(row,phase='probing',cursor=1,plan_id=None,attempts=0,failures=0,base_generation=cp['generation'],
                    reading_version=document['pipeline_version'],page_count=document['page_count'],source_stamp=stamp,vision_policy=policy)
            if (row['base_generation'],row['reading_version'],row['page_count'])!=(cp['generation'],document['pipeline_version'],document['page_count']):
                self._block(conn,job,'repair_baseline_changed');return None,self._summary(conn,job)
            if row['vision_policy']!=policy:
                self._block(conn,job,'repair_policy_changed');return None,self._summary(conn,job)
            if not cp['state'] or not cp['state']['closed'] or cp['state']['next_page']!=document['page_count']+1:
                self._block(conn,job,'repair_open_baseline');return None,self._summary(conn,job)
            if row['source_stamp']!=stamp:
                self._restart_probe(conn,row,stamp)
                return None,self._summary(conn,job,waiting='sources')
            if not reading_done:return None,self._summary(conn,job,waiting='reading')
            action=row['phase']
            if action=='starting' or action=='probing' and row['cursor']>row['page_count']:
                if row['attempts']>=3:
                    self._block(conn,job,'repair_replan_exhausted');return None,self._summary(conn,job)
                action='starting';row=dict(row,attempts=row['attempts']+1,phase=action)
            owner=uuid4()
            conn.execute('''UPDATE dd_agent.assembly_repair_runs SET owner_token=%s,phase=%s,attempts=%s,updated_at=clock_timestamp()
                WHERE job_id=%s''',(owner,action,row['attempts'],job))
            return dict(row,owner_token=owner,parent=parent,document=document,action=action),None

    def sources_current(self,job,*,expected_epoch):
        """只用于接纳后仍 stale 的收尾判定，不把历史确认当作当前成功。"""
        with self.database.connect() as conn:
            parent,_,cp=self._context(conn,job,expected_epoch)
            row=conn.execute('SELECT source_stamp,adopted_generation FROM dd_agent.assembly_repair_runs WHERE job_id=%s',(job,)).fetchone()
            return bool(row and row['adopted_generation']==cp['generation'] and row['source_stamp']==self._stamp(conn,parent['document_id']))

    def _drop_owner(self,job,epoch,owner,*,refund=False):
        with self.database.connect() as conn:
            parent=self.stages._parent(conn,job)
            if parent['run_epoch']==epoch:
                conn.execute('''UPDATE dd_agent.assembly_repair_runs SET owner_token=NULL,attempts=greatest(attempts-%s,0)
                    WHERE job_id=%s AND run_epoch=%s AND owner_token=%s''',(int(refund),job,epoch,owner))

    def _finish(self,job,epoch,owned,result,*,unexpected=False):
        with self.database.connect() as conn:
            parent,document,cp=self._context(conn,job,epoch)
            row=conn.execute('SELECT * FROM dd_agent.assembly_repair_runs WHERE job_id=%s FOR UPDATE',(job,)).fetchone()
            if row['run_epoch']!=epoch or row['owner_token']!=owned['owner_token']:return self._summary(conn,job,waiting='owner')
            stamp=self._stamp(conn,parent['document_id']);waiting=''
            if unexpected:
                failures=row['failures']+1
                conn.execute('UPDATE dd_agent.assembly_repair_runs SET failures=%s,owner_token=NULL WHERE job_id=%s',(failures,job))
                if failures>=3:self._block(conn,job,'repair_step_exhausted')
                return self._summary(conn,job,waiting='retry' if failures<3 else '')
            action=owned['action'];status=result.get('status','')
            plan_id=result['id'] if action=='starting' else row['plan_id']
            if status=='adopted':
                conn.execute("UPDATE dd_agent.assembly_repair_runs SET phase='done',adopted_generation=%s,owner_token=NULL,updated_at=clock_timestamp() WHERE job_id=%s",(result['generation'],job))
                return self._summary(conn,job,worked=True)
            if cp['generation']!=row['base_generation']:
                self._block(conn,job,'repair_baseline_changed');return self._summary(conn,job)
            if stamp!=row['source_stamp'] or status=='superseded':
                self._restart_probe(conn,dict(row,plan_id=plan_id),stamp)
                return self._summary(conn,job,waiting='sources',worked=True)
            if status=='blocked':
                self._block(conn,job,result.get('error_code') or 'repair_plan_blocked');return self._summary(conn,job)
            phase=row['phase'];cursor=row['cursor']
            if action=='probing':
                cursor=result['cursor'];waiting=result['waiting']
                if waiting in ('assembly_input_too_large','vision_artifact_missing'):
                    self._block(conn,job,waiting);return self._summary(conn,job)
            elif action=='starting':phase='scanning'
            elif action in ('scanning','preparing'):
                phase='validating' if status in ('prepared','clean') else 'preparing' if status in ('ready','preparing') else 'scanning'
                waiting=result.get('waiting','')
            elif action=='validating':phase='adopting' if status=='ready' else 'validating'
            elif action=='adopting':waiting=result.get('waiting','')
            conn.execute('''UPDATE dd_agent.assembly_repair_runs SET phase=%s,cursor=%s,plan_id=%s,owner_token=NULL,
                updated_at=clock_timestamp() WHERE job_id=%s''',(phase,cursor,plan_id,job))
            return self._summary(conn,job,waiting=waiting,worked=waiting not in ('vision','reading','claim','retry'))

    @timed('assembly_repair.step')
    def step(self,job_id,*,expected_epoch,vision_policy,reading_done,checkpoint=None):
        if type(reading_done) is not bool or not isinstance(vision_policy,str) or not 1<=len(vision_policy)<=256:
            raise ValueError('读取完成状态和视觉策略必须明确。')
        key=(str(job_id),expected_epoch,vision_policy,reading_done)
        deferred=self._deferred
        if deferred and deferred[0]==key and monotonic()<deferred[1]:
            with self.database.connect() as conn:
                # 退避也必须看到暂停／旧 epoch；不写 owner，也不重新扫来源。
                self._context(conn,job_id,expected_epoch,active=True,exclusive=False)
                current=self._summary(conn,job_id,waiting=deferred[5])
                if current and (current['phase'],current['plan_id'],current['checked_pages'])==deferred[2:5]:return current
        owned,summary=self._acquire(job_id,expected_epoch,vision_policy,reading_done)
        if owned is None:return self._remember(key,summary)
        result=None
        try:
            if checkpoint:checkpoint('assembly_repair',max(1,owned['cursor']))
            if owned['plan_id']:
                self.rebuild.resume(job_id,owned['plan_id'],expected_epoch=expected_epoch)
            action=owned['action'];args=dict(expected_epoch=expected_epoch)
            if action=='probing':
                selected,_,reason=resolved_prefix(self.database,owned['parent'],owned['document'],owned['cursor'],20,vision_policy,reading_done=True)
                result=dict(cursor=owned['cursor']+len(selected),waiting=reason)
            elif action=='starting':result=self.rebuild.start(job_id,vision_policy=vision_policy,**args)
            elif action=='scanning':result=self.rebuild.scan(job_id,owned['plan_id'],limit=4,**args)
            elif action=='preparing':result=self.rebuild.prepare_next(job_id,owned['plan_id'],checkpoint=checkpoint,**args)
            elif action=='validating':result=self.adoption.validate_next(job_id,owned['plan_id'],checkpoint=checkpoint,**args)
            else:result=self.adoption.commit(job_id,owned['plan_id'],checkpoint=checkpoint,**args)
            return self._remember(key,self._finish(job_id,expected_epoch,owned,result))
        except (ProcessingPaused,ProcessingStopped,StagePaused):
            # 创建尚未发生的合作式暂停不是失败；未知崩溃／异常不退款，
            # 避免提交确认不明时通过反复重启绕过计划预算。
            self._drop_owner(job_id,expected_epoch,owned['owner_token'],refund=owned['action']=='starting' and result is None);raise
        except Exception:
            # 不把内部异常或正文送到界面；异常预算同样持久，不无限轮询。
            return self._finish(job_id,expected_epoch,owned,{},unexpected=True)
