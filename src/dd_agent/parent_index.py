"""独立父子索引的有界发布基础；不切换活动调度或查询。

prepare→登记／领取阶段→embed→publish。只有短 SQL 发布在阶段保护内；
模型、tokenizer、组装都在事务外。历史 build 保留，来源有效性须另行复验。
"""
from copy import deepcopy
from uuid import UUID

from psycopg.types.json import Jsonb

from .assembly import VERSION as ASSEMBLY_VERSION,canonical,digest
from .assembly_store import AssemblyStore
from .config import Settings
from .db import Database
from .performance import timed,count
from .lexical import document_tsvector
from .retrieval import LocalModels,split_text,valid_vector
from .stage_publication import StageSuperseded
from .token_budget import LocalTokenBudget

CHILD_VERSION='parent-child-700-overlap80-token-4096-2048-v1'


class ParentBatchTooLarge(ValueError):
    """调用者可在有限次数内减小父块批次重试，不截断原文。"""


def _seal(value):
    value['seal']=digest(value)
    return value


def _check(value):
    if len(canonical(value).encode('utf-8'))>12*1024*1024:raise ParentBatchTooLarge('父子发布清单超过容量。')
    if digest({k:v for k,v in value.items() if k!='seal'})!=value.get('seal'):
        raise ValueError('父子发布清单已改变。')


def stage_binding(plan):
    """重建 StageJobs 的精确输入绑定；登记、恢复与发布共用同一规则。"""
    deps=[dict(page_number=d['page_number'],view='effective',revision=d['source_revision'])
          for d in plan['dependencies'] if d['readiness']=='ready']
    scope=dict(pages=[d['page_number'] for d in deps],parent_build=plan['id'])
    revision=digest(dict(stage='embedding',scope_key=digest(scope),source=plan['input_revision'],
        policy=plan['index_version'],dependencies=deps,predecessors=[]))
    return scope,deps,revision


def _clip(parent,start,end):
    """将父块坐标裁剪、平移到子块；分隔符只继承派生位置，不伪造原文。"""
    spans=[];separators=[]
    for source in parent['spans']:
        left=max(start,source['derived_start']);right=min(end,source['derived_end'])
        if left>=right:continue
        offset=left-source['derived_start']
        spans.append(dict(source,start=source['start']+offset,end=source['start']+offset+right-left,
                          derived_start=left-start,derived_end=right-start))
    for separator in parent['separators']:
        left=max(start,separator['start']);right=min(end,separator['end'])
        if left<right:separators.append(dict(start=left-start,end=right-start,text=parent['text'][left:right]))
    return spans,separators


def _validate_parent(parent,snapshots):
    body={k:v for k,v in parent.items() if k!='id'}
    if parent.get('id')!=digest(dict(version=ASSEMBLY_VERSION,**body)):raise ValueError('父块身份不符。')
    text=parent['text']
    if not 1<=len(text)<=4000 or not parent['spans'] or len({s['page_number'] for s in parent['spans']})>3:
        raise ValueError('父块文字／页数超过边界。')
    ranges=[]
    for span in parent['spans']:
        page=snapshots.get(span['page_number'])
        if page is None or page.readiness!='ready' or (span['project_id'],span['document_id'],span['source_revision'])!=(page.project_id,page.document_id,page.source_revision):
            raise ValueError('父块引用不属于就绪快照。')
        a,b,c,d=(span[k] for k in ('start','end','derived_start','derived_end'))
        if any(type(v) is not int for v in (a,b,c,d)) or not 0<=a<b<=len(page.text) or not 0<=c<d<=len(text) or b-a!=d-c or page.text[a:b]!=text[c:d]:
            raise ValueError('父块原文范围不符。')
        ranges.append((c,d))
    for separator in parent['separators']:
        a,b=separator['start'],separator['end']
        if type(a) is not int or type(b) is not int or not 0<=a<b<=len(text) or text[a:b]!=separator['text'] or not separator['text'].isspace():
            raise ValueError('派生分隔符无效。')
        ranges.append((a,b))
    end=0
    for a,b in sorted(ranges):
        if a!=end:raise ValueError('原文范围重叠或有未映射文字。')
        end=b
    if end!=len(text):raise ValueError('父块存在未映射文字。')
    for dep in parent['dependencies']:
        page=snapshots.get(dep['page_number'])
        if page is None or dep!=page.dependency():raise ValueError('父块边界依赖与快照不符。')
    if not {s['page_number'] for s in parent['spans']}.issubset({d['page_number'] for d in parent['dependencies'] if d['readiness']=='ready'}):
        raise ValueError('父块依赖遗漏引用页。')
    order=[(s['page_number'],s['start'],s['derived_start']) for s in parent['spans']]
    if order!=sorted(order):raise ValueError('父块引用跨度不按阅读顺序。')


class ParentIndex:
    def __init__(self,settings=None,*,database=None,models=None,budget=None):
        self.settings=settings or Settings.from_env()
        self.database=database or Database(self.settings.database_url)
        self.models=models or LocalModels(self.settings)
        self._budget=budget

    @property
    def budget(self):
        # 默认混合查询先发现候选；没有父子成果时直接走页级路径，
        # 不应仅因创建查询服务就加载父子 tokenizer。
        if self._budget is None:
            self._budget=LocalTokenBudget(self.settings)
        return self._budget

    @budget.setter
    def budget(self,value):
        self._budget=value

    def version(self):
        return digest(dict(assembly=ASSEMBLY_VERSION,children=CHILD_VERSION,
                           embedding=self.models.profile(),tokenizer=self.budget.profile()))

    def _verify(self,plan):
        _check(plan)
        if plan['index_version']!=self.version():raise ValueError('模型或分块版本已变化，请重新准备父子块。')

    def _split(self,parents):
        parts=[]
        for parent in parents:
            parts.extend(dict(parent_id=parent['id'],start=c['start_offset'],end=c['end_offset'],text=c['text'])
                         for c in split_text(parent['text']))
        # 每次不合格块二分；一个字符仍超预算时拒绝，而不是截断原文。
        for _ in range(11):
            if len(parts)>128:raise ParentBatchTooLarge('单批子块超过 128，请减小父块批次。')
            if not parts:return []
            counts=self.budget.counts([c['text'] for c in parts])
            if len(counts)!=len(parts):raise ValueError('Tokenizer 数量不符。')
            pending=[];changed=False
            for part,tokens in zip(parts,counts,strict=True):
                if any(type(tokens.get(k)) is not int or tokens[k]<1 for k in ('embedding','reranker')):
                    raise ValueError('Tokenizer 返回无效长度。')
                if tokens['embedding']<=4096 and tokens['reranker']<=2048:
                    pending.append(dict(part,embedding_tokens=tokens['embedding'],reranker_tokens=tokens['reranker']))
                else:
                    if len(part['text'])<2:raise ValueError('单字符超过模型预算。')
                    middle=part['start']+len(part['text'])//2
                    half=middle-part['start']
                    pending.extend([dict(parent_id=part['parent_id'],start=part['start'],end=middle,text=part['text'][:half]),
                        dict(parent_id=part['parent_id'],start=middle,end=part['end'],text=part['text'][half:])])
                    changed=True
            parts=pending
            if not changed:return parts
        raise ValueError('无法在有限拆分次数内满足 token 预算。')

    @timed('parent_index.prepare')
    def prepare(self,project_id,document_id,parents,snapshots):
        project_id,document_id=str(UUID(str(project_id))),str(UUID(str(document_id)))
        parents=deepcopy(list(parents));snapshots=list(snapshots)
        if not 1<=len(snapshots)<=20 or len(parents)>20 or sum(len(p.text) for p in snapshots)>200000:
            raise ParentBatchTooLarge('父子索引批次超过范围。')
        pages={p.page_number:p for p in snapshots}
        if len(pages)!=len(snapshots) or any((p.project_id,p.document_id)!=(project_id,document_id) for p in snapshots):
            raise ValueError('快照范围或归属不符。')
        if not parents and any(p.readiness=='ready' and p.text.strip() for p in snapshots):
            raise ValueError('非空尾段尚未封口，不能发布空索引。')
        if len({p['id'] for p in parents})!=len(parents):raise ValueError('父块重复。')
        for parent in parents:_validate_parent(parent,pages)
        deps=[p.dependency() for p in sorted(snapshots,key=lambda p:p.page_number)]
        with self.database.connect() as conn:
            document=AssemblyStore._document(conn,project_id,document_id,lock=True)
            AssemblyStore._validate(conn,project_id,document_id,document,deps,snapshots)
            if any(p['boundary']=='document_end' and max(s['page_number'] for s in p['spans'])!=document['page_count'] for p in parents):
                raise ValueError('材料结束父块不在当前末页。')
        children=self._split(parents)
        lookup={p['id']:p for p in parents};ordinals={}
        version=self.version()
        for child in children:
            parent=lookup[child['parent_id']]
            child['ordinal']=ordinals.get(parent['id'],0);ordinals[parent['id']]=child['ordinal']+1
            child['spans'],child['separators']=_clip(parent,child['start'],child['end'])
            child['id']=digest(dict(index_version=version,**child))
        revision=digest(dict(dependencies=deps,parents=[p['id'] for p in parents]))
        identity=digest(dict(document_id=document_id,index_version=version,input_revision=revision))
        return _seal(dict(id=identity,project_id=project_id,document_id=document_id,index_version=version,
            input_revision=revision,parents=parents,children=children,dependencies=deps,vectors=None))

    def register(self,stages,job_id,plan,*,expected_epoch=None):
        self._verify(plan)
        with self.database.connect() as conn:
            return self._register_transaction(stages,conn,job_id,plan,expected_epoch=expected_epoch)

    def _register_transaction(self,stages,conn,job_id,plan,*,expected_epoch=None,repair_preparation=False):
        """已在事务外验证清单的内部入口；允许交接账本与登记原子提交。"""
        from .parent_work import persist_preparation
        if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch<1):
            raise ValueError('执行代次必须为正整数。')
        scope,deps,_=stage_binding(plan)
        if not deps:raise ValueError('无就绪输入页，请先保存组装缺口，不登记向量阶段。')
        parent=stages._parent(conn,job_id,exclusive=True)
        if expected_epoch is not None and (parent['run_epoch']!=expected_epoch or parent['status'] not in ('queued','running','pause_requested')):
            raise StageSuperseded('登记者执行代次已失效。')
        if (str(parent['project_id']),str(parent['document_id']))!=(plan['project_id'],plan['document_id']):
            raise ValueError('父任务与材料不匹配。')
        document=AssemblyStore._document(conn,plan['project_id'],plan['document_id'],lock=True)
        AssemblyStore._validate(conn,plan['project_id'],plan['document_id'],document,plan['dependencies'],[])
        persist_preparation(conn,plan,repair=repair_preparation)
        return stages.ensure(job_id,'embedding',scope,
            source_revision=plan['input_revision'],policy_version=plan['index_version'],dependencies=deps,conn=conn)

    @timed('parent_index.embed')
    def embed(self,plan):
        self._verify(plan)
        result=deepcopy(plan);result.pop('seal')
        # 无子块则不进入模型，仍可以发布空白就绪页的版本记录。
        vectors=self.models.embed([c['text'] for c in plan['children']]) if plan['children'] else []
        if len(vectors)!=len(plan['children']):raise ValueError('父子向量数量不符。')
        for vector in vectors:valid_vector(vector)
        # JSONB 会将 -0.0 规范为 0.0；先统一数值表示，避免恢复时误报摘要损坏。
        # 仅改变零的表示，不改变模型数值、维度或推理精度。
        result['vectors']=[[float(value) if value else 0.0 for value in vector] for vector in vectors]
        return _seal(result)

    @timed('parent_index.publish')
    def publish(self,stages,task_id,owner_token,plan):
        from .parent_work import manifest_seal,require_preparation
        from .assembly_handoff import require_origin
        self._verify(plan)
        expected_manifest=manifest_seal(plan)
        if not isinstance(plan['vectors'],list) or len(plan['vectors'])!=len(plan['children']):raise ValueError('父子向量尚未完成。')
        vectors=[valid_vector(v) for v in plan['vectors']]
        with self.database.connect() as conn:
            task=conn.execute('''SELECT s.*,j.run_epoch AS active_epoch FROM dd_agent.stage_jobs s
                JOIN dd_agent.processing_jobs j ON j.id=s.job_id WHERE s.id=%s''',(task_id,)).fetchone()
            require_origin(conn,plan['id'])
        # stage_jobs 的策略已封入 input_revision；scope 精确绑定本 build，不能串用别的领取。
        if not task or task['stage']!='embedding' or (str(task['project_id']),str(task['document_id']))!=(plan['project_id'],plan['document_id']) or task['scope'].get('parent_build')!=plan['id']:
            raise ValueError('阶段任务与父子发布批次不符。')
        scope,_,expected=stage_binding(plan)
        if task['scope']!=scope or task['input_revision']!=expected:
            raise ValueError('阶段来源／策略绑定不符。')
        if task['owner_token']!=UUID(str(owner_token)):raise StageSuperseded('发布者不再拥有阶段。')
        if task['run_epoch']!=task['active_epoch']:raise StageSuperseded('旧执行代次不能重放发布。')
        if task['state']=='succeeded':
            meta=self.metadata(plan['project_id'],plan['document_id'],plan['id'])
            if task['result_ref']==plan['id'] and meta and meta['current']:return plan['id']
            raise StageSuperseded('旧发布已失效。')
        def save(conn):
            require_origin(conn,plan['id'])
            require_preparation(conn,plan,expected_manifest,computed=True)
            document=AssemblyStore._document(conn,plan['project_id'],plan['document_id'],lock=True)
            AssemblyStore._validate(conn,plan['project_id'],plan['document_id'],document,plan['dependencies'],[])
            inserted=conn.execute('''INSERT INTO dd_agent.parent_index_builds(id,document_id,index_version,input_revision)
                VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id''',
                (plan['id'],plan['document_id'],plan['index_version'],plan['input_revision'])).fetchone()
            if inserted is None:return
            for parent in plan['parents']:
                conn.execute('''INSERT INTO dd_agent.parent_index_parents
                    (build_id,id,text,source_spans,separators,dependencies,boundary,continuation_from)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (plan['id'],parent['id'],parent['text'],Jsonb(parent['spans']),Jsonb(parent['separators']),
                     Jsonb(parent['dependencies']),parent['boundary'],parent['continuation_from']))
            for child,vector in zip(plan['children'],vectors,strict=True):
                conn.execute('''INSERT INTO dd_agent.parent_index_children
                    (build_id,parent_id,id,ordinal,start_offset,end_offset,text,source_spans,separators,embedding_tokens,reranker_tokens,embedding,lexemes)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::public.vector,%s::tsvector)''',
                    (plan['id'],child['parent_id'],child['id'],child['ordinal'],child['start'],child['end'],child['text'],
                     Jsonb(child['spans']),Jsonb(child['separators']),child['embedding_tokens'],child['reranker_tokens'],vector,document_tsvector(child['text'])))
            for dep in plan['dependencies']:
                conn.execute('''INSERT INTO dd_agent.parent_index_dependencies
                    (build_id,document_id,page_number,source_revision,readiness,quality) VALUES(%s,%s,%s,%s,%s,%s)''',
                    (plan['id'],plan['document_id'],dep['page_number'],dep['source_revision'],dep['readiness'],dep['quality']))
        if not stages.complete(task_id,owner_token,publish=save,result_ref=plan['id']):raise StageSuperseded('执行者或来源已失效。')
        count('parent_index.published')
        return plan['id']

    def metadata(self,project_id,document_id,identity):
        """有界版本状态，不返回父子正文；不是 06c 的证据补读接口。"""
        version=self.version()
        with self.database.connect() as conn:
            return conn.execute('''SELECT b.id,b.index_version,b.input_revision,
                (SELECT count(*) FROM dd_agent.parent_index_parents p WHERE p.build_id=b.id)::integer AS parents,
                (SELECT count(*) FROM dd_agent.parent_index_children c WHERE c.build_id=b.id)::integer AS children,
                b.index_version=%s AND dd_agent.parent_build_origin_current(b.id)
                AND NOT EXISTS(SELECT 1 FROM dd_agent.parent_index_dependencies s
                    LEFT JOIN dd_agent.effective_document_pages e ON e.document_id=s.document_id AND e.page_number=s.page_number
                    WHERE s.build_id=b.id AND NOT coalesce(s.page_number<=r.page_count AND
                        CASE WHEN s.readiness='missing' THEN e.page_number IS NULL AND s.source_revision='missing:'||r.pipeline_version
                        ELSE e.source_revision=s.source_revision AND e.verification=s.quality AND
                          (s.readiness='pending' OR s.readiness='ready' AND e.status='succeeded' OR s.readiness='failed' AND e.status='failed')
                        END,false)) AS current
                FROM dd_agent.parent_index_builds b JOIN dd_agent.documents d ON d.id=b.document_id
                JOIN dd_agent.document_readings r ON r.document_id=d.id
                WHERE d.project_id=%s AND d.id=%s AND b.id=%s''',(version,project_id,document_id,identity)).fetchone()
