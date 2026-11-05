"""显式的父子查询服务；尚不替换默认页级入口。

复用 EvidenceService 的纯模型缓存与本地模型客户端。来源不进入缓存，
每次召回后、重排后均通过数据库重新确认，返回的是证据候选而非事实结论。
"""
import math
from time import monotonic
from uuid import UUID

from .lexical import build_keyword_query, fuse_for, prepare_keyword_rows
from .model_admission import request_scope,current_control,AdmissionError
from .parent_queries import ParentQueries
from .parent_references import reference_url
from .retrieval import valid_vector,RetrievalError,RetrievalBusy,SearchCancelled,SearchTimeout


class ParentSearch:
    def __init__(self,evidence,index):
        # 索引版本必须与查询向量来自同一模型。调用方共享实例，避免无意混用向量空间。
        if evidence.database is not index.database or evidence.models is not index.models:
            raise ValueError('父子查询必须共享数据库及模型实例。')
        self.evidence=evidence
        self.index=index
        self.queries=ParentQueries(evidence.database)

    def search(self,project_id,query,*,cancel=None):
        project_id=UUID(str(project_id));query=query.strip()
        if not 1<=len(query)<=500:raise ValueError('查询须为 1–500 个字符。')
        try:
            with request_scope('interactive',cancel=cancel,
                    deadline=monotonic()+self.evidence.settings.search_timeout_seconds):
                return self._search(project_id,query)
        except AdmissionError as exc:
            if exc.code=='cancelled':raise SearchCancelled('查询已取消。') from None
            if exc.code=='deadline':raise SearchTimeout('本次查询超时，请稍后重试。') from None
            raise RetrievalBusy('本地模型繁忙，请稍后重试。') from None

    def _search(self,project_id,query):
        control=current_control();control.check()
        version=self.index.version();control.check()
        def embed():
            values=self.evidence.models.embed([query],query=True)
            if len(values)!=1:raise RetrievalError('查询向量数量不符。')
            return values[0]
        settings=self.evidence.settings
        vector_rows=[]
        if settings.retrieval_mode!='keyword':
            vector=self.evidence._model_result('embedding',project_id,query,[],embed,valid_vector)
            vector_rows=self.queries.candidates(project_id,version,vector,limit=settings.retrieval_vector_limit)
        keyword_rows=[]
        if settings.retrieval_mode!='vector':
            plan=build_keyword_query(query)
            if plan.tsquery:
                found=self.queries.keyword_candidates(project_id,version,plan.tsquery,plan.needles,limit=settings.retrieval_keyword_limit)
                keyword_rows=prepare_keyword_rows(found,plan)[:settings.retrieval_keyword_limit]
        rows=fuse_for(settings,vector_rows,keyword_rows,key=lambda row:('parent',str(row['build_id']),str(row['id'])))
        control.check()
        def validate(scores):
            if not isinstance(scores,list) or len(scores)!=len(rows) or any(
                    type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in scores):
                raise RetrievalError('重排序输出无效，请重试。')
        scores=[]
        if rows:
            texts=[r['text'] for r in rows]
            scores=self.evidence._model_result('reranker',project_id,query,texts,
                lambda:self.evidence.models.rerank(query,texts),validate)
        control.check()
        ranked={(r['build_id'],r['id']):(r,score) for r,score in zip(rows,scores,strict=True)}
        live=self.queries.finalize(project_id,version,rows);control.check()
        results=self.format(project_id,version,ranked,live)
        return dict(index_kind='parent_child',index_version=version,
            coverage=dict(complete=False,status='not_evaluated'),
            status='candidates' if results else 'no_matches',results=results)

    def format(self,project_id,version,ranked,live):
        """将已评分且最终复验的子块聚合；混合查询复用同一来源／上下文规则。"""
        groups={}
        for row in live:
            original,score=ranked[(row['build_id'],row['id'])]
            # 不仅确认 ID 存在；当前库中子块内容／归属必须与模型实际评分输入一致。
            if any(row[k]!=original[k] for k in ('text','parent_id','document_id','source_spans')):
                continue
            key=(row['build_id'],row['parent_id'])
            group=groups.setdefault(key,dict(build_id=key[0],parent_id=key[1],
                document_id=row['document_id'],matched_children=[],
                reference=dict(url=reference_url(project_id,row['document_id'],key[0],key[1]),
                    params=dict(index_version=version)),
                context=dict(text=row['parent_text'],offset=0,next_offset=None,
                    source_spans=self._links(project_id,row['document_id'],row['parent_spans']),
                    separators=row['separators'],boundary=row['boundary'],continuation_from=row['continuation_from'])))
            group['matched_children'].append(dict(id=row['id'],text=row['text'],
                start_offset=row['start_offset'],end_offset=row['end_offset'],rerank_score=score,
                similarity=original['similarity'],recall=original.get('recall'),
                source_spans=self._links(project_id,row['document_id'],row['source_spans'])))
        for group in groups.values():
            group['matched_children'].sort(key=lambda c:(-c['rerank_score'],-c['similarity'],c['id']))
            best=group['matched_children'][0]
            group.update(rerank_score=best['rerank_score'],similarity=best['similarity'],recall=best.get('recall'))
        results=sorted(groups.values(),key=lambda g:(-g['rerank_score'],-g['similarity'],g['build_id'],g['parent_id']))[:5]
        return results

    @staticmethod
    def _links(project_id,document_id,spans):
        # 阅读链接用于导航；source_revision 与范围仍作为可验证引用保留。
        return [dict(s,source_url=f"/reader?project={project_id}&document={document_id}&page={s['page_number']}") for s in spans]
