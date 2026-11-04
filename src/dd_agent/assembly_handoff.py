"""持久组装收据到工作包的有界对账；由受控后台调用。

收据 outbox 与尾段同事务；prepare／tokenizer 在事务外；登记、分段映射与
游标确认同事务。游标只属于某个父任务代次和索引版本，不充当全局水位。
"""
from .assembly import VERSION,PageSnapshot,canonical,digest
from .assembly_store import AssemblyStore,AssemblyStale
from .parent_index import ParentBatchTooLarge,_clip
from .performance import timed,count
from .stage_publication import StageSuperseded


def require_origin(conn,identity):
    row=conn.execute('SELECT dd_agent.parent_build_origin_current(%s) AS valid',(identity,)).fetchone()
    if not row['valid']:raise StageSuperseded('收据已退役或组装已重置，旧交接成果不能继续发布。')


class ReceiptHandoff:
    def __init__(self,index,stages):
        self.index,self.stages,self.database=index,stages,index.database

    def _parent(self,conn,job_id,epoch,*,exclusive=False):
        parent=self.stages._parent(conn,job_id,exclusive=exclusive)
        if parent['run_epoch']!=epoch or parent['status'] not in ('queued','running','pause_requested'):
            raise StageSuperseded('交接执行代次已失效。')
        return parent

    @staticmethod
    def _next(conn,parent,version,*,payload=True):
        columns='o.*,b.result,coalesce(h.cursor,0) AS cursor' if payload else '1'
        return conn.execute('''SELECT '''+columns+''' FROM dd_agent.assembly_live_receipts o
            JOIN dd_agent.assembly_batches b ON b.document_id=o.document_id
              AND b.policy_version=o.assembly_policy AND b.generation=o.receipt_generation
            LEFT JOIN dd_agent.assembly_handoffs h ON h.document_id=o.document_id
              AND h.assembly_policy=o.assembly_policy AND h.receipt_generation=o.receipt_generation
              AND h.job_id=%s AND h.run_epoch=%s AND h.index_version=%s
            WHERE o.document_id=%s AND o.assembly_policy=%s AND (h.state IS NULL OR h.state='pending')
              AND o.receipt_generation>=coalesce((SELECT max(r.receipt_generation)
                FROM dd_agent.assembly_receipt_outbox r WHERE r.document_id=o.document_id
                AND r.assembly_policy=o.assembly_policy AND r.is_reset),0)
            ORDER BY o.position LIMIT 1''',
            (parent['id'],parent['run_epoch'],version,parent['document_id'],VERSION)).fetchone()

    @staticmethod
    def _receipt(row):
        result=row['result'];state=result.get('state');parents=result.get('parents',[])
        if result.get('generation')!=row['receipt_generation'] or len(canonical(result).encode('utf-8'))>8*1024*1024 or len(parents)>2000:
            raise ValueError('收据身份或容量不符。')
        if bool(row['is_reset'])!=(state is None):raise ValueError('收据重置标记不符。')
        if state is not None:
            body={k:v for k,v in state.items() if k!='checksum'}
            if state.get('version')!=VERSION or state.get('checksum')!=digest(body):raise ValueError('收据检查点损坏。')
        if not 0<=row['cursor']<=len(parents):raise ValueError('交接游标超出收据范围。')
        return parents,AssemblyStore._dependencies(result)

    @staticmethod
    def _latest_reset(conn,document_id):
        return conn.execute('''SELECT coalesce(max(receipt_generation),0) AS generation
            FROM dd_agent.assembly_receipt_outbox WHERE document_id=%s AND assembly_policy=%s AND is_reset''',
            (document_id,VERSION)).fetchone()['generation']

    def _sources(self,conn,parent,row,dependencies):
        document=AssemblyStore._document(conn,parent['project_id'],parent['document_id'],lock=True)
        if not self._current(conn,parent['document_id'],row['receipt_generation']):return False
        AssemblyStore._validate(conn,parent['project_id'],parent['document_id'],document,dependencies,[])
        state=row['result'].get('state')
        if state and state.get('closed') and state['next_page']!=document['page_count']+1:
            raise AssemblyStale('材料页数改变，旧结束收据不可交接。')
        return True

    def _old_part(self,parent,row,version):
        # 不复用任务状态或 owner。不同 job 的相同分段去重后最多取两个，
        # 用来检测歧义；不能按“最新任务”任意挑一个大正文清单。
        # work 的 document/version 索引与 parts 的 build 索引限制查找范围。
        with self.database.connect() as conn:
            rows=conn.execute('''SELECT DISTINCT p.end_parent,p.build_id,w.plan_seal,
                    w.document_id,w.index_version,w.input_revision
                FROM dd_agent.assembly_handoff_parts p
                JOIN dd_agent.parent_index_work w ON w.id=p.build_id
                JOIN dd_agent.processing_jobs j ON j.id=p.job_id
                WHERE w.document_id=%s AND w.index_version=%s
                  AND j.project_id=%s AND j.document_id=w.document_id
                  AND p.index_version=w.index_version AND p.document_id=w.document_id AND p.assembly_policy=%s
                  AND p.receipt_generation=%s AND p.start_parent=%s
                  AND dd_agent.assembly_receipt_current(p.document_id,p.assembly_policy,p.receipt_generation)
                ORDER BY p.build_id,p.end_parent LIMIT 2''',
                (parent['document_id'],version,parent['project_id'],VERSION,row['receipt_generation'],row['cursor'])).fetchall()
            if len(rows)>1:
                count('assembly.handoff.plan_ambiguous');return None
            if not rows:return None
            # 先在小元数据上去重和检测歧义，再取唯一候选的正文，避免
            # 多个历史 job 导致数据库反复排序／传输同一大 JSON 清单。
            saved=conn.execute('SELECT plan FROM dd_agent.parent_index_work WHERE id=%s',
                (rows[0]['build_id'],)).fetchone()
            return dict(rows[0],plan=saved['plan']) if saved else None

    def _validate_reuse(self,parent,row,parents,version,old):
        """事务外复验不可变计划；缓存命中不能替代来源／代次的提交复验。"""
        end=old['end_parent'];plan=old['plan']
        if type(end) is not int or not row['cursor']<end<=min(len(parents),row['cursor']+20):
            raise ValueError('历史分段游标无效。')
        self.index._verify(plan)
        selected=parents[row['cursor']:end]
        if plan['parents']!=selected or plan['vectors'] is not None:
            raise ValueError('历史分段与收据不符。')
        dependencies={}
        for block in selected:
            for dep in block['dependencies']:
                number=dep['page_number']
                if number in dependencies and dependencies[number]!=dep:raise ValueError('父块存在冲突来源。')
                dependencies[number]=dep
        deps=[dependencies[n] for n in sorted(dependencies)]
        if not 1<=len(deps)<=20:raise ValueError('历史分段来源超出范围。')
        revision=digest(dict(dependencies=deps,parents=[p['id'] for p in selected]))
        identity=digest(dict(document_id=str(parent['document_id']),index_version=version,input_revision=revision))
        if (plan['id'],plan['project_id'],plan['document_id'],plan['index_version'],plan['input_revision'],
                plan['dependencies'],plan['seal'])!=(identity,str(parent['project_id']),str(parent['document_id']),
                version,revision,deps,old['plan_seal']):raise ValueError('历史清单身份或依赖不符。')
        if (old['build_id'],str(old['document_id']),old['index_version'],old['input_revision'])!=(
                identity,plan['document_id'],version,revision):raise ValueError('历史清单登记不符。')
        # seal 之外再核对派生文字／坐标／身份，不能让不一致的子块进入
        # 新 job。仅做有界纯计算，复用已验证 token 长度，不重新启动分词器。
        children=plan['children'];lookup={p['id']:p for p in selected};ends={};ordinals={}
        if not isinstance(children,list) or not 1<=len(children)<=128:raise ValueError('历史子块数量不符。')
        for child in children:
            block=lookup[child['parent_id']];start=child['start'];stop=child['end']
            if (type(start) is not int or type(stop) is not int or not 0<=start<stop<=len(block['text'])
                    or start>ends.get(block['id'],0) and block['text'][ends.get(block['id'],0):start].strip()
                    or child['ordinal']!=ordinals.get(block['id'],0)
                    or child['text']!=block['text'][start:stop]):raise ValueError('历史子块文字或范围不符。')
            spans,separators=_clip(block,start,stop)
            if child['spans']!=spans or child['separators']!=separators:raise ValueError('历史子块来源不符。')
            for key,maximum in (('embedding_tokens',4096),('reranker_tokens',2048)):
                if type(child[key]) is not int or not 1<=child[key]<=maximum:raise ValueError('历史 token 预算无效。')
            if child['id']!=digest(dict(index_version=version,**{k:v for k,v in child.items() if k!='id'})):
                raise ValueError('历史子块身份不符。')
            # 原切块器会去掉边缘空白，token 二分也可能保留重叠小块；
            # 只拒绝实际正文缺口，不能因此把合法计划误判为缓存损坏。
            ends[block['id']]=max(stop,ends.get(block['id'],0));ordinals[block['id']]=child['ordinal']+1
        if any(p['id'] not in ends or p['text'][ends[p['id']]:].strip() for p in selected):
            raise ValueError('历史子块覆盖不完整。')
        return plan,end

    @staticmethod
    def _current(conn,document,generation):
        return conn.execute('SELECT dd_agent.assembly_receipt_current(%s,%s,%s) AS valid',(document,VERSION,generation)).fetchone()['valid']

    def _snapshots(self,parent,parents):
        dependencies={}
        for block in parents:
            for dep in block['dependencies']:
                number=dep['page_number']
                if number in dependencies and dependencies[number]!=dep:raise ValueError('父块存在冲突来源。')
                dependencies[number]=dep
        if len(dependencies)>20:raise ParentBatchTooLarge('本段引用和边界页超过 20。')
        with self.database.connect() as conn:
            rows={r['page_number']:r for r in conn.execute('''SELECT page_number,text,source_revision
                FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=ANY(%s)''',
                (parent['document_id'],list(dependencies))).fetchall()}
        snapshots=[]
        for number,dep in sorted(dependencies.items()):
            source=rows.get(number)
            if dep['readiness']=='ready' and (source is None or str(source['source_revision'])!=dep['source_revision']):
                raise AssemblyStale('交接准备期间来源改变。')
            snapshots.append(PageSnapshot(str(parent['project_id']),str(parent['document_id']),number,
                dep['source_revision'],source['text'] if dep['readiness']=='ready' else '',dep['readiness'],dep['quality']))
        return snapshots

    def _prepare(self,parent,row,parents,version):
        old=self._old_part(parent,row,version)
        if old:
            try:prepared=self._validate_reuse(parent,row,parents,version,old)
            except (ValueError,KeyError,TypeError,AttributeError):
                # 仅把不兼容缓存当 miss；来源过期、失去领取等异常仍向上传递。
                count('assembly.handoff.plan_invalid')
            else:
                count('assembly.handoff.plan_reused');return prepared
        count('assembly.handoff.plan_prepared')
        size=min(20,len(parents)-row['cursor'])
        while size:
            selected=parents[row['cursor']:row['cursor']+size]
            try:
                snapshots=self._snapshots(parent,selected)
                plan=self.index.prepare(parent['project_id'],parent['document_id'],selected,snapshots)
                self.index._verify(plan)
                if plan['index_version']!=version:raise ValueError('索引配置在准备期间改变，请重新对账。')
                return plan,row['cursor']+size
            except ParentBatchTooLarge:
                if size==1:raise
                size//=2
        raise ValueError('收据没有可准备的父块。')

    @staticmethod
    def _key(parent,row,version):
        return (parent['id'],parent['run_epoch'],version,parent['document_id'],VERSION,row['receipt_generation'])

    def _ledger(self,conn,parent,row,version):
        key=self._key(parent,row,version)
        conn.execute('''INSERT INTO dd_agent.assembly_handoffs
            (job_id,run_epoch,index_version,document_id,assembly_policy,receipt_generation,project_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(*key,parent['project_id']))
        return conn.execute('''SELECT cursor,state FROM dd_agent.assembly_handoffs
            WHERE job_id=%s AND run_epoch=%s AND index_version=%s AND document_id=%s
            AND assembly_policy=%s AND receipt_generation=%s FOR UPDATE''',key).fetchone()

    def _commit(self,job_id,epoch,row,version,dependencies,*,plan=None,end=None,stale=False):
        with self.database.connect() as conn:
            parent=self._parent(conn,job_id,epoch,exclusive=True)
            # 阅读锁封住 reset 与缺页插入；随后才锁交接账本。
            AssemblyStore._document(conn,parent['project_id'],parent['document_id'],lock=True)
            if not self._current(conn,parent['document_id'],row['receipt_generation']):
                return dict(outcome='superseded')
            if not stale:self._sources(conn,parent,row,dependencies)
            ledger=self._ledger(conn,parent,row,version)
            if ledger['state']!='pending' or ledger['cursor']!=row['cursor']:return dict(outcome='conflict')
            key=self._key(parent,row,version);task=None
            if stale:state='stale';next_cursor=row['cursor']
            elif plan is None:state='empty';next_cursor=row['cursor']
            else:
                if (plan['project_id'],plan['document_id'],plan['index_version'])!=(str(parent['project_id']),str(parent['document_id']),version):
                    raise ValueError('工作包与交接材料／版本不符。')
                # 已按当前收据在锁外验证／重算，并在本事务复验来源。
                # 允许修复同一确定性身份下损坏的准备副本，不修改已发布历史。
                task=self.index._register_transaction(self.stages,conn,job_id,plan,expected_epoch=epoch,
                    repair_preparation=True)
                conn.execute('''INSERT INTO dd_agent.assembly_handoff_parts
                    (job_id,run_epoch,index_version,document_id,assembly_policy,receipt_generation,
                     start_parent,end_parent,build_id,stage_job_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (*key,row['cursor'],end,plan['id'],task['id']))
                # 新 origin 与登记同事务生效。只重新排队因 origin 退役取消的
                # 同源工作，保留向量检查点；旧 owner 永不复活，须重新领取。
                revived=conn.execute('''UPDATE dd_agent.stage_jobs SET state='pending',owner_token=NULL,
                    error_code='',available_at=clock_timestamp(),updated_at=clock_timestamp()
                    WHERE id=%s AND state='cancelled' AND error_code='origin_retired' RETURNING *''',(task['id'],)).fetchone()
                if revived:task=dict(revived,reused=False)
                next_cursor=end;state='delivered' if end==len(row['result']['parents']) else 'pending'
            conn.execute('''UPDATE dd_agent.assembly_handoffs SET cursor=%s,state=%s,error_code=%s,updated_at=clock_timestamp()
                WHERE job_id=%s AND run_epoch=%s AND index_version=%s AND document_id=%s
                AND assembly_policy=%s AND receipt_generation=%s''',
                (next_cursor,state,'source_changed' if stale else '',*key))
            return dict(outcome=state,task=task)

    @timed('assembly.handoff')
    def reconcile(self,job_id,*,expected_epoch,limit=4,checkpoint=None):
        if type(expected_epoch) is not int or expected_epoch<1 or type(limit) is not int or not 1<=limit<=20:
            raise ValueError('交接代次须为正整数，每轮限制为 1–20。')
        version=self.index.version()
        result=dict(processed=0,registered=0,delivered=0,empty=0,stale=0,superseded=0,conflicts=0,tasks=[])
        for _ in range(limit):
            with self.database.connect() as conn:
                parent=self._parent(conn,job_id,expected_epoch)
                row=self._next(conn,parent,version)
            if row is None:break
            parents,dependencies=self._receipt(row)
            number=min((d['page_number'] for d in dependencies),default=1)
            if checkpoint:checkpoint('assembly_handoff',number)
            try:
                with self.database.connect() as conn:
                    current=self._parent(conn,job_id,expected_epoch)
                    valid=self._sources(conn,current,row,dependencies)
                if not valid:outcome=dict(outcome='superseded')
                elif not parents:outcome=self._commit(job_id,expected_epoch,row,version,dependencies)
                else:
                    plan,end=self._prepare(parent,row,parents,version)
                    if checkpoint:checkpoint('assembly_handoff',number)
                    outcome=self._commit(job_id,expected_epoch,row,version,dependencies,plan=plan,end=end)
            except AssemblyStale:
                outcome=self._commit(job_id,expected_epoch,row,version,dependencies,stale=True)
            result['processed']+=1
            name=outcome['outcome']
            if name=='conflict':result['conflicts']+=1
            elif name in ('delivered','empty','stale','superseded'):result[name]+=1
            if outcome.get('task'):
                result['registered']+=1;result['tasks'].append(outcome['task']['id'])
        with self.database.connect() as conn:
            parent=self._parent(conn,job_id,expected_epoch)
            result['has_more']=self._next(conn,parent,version,payload=False) is not None
        count('assembly.handoff.registered',result['registered'])
        return result
