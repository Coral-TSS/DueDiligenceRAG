"""组装的领取保护入口；不自动启用窗口生产者或改动活动索引。

清单是一次纯组装计算的局部副本。页面、尾段和收据已有持久存储；中断后
由协调器重新准备未提交窗口，不为它再建一份全文库。原文组装／摘要在锁外，
只有 SQL 保存进入同一个阶段发布事务。
"""
from uuid import UUID

from .assembly import VERSION,digest
from .assembly_store import AssemblyStale,AssemblyConflict
from .control import ProcessingPaused,ProcessingStopped
from .stage_publication import StagePaused,StageSuperseded
from .performance import timed


def binding(plan):
    dependencies=[dict(page_number=d['page_number'],view='effective',revision=d['source_revision'],
        readiness=d['readiness'],quality=d['quality']) for d in plan['dependencies']]
    dependencies.sort(key=lambda d:(d['page_number'],d['view'],d['revision']))
    scope=dict(pages=sorted({d['page_number'] for d in dependencies}),
        assembly_request=plan['request_key'],generation=plan['expected_generation'],final=plan['final'])
    revision=digest(dict(stage='assembly',scope_key=digest(scope),source=plan['request_key'],policy=VERSION,
        dependencies=dependencies,predecessors=[]))
    return scope,dependencies,revision


class AssemblyWork:
    def __init__(self,store,stages):
        self.store,self.stages,self.database=store,stages,store.database

    @staticmethod
    def _active(parent,epoch):
        if parent['run_epoch']!=epoch:raise StageSuperseded('组装执行代次已失效。')
        if parent['status'] in ('paused','pause_requested'):raise ProcessingPaused()
        if parent['status'] not in ('queued','running'):raise StageSuperseded('组装父任务已结束。')
        if {'assembly','embedding'}.intersection(parent.get('paused_stages',[])):raise StagePaused()

    @timed('assembly_work.register')
    def register(self,job_id,prepared,*,expected_epoch):
        if type(expected_epoch) is not int or expected_epoch<1:raise ValueError('执行代次必须为正整数。')
        plan,_=self.store._validated(prepared)
        scope,deps,_=binding(plan)
        with self.database.connect() as conn:
            parent=self.stages._parent(conn,job_id,exclusive=True)
            self._active(parent,expected_epoch)
            if (str(parent['project_id']),str(parent['document_id']))!=(plan['project_id'],plan['document_id']):
                raise ValueError('组装准备不属于当前父任务。')
            # 同一执行代次的显式重放也重新领取，绝不借用历史 owner 发布。
            return self.stages.ensure(job_id,'assembly',scope,source_revision=plan['request_key'],
                policy_version=VERSION,dependencies=deps,reuse=False,conn=conn)

    @timed('assembly_work.run')
    def run(self,task_id,owner_token,prepared,*,checkpoint=None):
        try:
            # 大清单复制及摘要在任何 SQL 锁之外；后续只使用该局部副本。
            plan,pages=self.store._validated(prepared)
            scope,_,revision=binding(plan)
            with self.database.connect() as conn:
                task=conn.execute('''SELECT s.*,j.status AS parent_status,j.run_epoch AS parent_epoch,j.paused_stages
                    FROM dd_agent.stage_jobs s JOIN dd_agent.processing_jobs j ON j.id=s.job_id
                    WHERE s.id=%s AND s.owner_token=%s AND s.state='running' ''',
                    (task_id,UUID(str(owner_token)))).fetchone()
            if task is None:raise StageSuperseded('组装领取不存在或已失效。')
            self._active(dict(run_epoch=task['parent_epoch'],status=task['parent_status'],paused_stages=task['paused_stages']),task['run_epoch'])
            if (task['stage'],str(task['project_id']),str(task['document_id']),task['scope'],task['input_revision'])!=(
                'assembly',plan['project_id'],plan['document_id'],scope,revision):
                raise ValueError('组装准备与阶段领取不符。')
            if checkpoint:checkpoint('assembly',min(scope['pages']))
            with self.stages.publish_scope(task_id,owner_token) as conn:
                # 暂停可能在外层检查后发生；持锁后只做本连接内的控制复验，
                # 不能调用会另开连接保存进度的 checkpoint。
                parent=self.stages._parent(conn,task['job_id'])
                self._active(parent,task['run_epoch'])
                if conn.execute('''SELECT 1 FROM dd_agent.assembly_receipt_outbox
                    WHERE document_id=%s AND assembly_policy=%s AND is_reset
                    AND receipt_generation>%s LIMIT 1''',
                    (task['document_id'],VERSION,plan['expected_generation'])).fetchone():
                    raise StageSuperseded('组装已重置，不能在当前领取中重放旧收据。')
                result=self.store._commit_verified(plan,pages,conn)
                conn.execute('UPDATE dd_agent.stage_jobs SET result_ref=%s WHERE id=%s',
                    (str(result['generation']),task_id))
            return result
        except (ProcessingPaused,ProcessingStopped,StagePaused):
            self.stages.park(task_id,owner_token)
            raise
        except (StageSuperseded,AssemblyStale):
            self.stages.fail(task_id,owner_token,category='stale',error_code='assembly_source_changed')
            raise
        except AssemblyConflict:
            self.stages.fail(task_id,owner_token,category='stale',error_code='assembly_generation_changed')
            raise
        except Exception:
            self.stages.fail(task_id,owner_token,category='transient',error_code='assembly_publish_failed',retry_after=.2)
            raise
