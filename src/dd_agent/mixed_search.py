"""默认混合查询服务；无名单自动发现，显式名单可进一步限定范围。

两套索引共享同一 Embedding 模型，只在这个前提下合并向量候选；最终相关性
来自一次统一重排。材料整体选择，不以新增索引覆盖不全的子集替换旧材料。
"""
import math
from time import monotonic
from uuid import UUID
from .lexical import build_keyword_query, fuse_for, mixed_key, prepare_keyword_rows
from .model_admission import request_scope,current_control,AdmissionError
from .parent_coverage import ParentCoverage
from .parent_search import ParentSearch
from .search_queries import SearchQueries
from .retrieval import valid_vector,RetrievalError,RetrievalBusy,SearchCancelled,SearchTimeout


class MixedSearch:
    def __init__(self,evidence,index,documents):
        if not isinstance(documents,tuple) or len(documents)>20:
            raise ValueError('查询试运行名单须为最多 20 个 UUID。')
        try:documents=tuple(str(UUID(str(v))) for v in documents)
        except (ValueError,TypeError,AttributeError):raise ValueError('查询名单 UUID 无效。') from None
        if len(set(documents))!=len(documents):raise ValueError('查询名单不能重复。')
        self.parent=ParentSearch(evidence,index)
        self.evidence,self.index,self.documents=evidence,index,documents
        self.coverage=ParentCoverage(evidence.database)
        self.pages=SearchQueries(evidence.database)

    def search(self,project_id,query,*,cancel=None):
        project_id=UUID(str(project_id));query=query.strip()
        if not 1<=len(query)<=500:raise ValueError('查询须为 1–500 个字符。')
        try:
            with request_scope('interactive',cancel=cancel,
                    deadline=monotonic()+self.evidence.settings.search_timeout_seconds):
                return self._search(project_id,query,document_id=None)
        except AdmissionError as exc:
            if exc.code=='cancelled':raise SearchCancelled('查询已取消。') from None
            if exc.code=='deadline':raise SearchTimeout('本次查询超时，请稍后重试。') from None
            raise RetrievalBusy('本地模型繁忙，请稍后重试。') from None

    def _search(self,project_id,query,*,document_id=None):
        control=current_control();control.check()
        documents=self.documents;limited=False;project_checked=False
        if not documents:
            discovered=self.coverage.discover(project_id);control.check()
            if discovered is None:raise RetrievalError('项目不存在。')
            documents=discovered['documents'];limited=discovered['limited'];project_checked=True
        if not documents:
            response=self.evidence._search(project_id,query,project_checked=True,document_id=document_id)
            for hit in response['results']:hit['index_kind']='page'
            return dict(response,index_kind='page',parent_documents=[],selection_changed=False,parent_selection_limited=False)
        if not project_checked and not self.evidence.database.project_exists(project_id):raise RetrievalError('项目不存在。')
        parent_version=self.index.version();page_version=self.evidence.version();control.check()
        selected=[]
        for doc in documents:
            control.check();state=self.coverage.inspect(project_id,doc,parent_version)
            if state and state['eligible']:selected.append(doc)
        # 材料过滤只改召回范围：页级仍排除已走父子索引的材料，融合和重排不另写。
        page_included=None
        parent_selected=selected
        if document_id is not None:
            only=UUID(str(document_id))
            if self.evidence.database.document(project_id,only) is None:
                raise RetrievalError('材料不存在或不属于当前项目。')
            page_included=(only,)
            parent_selected=tuple(doc for doc in selected if str(doc)==str(only))
        settings=self.evidence.settings
        vector_rows=[]
        if settings.retrieval_mode!='keyword':
            def embed():
                values=self.evidence.models.embed([query],query=True)
                if len(values)!=1:raise RetrievalError('查询向量数量不符。')
                return values[0]
            vector=self.evidence._model_result('embedding',project_id,query,[],embed,valid_vector)
            old=self.pages.candidates(project_id,page_version,valid_vector(vector),excluded_documents=selected,
                included_documents=page_included,limit=settings.retrieval_vector_limit)
            children=self.parent.queries.candidates(project_id,parent_version,vector,documents=parent_selected,
                current_receipts_only=True,limit=settings.retrieval_vector_limit) if parent_selected else []
            # 两套索引先按相似度各取有界名单，再合并到该路上限。距离只用于候选选择。
            vector_rows=sorted([dict(r,index_kind='page') for r in old]+[dict(r,index_kind='parent_child') for r in children],
                key=lambda r:(-r['similarity'],r['index_kind'],str(r['id'])))[:settings.retrieval_vector_limit]
        keyword_rows=[]
        if settings.retrieval_mode!='vector':
            plan=build_keyword_query(query)
            if plan.tsquery:
                old=self.pages.keyword_candidates(project_id,page_version,plan.tsquery,plan.needles,
                    excluded_documents=selected,included_documents=page_included,limit=settings.retrieval_keyword_limit)
                children=self.parent.queries.keyword_candidates(project_id,parent_version,plan.tsquery,plan.needles,
                    documents=parent_selected,current_receipts_only=True,limit=settings.retrieval_keyword_limit) if parent_selected else []
                keyword_rows=prepare_keyword_rows([dict(r,index_kind='page') for r in old]+[dict(r,index_kind='parent_child') for r in children],plan)
                keyword_rows=keyword_rows[:settings.retrieval_keyword_limit]
        # 融合后再交给一次重排，重排条数仍受 candidate_limit 限制，默认不超过原来的 12 条。
        rows=fuse_for(settings,vector_rows,keyword_rows,key=mixed_key)
        def validate(scores):
            if not isinstance(scores,list) or len(scores)!=len(rows) or any(
                    type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in scores):
                raise RetrievalError('重排序输出无效，请重试。')
        if rows:
            texts=[r['text'] for r in rows]
            scores=self.evidence._model_result('reranker',project_id,query,texts,
                lambda:self.evidence.models.rerank(query,texts),validate)
            for row,score in zip(rows,scores,strict=True):row['rerank_score']=score
        control.check()
        old=[r for r in rows if r['index_kind']=='page']
        children=[r for r in rows if r['index_kind']=='parent_child']
        with self.evidence.database.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            # 最终资格、旧索引来源、父子来源和覆盖计数均来自同一快照。
            # 失效材料不临时混入未经本次评分的旧片段，提示重试下一次查询。
            valid=[];parent_pages=0
            for doc in selected:
                state=self.coverage.inspect(project_id,doc,parent_version,conn=conn)
                if state and state['eligible']:valid.append(doc);parent_pages+=state['covered_pages']
            coverage,live_old=self.pages.finalize(project_id,page_version,[r['id'] for r in old],
                conn=conn,excluded_documents=selected)
            live_children=self.parent.queries.finalize(project_id,parent_version,children,
                conn=conn,current_receipts_only=True)
            names=self.pages.names(project_id,[r['document_id'] for r in live_children],conn=conn)
        control.check()
        live_children=[r for r in live_children if str(r['document_id']) in valid]
        ranked={(r['build_id'],r['id']):(r,r['rerank_score']) for r in children}
        results=[dict(r,index_kind='parent_child') for r in self.parent.format(project_id,parent_version,ranked,live_children)]
        for result in results:
            result.update(names.get(result['document_id'],dict(filenames=[],sources_truncated=False)))
        for row in old:
            if row['id'] not in live_old:continue
            row.update(live_old[row['id']])
            row['source_url']=f"/reader?project={project_id}&document={row['document_id']}&page={row['page_number']}"
            results.append(row)
        # 同一材料可能在推理期间重建完毕，最终仍具备资格，但本次评分的
        # 旧 child 已退出当前谱系。资格相同不代表候选快照相同。
        changed=valid!=selected or len(live_children)!=len(children)
        indexed=coverage['indexed_pages']+parent_pages
        coverage=dict(coverage,indexed_pages=indexed,complete=bool(indexed) and
            indexed==coverage['total_pages'] and not coverage['unread_documents'] and not changed)
        results=sorted(results,key=lambda r:(-r['rerank_score'],-r['similarity'],
            str(r['document_id']),r['index_kind'],str(r.get('id',r.get('parent_id')))))[:5]
        return dict(index_kind='mixed',index_version=page_version,parent_index_version=parent_version,
            parent_documents=valid,selection_changed=changed,coverage=coverage,results=results,
            parent_selection_limited=limited,
            status='candidates' if results else 'not_indexed' if not indexed else 'no_matches')
