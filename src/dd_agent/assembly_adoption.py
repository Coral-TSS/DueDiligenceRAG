"""逐份校验后原子接纳局部重算；不执行模型或切换默认检索。

position 是物理窗口次序，generation 是不可变收据身份。发布时只追加 replace
收据，keep 保留原身份／向量来源；谱系、检查点、outbox 和阶段成功同事务提交。
"""
from psycopg.types.json import Jsonb

from .assembly import VERSION,digest
from .assembly_rebuild import AssemblyRebuild,check_state
from .assembly_store import AssemblyConflict
from .control import ProcessingPaused,ProcessingStopped
from .stage_publication import StagePaused,StageSuperseded
from .performance import timed


# PostgreSQL 的 JSONB 表示与 Python canonical 不同。先验证 Python seal，再保存
# 同一行的数据库摘要；发布复验此摘要，避免锁内再次往 Python 搬运所有正文。
DB_HASH="encode(sha256(convert_to(result::text,'UTF8')),'hex')"
FINAL_FROM='''FROM dd_agent.assembly_rebuild_members m
    JOIN dd_agent.assembly_batches b ON b.document_id=m.document_id AND b.policy_version=m.policy_version AND b.generation=m.receipt_generation
    LEFT JOIN dd_agent.assembly_rebuild_parts p USING(plan_id,receipt_generation)
    WHERE m.plan_id=%s ORDER BY m.position DESC LIMIT 1'''
FINAL_STATE="(coalesce(p.result,b.result)->'state')"


class AssemblyAdoption:
    def __init__(self,stages):
        self.stages,self.database=stages,stages.database
        self.rebuild=AssemblyRebuild(stages)

    def _load(self,conn,job,plan,epoch,*,exclusive=True):
        parent,document,baseline=self.rebuild._guard(conn,job,plan,epoch,exclusive=exclusive)
        if baseline['status'] not in ('prepared','clean','adopted','blocked','superseded'):
            raise ValueError('必须先完成局部重算准备。')
        conn.execute('INSERT INTO dd_agent.assembly_adoptions(plan_id) VALUES(%s) ON CONFLICT DO NOTHING',(plan,))
        row=conn.execute('SELECT * FROM dd_agent.assembly_adoptions WHERE plan_id=%s FOR UPDATE',(plan,)).fetchone()
        if row['status']!='adopted' and baseline['status'] in ('blocked','superseded'):
            self._mark(conn,plan,baseline['status'],baseline['error_code'])
            row=dict(row,status=baseline['status'],error_code=baseline['error_code'])
        return parent,document,baseline,row

    @staticmethod
    def _mark(conn,plan,status,code=''):
        conn.execute('''UPDATE dd_agent.assembly_adoptions SET status=%s,error_code=%s,updated_at=clock_timestamp()
            WHERE plan_id=%s''',(status,code,plan))

    @staticmethod
    def _summary(conn,plan,*,waiting=''):
        row=conn.execute('''SELECT a.status,a.error_code,a.generation,
            (SELECT count(*) FROM dd_agent.assembly_adoption_checks c WHERE c.plan_id=a.plan_id) AS validated
            FROM dd_agent.assembly_adoptions a WHERE a.plan_id=%s''',(plan,)).fetchone()
        return dict(row,waiting=waiting)

    @timed('assembly_adoption.validate')
    def validate_next(self,job_id,plan_id,*,expected_epoch,checkpoint=None):
        with self.database.connect() as conn:
            parent,document,plan,adoption=self._load(conn,job_id,plan_id,expected_epoch)
            if adoption['status']!='validating':return self._summary(conn,plan_id)
            part=conn.execute('''SELECT p.*,m.position,m.next_page,m.closed,m.input_dependencies,
                '''+DB_HASH.replace('result::','p.result::')+''' AS payload_hash
                FROM dd_agent.assembly_rebuild_parts p JOIN dd_agent.assembly_rebuild_members m USING(plan_id,receipt_generation)
                WHERE p.plan_id=%s AND m.position>%s ORDER BY m.position LIMIT 1''',(plan_id,adoption['cursor'])).fetchone()
            last=conn.execute('SELECT '+FINAL_STATE+" AS state,encode(sha256(convert_to("+FINAL_STATE+"::text,'UTF8')),'hex') AS hash "+FINAL_FROM,(plan_id,)).fetchone()
        # 正文摘要和尾段检查只在锁外。每次一个 part，不加载整份报告或向量。
        try:
            check_state(last['state'],parent['project_id'],parent['document_id'])
            if not last['state']['closed'] or last['state']['next_page']!=document['page_count']+1:
                raise ValueError('末尾未封口。')
            if part:
                result=part['result'];state=result['state']
                if digest(result)!=part['seal']:raise ValueError('重算正文已损坏。')
                check_state(state,parent['project_id'],parent['document_id'])
                if (state['next_page'],state['closed'])!=(part['next_page'],part['closed']):raise ValueError('重算范围不符。')
                if [d['page_number'] for d in result['input_dependencies']]!=[d['page_number'] for d in part['input_dependencies']]:
                    raise ValueError('输入页序不符。')
        except (ValueError,KeyError,TypeError):
            with self.database.connect() as conn:
                _,_,_,row=self._load(conn,job_id,plan_id,expected_epoch)
                if row['status']=='validating':self._mark(conn,plan_id,'blocked','invalid_payload')
                return self._summary(conn,plan_id)
        if checkpoint:checkpoint('assembly_adoption_validation',1)
        with self.database.connect() as conn:
            _,_,_,current=self._load(conn,job_id,plan_id,expected_epoch)
            if current['status']!='validating' or current['cursor']!=adoption['cursor']:
                return self._summary(conn,plan_id)
            cursor=adoption['cursor']
            if part:
                now=conn.execute('SELECT seal,'+DB_HASH+''' AS payload_hash FROM dd_agent.assembly_rebuild_parts
                    WHERE plan_id=%s AND receipt_generation=%s''',(plan_id,part['receipt_generation'])).fetchone()
                if (now['seal'],now['payload_hash'])!=(part['seal'],part['payload_hash']):
                    self._mark(conn,plan_id,'blocked','payload_changed');return self._summary(conn,plan_id)
                conn.execute('''INSERT INTO dd_agent.assembly_adoption_checks(plan_id,receipt_generation,seal,payload_hash)
                    VALUES(%s,%s,%s,%s)''',(plan_id,part['receipt_generation'],part['seal'],part['payload_hash']))
                cursor=part['position']
            more=conn.execute('''SELECT 1 FROM dd_agent.assembly_rebuild_parts p
                JOIN dd_agent.assembly_rebuild_members m USING(plan_id,receipt_generation)
                WHERE p.plan_id=%s AND m.position>%s LIMIT 1''',(plan_id,cursor)).fetchone()
            conn.execute('''UPDATE dd_agent.assembly_adoptions SET cursor=%s,status=%s,final_state=%s,final_hash=%s,
                updated_at=clock_timestamp() WHERE plan_id=%s''',
                (cursor,'validating' if more else 'ready',Jsonb(last['state']),last['hash'],plan_id))
            return self._summary(conn,plan_id)

    @staticmethod
    def _payloads_current(conn,plan_id,adoption):
        valid=conn.execute('''SELECT NOT EXISTS(SELECT 1 FROM dd_agent.assembly_rebuild_parts p
            LEFT JOIN dd_agent.assembly_adoption_checks c USING(plan_id,receipt_generation)
            WHERE p.plan_id=%s AND (c.seal IS DISTINCT FROM p.seal OR c.payload_hash IS DISTINCT FROM
            '''+DB_HASH.replace('result::','p.result::')+''')) AS valid''',(plan_id,)).fetchone()['valid']
        final=conn.execute("SELECT encode(sha256(convert_to("+FINAL_STATE+"::text,'UTF8')),'hex') AS hash "+FINAL_FROM,(plan_id,)).fetchone()
        saved=conn.execute("SELECT encode(sha256(convert_to(final_state::text,'UTF8')),'hex') AS hash FROM dd_agent.assembly_adoptions WHERE plan_id=%s",(plan_id,)).fetchone()
        return valid and final['hash']==saved['hash']==adoption['final_hash']

    def _switch(self,conn,plan,adoption):
        """SQL-only 原子切换；不覆盖旧 receipt，不复制 keep 的正文或向量。"""
        identity=plan['id'];document=plan['document_id'];base=plan['base_generation']
        conn.execute('''INSERT INTO dd_agent.assembly_batches(document_id,policy_version,generation,request_key,result)
            SELECT %s,%s,%s+row_number() OVER(ORDER BY m.position),
                encode(sha256(convert_to(%s||':'||m.position::text,'UTF8')),'hex'),
                jsonb_set(p.result,'{generation}',to_jsonb(%s+row_number() OVER(ORDER BY m.position)))
            FROM dd_agent.assembly_rebuild_parts p JOIN dd_agent.assembly_rebuild_members m USING(plan_id,receipt_generation)
            WHERE p.plan_id=%s ORDER BY m.position''',(document,VERSION,base,str(identity),base,identity))
        n=conn.execute('SELECT count(*) AS n FROM dd_agent.assembly_rebuild_parts WHERE plan_id=%s',(identity,)).fetchone()['n']
        conn.execute('''INSERT INTO dd_agent.assembly_lineage_heads(document_id,assembly_policy,plan_id,reset_generation)
            VALUES(%s,%s,%s,%s) ON CONFLICT(document_id,assembly_policy) DO UPDATE
            SET plan_id=excluded.plan_id,reset_generation=excluded.reset_generation''',(document,VERSION,identity,plan['reset_generation']))
        conn.execute('DELETE FROM dd_agent.assembly_lineage_members WHERE document_id=%s AND assembly_policy=%s',(document,VERSION))
        conn.execute('''INSERT INTO dd_agent.assembly_lineage_members(document_id,assembly_policy,position,receipt_generation)
            SELECT %s,%s,m.position,CASE WHEN p.plan_id IS NULL THEN m.receipt_generation
                ELSE %s+count(p.plan_id) OVER(ORDER BY m.position) END
            FROM dd_agent.assembly_rebuild_members m LEFT JOIN dd_agent.assembly_rebuild_parts p USING(plan_id,receipt_generation)
            WHERE m.plan_id=%s ORDER BY m.position''',(document,VERSION,base,identity))
        conn.execute('''UPDATE dd_agent.assembly_checkpoints SET generation=%s,state=%s,updated_at=clock_timestamp()
            WHERE document_id=%s AND policy_version=%s''',(base+n,Jsonb(adoption['final_state']),document,VERSION))
        # 已领取的旧包也退役。发布器仍复验 origin，迟到进程不能发布；向量检查点保留。
        conn.execute('''WITH retired AS (UPDATE dd_agent.stage_jobs SET state='cancelled',owner_token=NULL,
            error_code='origin_retired',updated_at=clock_timestamp()
            WHERE document_id=%s AND stage='embedding' AND scope ? 'parent_build' AND state IN ('pending','running')
              AND NOT dd_agent.parent_build_origin_current(scope->>'parent_build') RETURNING id)
            UPDATE dd_agent.stage_attempts SET ended_at=clock_timestamp(),outcome='cancelled',error_code='origin_retired'
            WHERE ended_at IS NULL AND stage_job_id IN (SELECT id FROM retired)''',(document,))
        conn.execute("UPDATE dd_agent.assembly_rebuild_plans SET status='adopted',updated_at=clock_timestamp() WHERE id=%s",(identity,))
        conn.execute("UPDATE dd_agent.assembly_adoptions SET status='adopted',generation=%s,updated_at=clock_timestamp() WHERE plan_id=%s",(base+n,identity))

    @timed('assembly_adoption.commit')
    def commit(self,job_id,plan_id,*,expected_epoch,checkpoint=None):
        with self.database.connect() as conn:
            _,_,plan,row=self._load(conn,job_id,plan_id,expected_epoch)
            if row['status']!='ready':return self._summary(conn,plan_id)
            if not self._payloads_current(conn,plan_id,row):
                self._mark(conn,plan_id,'blocked','payload_changed');return self._summary(conn,plan_id)
            # 阶段绑定第一页作来源锚点；完整冻结来源由 reading 锁内的 _guard 复验。
            source=conn.execute('SELECT * FROM dd_agent.assembly_rebuild_sources WHERE plan_id=%s AND page_number=1',(plan_id,)).fetchone()
            dep=dict(page_number=1,view='effective',revision=source['revision'],quality=source['quality'],
                readiness='pending' if source['status']=='succeeded' else source['status'])
            task=self.stages.ensure(job_id,'assembly',dict(pages=[1],assembly_adoption=str(plan_id)),
                source_revision=digest(dict(plan=str(plan_id),base=plan['base_generation'],final=row['final_hash'])),
                policy_version='adopt:'+VERSION,dependencies=[dep],reuse=False,conn=conn)
        claim=self.stages.claim(job_id,'assembly',task_id=task['id'])
        if claim is None:
            with self.database.connect() as conn:
                _,_,_,current=self._load(conn,job_id,plan_id,expected_epoch)
                state=conn.execute('SELECT state,error_code FROM dd_agent.stage_jobs WHERE id=%s',(task['id'],)).fetchone()
                if current['status']=='ready' and state['state'] in ('failed','cancelled'):
                    self._mark(conn,plan_id,'blocked',state['error_code'] or 'adoption_stage_failed')
                    return self._summary(conn,plan_id)
                return self._summary(conn,plan_id,waiting='claim' if current['status']=='ready' else '')
        try:
            if checkpoint:checkpoint('assembly_adoption',1)
            with self.stages.publish_scope(claim['id'],claim['owner_token']) as conn:
                _,_,plan,current=self._load(conn,job_id,plan_id,expected_epoch,exclusive=False)
                if current['status']!='ready':raise AssemblyConflict('接纳状态已改变。')
                if not self._payloads_current(conn,plan_id,current):raise AssemblyConflict('接纳正文已改变。')
                self._switch(conn,plan,current)
        except (ProcessingPaused,ProcessingStopped,StagePaused):
            self.stages.park(claim['id'],claim['owner_token']);raise
        except (StageSuperseded,AssemblyConflict):
            self.stages.fail(claim['id'],claim['owner_token'],category='stale',error_code='adoption_changed')
        except Exception:
            self.stages.fail(claim['id'],claim['owner_token'],category='transient',error_code='adoption_save_failed',retry_after=0)
            raise
        with self.database.connect() as conn:
            _,_,_,current=self._load(conn,job_id,plan_id,expected_epoch)
            # publish_scope 中发现的冲突会回滚其状态更新；在新事务准确记录
            # 正文失效，不能让调用方再走一轮才看到 blocked。
            if current['status']=='ready' and not self._payloads_current(conn,plan_id,current):
                self._mark(conn,plan_id,'blocked','payload_changed')
            return self._summary(conn,plan_id)
