"""父子索引工作包恢复；不负责自动领取父任务或切换应用索引。

准备清单与 StageJobs 登记同事务，向量另存受保护的可恢复检查点。
加载、保存和发布都绑定当前领取，旧进程不能凭旧 UUID 使用工作包。
"""
from copy import deepcopy
from uuid import UUID

from psycopg.types.json import Jsonb

from .assembly import digest
from .assembly_store import AssemblyStore
from .control import ProcessingPaused,ProcessingStopped,ProcessingDeferred
from .model_admission import request_scope
from .parent_index import stage_binding
from .performance import timed,count
from .retrieval import valid_vector
from .stage_publication import StageSuperseded,StagePaused


def manifest_seal(plan):
    """已算向量不改变准备清单身份；哈希在持锁事务外计算。"""
    return digest(dict({k:v for k,v in plan.items() if k!='seal'},vectors=None))


def persist_preparation(conn,plan,*,repair=False):
    """调用者已持 parent／reading 锁；不在这里加载模型或开启另一连接。"""
    if plan['vectors'] is not None:raise ValueError('阶段登记必须使用未计算向量的准备清单。')
    conn.execute('''INSERT INTO dd_agent.parent_index_work
        (id,document_id,index_version,input_revision,plan,plan_seal) VALUES(%s,%s,%s,%s,%s,%s)
        ON CONFLICT(id) DO NOTHING''',
        (plan['id'],plan['document_id'],plan['index_version'],plan['input_revision'],Jsonb(plan),plan['seal']))
    if repair:
        # 只供收据交接：调用者已经证明当前收据分段和来源，不接受任意
        # register 请求覆盖计划。行锁与账本／阶段登记共同提交或回滚。
        row=conn.execute('''SELECT document_id,index_version,input_revision,plan,plan_seal
            FROM dd_agent.parent_index_work WHERE id=%s FOR UPDATE''',(plan['id'],)).fetchone()
        if (str(row['document_id']),row['index_version'],row['input_revision'])!=(
                plan['document_id'],plan['index_version'],plan['input_revision']):
            raise ValueError('工作包登记身份冲突，不能修复。')
        if row['plan']!=plan or row['plan_seal']!=plan['seal']:
            conn.execute('''UPDATE dd_agent.parent_index_work SET plan=%s,plan_seal=%s,
                vectors=NULL,vector_seal=NULL,updated_at=clock_timestamp() WHERE id=%s''',
                (Jsonb(plan),plan['seal'],plan['id']))
            count('assembly.handoff.plan_repaired')
    require_preparation(conn,plan,plan['seal'])


def require_preparation(conn,plan,expected_seal,*,computed=False):
    row=conn.execute('''SELECT document_id,index_version,input_revision,plan_seal,vector_seal
        FROM dd_agent.parent_index_work WHERE id=%s''',(plan['id'],)).fetchone()
    if not row:raise ValueError('缺少持久准备清单，请从组装收据重新准备。')
    if (str(row['document_id']),row['index_version'],row['input_revision'],row['plan_seal'])!=(
        plan['document_id'],plan['index_version'],plan['input_revision'],expected_seal):
        raise ValueError('同一批次的准备清单冲突，不能覆盖原成果。')
    if computed and row['vector_seal'] is not None and row['vector_seal']!=plan['seal']:
        raise ValueError('向量检查点冲突，不能替换已保存结果。')
    return row


class ParentWork:
    def __init__(self,service,stages):
        self.service,self.stages,self.database=service,stages,service.database

    @staticmethod
    def _vectors(plan):
        if not isinstance(plan['vectors'],list) or len(plan['vectors'])!=len(plan['children']):
            raise ValueError('向量检查点尚未完整。')
        for vector in plan['vectors']:valid_vector(vector)

    def _sources(self,conn,plan):
        from .assembly_handoff import require_origin
        document=AssemblyStore._document(conn,plan['project_id'],plan['document_id'],lock=True)
        require_origin(conn,plan['id'])
        AssemblyStore._validate(conn,plan['project_id'],plan['document_id'],document,plan['dependencies'],[])

    @timed('parent_work.load')
    def load(self,task_id,owner_token):
        # 首先按当前领取过滤，避免向无权执行者返回正文或向量。
        with self.database.connect() as conn:
            row=conn.execute('''SELECT s.*,w.plan,w.plan_seal,w.vectors AS saved_vectors,w.vector_seal
                FROM dd_agent.stage_jobs s JOIN dd_agent.processing_jobs j ON j.id=s.job_id
                JOIN dd_agent.parent_index_work w ON w.id=s.scope->>'parent_build' AND w.document_id=s.document_id
                WHERE s.id=%s AND s.owner_token=%s AND s.state='running' AND s.run_epoch=j.run_epoch
                  AND j.status IN ('queued','running','pause_requested')''',(task_id,UUID(str(owner_token)))).fetchone()
        if row is None:raise StageSuperseded('任务、领取或执行代次已失效。')
        plan=row['plan']
        self.service._verify(plan)  # 指纹／清单校验在事务外。
        if plan['vectors'] is not None or plan['seal']!=row['plan_seal']:raise ValueError('准备清单已损坏。')
        scope,_,revision=stage_binding(plan)
        if (row['stage'],str(row['project_id']),str(row['document_id']),row['scope'],row['input_revision'])!=(
            'embedding',plan['project_id'],plan['document_id'],scope,revision):
            raise ValueError('持久清单与阶段绑定不符。')
        if row['saved_vectors'] is not None:
            plan=deepcopy(plan);plan['vectors']=row['saved_vectors'];plan['seal']=row['vector_seal']
            self.service._verify(plan);self._vectors(plan)
        expected=manifest_seal(plan)
        # 读取后再做一次受 owner／epoch 保护的来源复验，不持锁进入推理。
        with self.stages.publish_scope(task_id,owner_token,final=False) as conn:
            require_preparation(conn,plan,expected,computed=plan['vectors'] is not None)
            self._sources(conn,plan)
        count('parent_work.restored_vectors' if plan['vectors'] is not None else 'parent_work.restored_plan')
        return plan

    @timed('parent_work.save_vectors')
    def save_vectors(self,task_id,owner_token,plan):
        self.service._verify(plan);self._vectors(plan)
        expected=manifest_seal(plan)
        # 与最终发布使用相同的任务绑定；不能借另一个材料的领取保存结果。
        scope,_,revision=stage_binding(plan)
        with self.stages.publish_scope(task_id,owner_token,final=False) as conn:
            task=conn.execute('SELECT stage,project_id,document_id,scope,input_revision FROM dd_agent.stage_jobs WHERE id=%s',(task_id,)).fetchone()
            if (task['stage'],str(task['project_id']),str(task['document_id']),task['scope'],task['input_revision'])!=(
                'embedding',plan['project_id'],plan['document_id'],scope,revision):
                raise ValueError('向量检查点与阶段领取不符。')
            require_preparation(conn,plan,expected,computed=True)
            self._sources(conn,plan)
            conn.execute('''UPDATE dd_agent.parent_index_work SET vectors=%s,vector_seal=%s,updated_at=clock_timestamp()
                WHERE id=%s''',(Jsonb(plan['vectors']),plan['seal'],plan['id']))
        count('parent_work.saved_vectors')

    def requeue(self,job_id,identity,*,expected_epoch):
        """明确继续后的重新登记；只读已有清单，不 OCR、不重新 tokenize。"""
        if type(expected_epoch) is not int or expected_epoch<1:raise ValueError('执行代次必须为正整数。')
        with self.database.connect() as conn:
            parent=self.stages._parent(conn,job_id)
            if parent['run_epoch']!=expected_epoch or parent['status'] not in ('queued','running','pause_requested'):
                raise StageSuperseded('重新登记的执行代次已失效。')
            row=conn.execute('SELECT plan FROM dd_agent.parent_index_work WHERE id=%s AND document_id=%s',
                (identity,parent['document_id'])).fetchone()
        if row is None:raise ValueError('工作包不存在或不属于当前材料。')
        # register 再次持 parent 锁核对代次，封住读取后发生 resume 的窗口。
        return self.service.register(self.stages,job_id,row['plan'],expected_epoch=expected_epoch)

    @timed('parent_work.run')
    def run(self,task_id,owner_token,*,checkpoint=None):
        try:
            with request_scope('background',defer_busy=True):
                plan=self.load(task_id,owner_token)
                number=min(d['page_number'] for d in plan['dependencies'] if d['readiness']=='ready')
                if checkpoint:checkpoint('embedding',number)
                if plan['vectors'] is None:
                    plan=self.service.embed(plan)
                    self.save_vectors(task_id,owner_token,plan)
                # 先耐久保存再响应暂停，继续时无需重新算已经完成的向量。
                if checkpoint:checkpoint('embedding',number)
                return self.service.publish(self.stages,task_id,owner_token,plan)
        except ProcessingDeferred:
            self.stages.park(task_id,owner_token,reason='deferred',retry_after=.2)
            raise
        except (ProcessingPaused,ProcessingStopped,StagePaused):
            self.stages.park(task_id,owner_token)
            raise
