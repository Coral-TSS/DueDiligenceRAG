"""组装检查点与批次收据。独立于活动索引，供 RG-06b 接入前验证。

纯组装在事务外执行；保存时按阅读记录→升序原始页→检查点加锁，
重新验证文字和边界依赖，避免暂停恢复、重投或并发时写入旧来源。
"""
from copy import deepcopy
from dataclasses import asdict
from uuid import UUID

from psycopg.types.json import Jsonb

from .assembly import PageSnapshot, VERSION, assemble, canonical, digest
from .performance import timed


class AssemblyConflict(Exception):
    """检查点已由另一执行者推进；重新读取后再计算。"""


class AssemblyStale(Exception):
    """来源已改变；不能把旧尾段或收据作为当前成果继续。"""


class AssemblyStore:
    def __init__(self,database):self.database=database

    @staticmethod
    def _document(conn,project_id,document_id,*,lock=False):
        row=conn.execute('''SELECT d.id,r.pipeline_version,r.page_count
            FROM dd_agent.documents d JOIN dd_agent.document_readings r ON r.document_id=d.id
            WHERE d.project_id=%s AND d.id=%s'''+(' FOR UPDATE OF r' if lock else ''),
            (UUID(str(project_id)),UUID(str(document_id)))).fetchone()
        if row is None:raise ValueError('材料不存在、不属于当前项目或尚未预检。')
        return row

    @staticmethod
    def _checkpoint(conn,document_id,*,lock=False):
        row=conn.execute('''SELECT generation,state FROM dd_agent.assembly_checkpoints
            WHERE document_id=%s AND policy_version=%s'''+(' FOR UPDATE' if lock else ''),
            (document_id,VERSION)).fetchone()
        return row or dict(generation=0,state=None)

    def load(self,project_id,document_id):
        with self.database.connect() as conn:
            self._document(conn,project_id,document_id)
            return self._checkpoint(conn,document_id)

    def snapshots(self,project_id,document_id,numbers,*,ready_pages):
        """必须由调用者明确哪些页已完成必要增强，不能把基础成功自动当作就绪。

        缺页显式进入序列；这里不调用 OCR 或模型。协调器的接入留给 06b。
        """
        numbers=list(numbers);ready_pages=set(ready_pages)
        if (not 1<=len(numbers)<=20 or any(type(n) is not int or n<1 for n in numbers)
            or numbers!=list(range(numbers[0],numbers[0]+len(numbers))) or not ready_pages.issubset(numbers)):
            raise ValueError('快照范围必须为连续 1–20 页，就绪集合须在本批范围内。')
        with self.database.connect() as conn:
            document=self._document(conn,project_id,document_id)
            if numbers[-1]>document['page_count']:raise ValueError('页号超出已知材料范围。')
            rows={r['page_number']:r for r in conn.execute('''SELECT page_number,status,source_revision,text,verification
                FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=ANY(%s)''',
                (document_id,numbers)).fetchall()}
        result=[]
        for n in numbers:
            row=rows.get(n)
            status='missing' if row is None else 'failed' if row['status']=='failed' else 'ready' if row['status']=='succeeded' and n in ready_pages else 'pending'
            result.append(PageSnapshot(str(project_id),str(document_id),n,
                str(row['source_revision']) if row else 'missing:'+document['pipeline_version'],
                row['text'] if status=='ready' else '',status,row['verification'] if row else 'unverified'))
        if sum(len(p.text) for p in result)>200000:raise ValueError('单批文字超过 200000 字符，请减小范围。')
        return result

    @staticmethod
    def _dependencies(result):
        # 新收据保留所有输入页，空页／缺口不能只剩最后一页的依赖。
        # 历史收据没有此字段时仍按其原有引用／尾段依赖验证，不伪造历史信息。
        dependencies=list(result.get('input_dependencies',[]))
        state=result.get('state') or {}
        blocks=[*result.get('parents',[])]
        if state.get('tail'):blocks.append(state['tail'])
        for block in blocks:
            for dependency in block['dependencies']:
                if dependency not in dependencies:dependencies.append(dependency)
        if state.get('last') and state['last'] not in dependencies:dependencies.append(state['last'])
        return dependencies

    @staticmethod
    def _validate(conn,project_id,document_id,document,dependencies,pages):
        numbers=sorted({d['page_number'] for d in dependencies}|{p.page_number for p in pages})
        if not numbers:return
        # 同原始／机器结果发布的阅读→页顺序；不存在的页由阅读行锁保护其插入。
        conn.execute('''SELECT page_number FROM dd_agent.document_pages WHERE document_id=%s
            AND pipeline_version=%s AND page_number=ANY(%s) ORDER BY page_number FOR UPDATE''',
            (document_id,document['pipeline_version'],numbers)).fetchall()
        rows={r['page_number']:r for r in conn.execute('''SELECT page_number,status,source_revision,text,verification
            FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=ANY(%s)''',
            (document_id,numbers)).fetchall()}
        for dep in dependencies:
            if (dep['project_id'],dep['document_id'])!=(str(project_id),str(document_id)):
                raise ValueError('依赖不属于当前项目／材料。')
            if dep['page_number']>document['page_count']:
                raise AssemblyStale('依赖页超出当前材料范围。')
            row=rows.get(dep['page_number'])
            if row is None:
                valid=dep['readiness']=='missing' and dep['source_revision']=='missing:'+document['pipeline_version']
            else:
                status=dep['readiness']
                valid=(str(row['source_revision'])==dep['source_revision'] and row['verification']==dep['quality']
                       and (status=='pending' or status=='ready' and row['status']=='succeeded' or status=='failed' and row['status']=='failed'))
            if not valid:raise AssemblyStale('组装依赖已改变，请重新读取快照并重建受影响范围。')
        # 仅比较修订不够：拒绝调用者携带同修订号但被改写的正文。
        for page in pages:
            row=rows.get(page.page_number)
            if page.readiness=='ready' and (row is None or page.text!=row['text']):
                raise AssemblyStale('快照正文与保存的来源不符。')

    @staticmethod
    def _generation(expected_generation):
        if type(expected_generation) is not int or expected_generation<0:
            raise ValueError('检查点代次必须为非负整数。')

    @staticmethod
    def _save(conn,document_id,generation,request_key,result):
        # 收据和检查点同事务；任一 SQL 失败都不会只保存其中一半。
        conn.execute('''INSERT INTO dd_agent.assembly_batches(document_id,policy_version,generation,request_key,result)
            VALUES(%s,%s,%s,%s,%s)''',(document_id,VERSION,generation,request_key,Jsonb(result)))
        conn.execute('''INSERT INTO dd_agent.assembly_checkpoints(document_id,policy_version,generation,state)
            VALUES(%s,%s,%s,%s) ON CONFLICT(document_id,policy_version) DO UPDATE
            SET generation=excluded.generation,state=excluded.state,updated_at=clock_timestamp()''',
            (document_id,VERSION,generation,Jsonb(result['state']) if result['state'] is not None else None))

    @timed('assembly.prepare')
    def prepare(self,project_id,document_id,pages,*,expected_generation,final=False):
        self._generation(expected_generation)
        pages=list(pages)
        if len(pages)>20 or sum(len(p.text) for p in pages)>200000:raise ValueError('组装批次过大。')
        for page in pages:
            if (page.project_id,page.document_id)!=(str(project_id),str(document_id)):
                raise ValueError('快照不属于当前项目／材料。')
        request_key=digest(dict(policy=VERSION,generation=expected_generation,pages=[asdict(p) for p in pages],final=bool(final)))
        before=self.load(project_id,document_id)
        if before['generation']==expected_generation:
            calculated=assemble(pages,state=before['state'],final=final)
            result=dict(calculated,generation=expected_generation+1,input_dependencies=[p.dependency() for p in pages])
            if len(canonical(result).encode('utf-8'))>8*1024*1024:raise ValueError('组装收据超过 8 MiB。')
        else:result=None
        dependencies=self._dependencies(result or {})
        for dep in [*self._dependencies(before),*(p.dependency() for p in pages)]:
            if dep not in dependencies:dependencies.append(dep)
        plan=dict(project_id=str(project_id),document_id=str(document_id),expected_generation=expected_generation,
            final=bool(final),request_key=request_key,pages=[asdict(p) for p in pages],
            before=before,result=result,dependencies=dependencies)
        plan['seal']=digest(plan)
        return plan

    @staticmethod
    def _validated(plan):
        # 复制、容量和摘要检查都必须发生在父任务／页／领取行锁之外。
        plan=deepcopy(plan)
        if len(canonical(plan).encode('utf-8'))>12*1024*1024:
            raise ValueError('组装准备清单超过 12 MiB。')
        if digest({k:v for k,v in plan.items() if k!='seal'})!=plan.get('seal'):
            raise ValueError('组装准备清单已改变。')
        pages=[PageSnapshot(**page) for page in plan['pages']]
        return plan,pages

    def commit(self,plan,conn):
        """供显式调用者复用事务；后台应在持锁前验证后使用内部提交入口。"""
        plan,pages=self._validated(plan)
        return self._commit_verified(plan,pages,conn)

    def _commit_verified(self,plan,pages,conn):
        """只接收本模块已经验证的局部副本，不在持锁区运行组装或大摘要。"""
        project_id,document_id=plan['project_id'],plan['document_id']
        expected_generation,final=plan['expected_generation'],plan['final']
        request_key,result=plan['request_key'],plan['result']
        document=self._document(conn,project_id,document_id,lock=True)
        checkpoint=self._checkpoint(conn,document_id,lock=True)
        if checkpoint['generation']!=expected_generation:
            receipt=conn.execute('''SELECT result FROM dd_agent.assembly_batches
                WHERE document_id=%s AND policy_version=%s AND request_key=%s''',
                (document_id,VERSION,request_key)).fetchone()
            if receipt:
                if final and receipt['result']['state']['next_page']!=document['page_count']+1:
                    raise AssemblyStale('材料页数已变化，原结束收据不能复用。')
                self._validate(conn,project_id,document_id,document,self._dependencies(receipt['result']),pages)
                return receipt['result']
            raise AssemblyConflict('检查点已推进，请重新加载。')
        if result is None:raise AssemblyConflict('检查点已改变，请重新加载。')
        if final and result['state']['next_page']!=document['page_count']+1:
            raise AssemblyStale('尚未到达材料末页，不能将窗口结束标为整份封口。')
        dependencies=plan['dependencies']
        if any(n.page_number>document['page_count'] for n in pages):raise AssemblyStale('材料页数已改变。')
        self._validate(conn,project_id,document_id,document,dependencies,pages)
        self._save(conn,document_id,expected_generation+1,request_key,result)
        return result


    @timed('assembly.advance')
    def advance(self,project_id,document_id,pages,*,expected_generation,final=False):
        # 保留旧内部调用；自动后台只能走 AssemblyWork 的领取保护入口。
        plan=self.prepare(project_id,document_id,pages,expected_generation=expected_generation,final=final)
        plan,pages=self._validated(plan)
        with self.database.connect() as conn:
            return self._commit_verified(plan,pages,conn)

    def restart(self,project_id,document_id,*,expected_generation):
        """显式放弃旧尾段，从首批重新组装；历史收据保留，不重做基础 OCR。"""
        self._generation(expected_generation)
        with self.database.connect() as conn:
            self._document(conn,project_id,document_id,lock=True)
            before=self._checkpoint(conn,document_id,lock=True)
            if before['generation']!=expected_generation:raise AssemblyConflict('检查点已推进。')
            result=dict(generation=expected_generation+1,state=None)
            self._save(conn,document_id,result['generation'],digest(dict(restart=expected_generation)),result)
        return result
