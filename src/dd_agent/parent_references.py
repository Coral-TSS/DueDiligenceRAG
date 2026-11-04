"""版本绑定的父块引用读取；无模型加载，供 HTTP 与后续工具复用。"""
import re
from uuid import UUID
from .parent_queries import ParentQueries


class ParentReferenceError(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__('引用不存在或不属于当前项目。' if code=='not_found'
                         else '引用来源或索引版本已失效，请重新检索。')


def reference_url(project_id,document_id,build_id,parent_id):
    return f'/api/projects/{project_id}/documents/{document_id}/parent-builds/{build_id}/parents/{parent_id}'


class ParentReferences:
    def __init__(self,database):
        self.database=database
        self.queries=ParentQueries(database)

    def read(self,project_id,document_id,build_id,parent_id,index_version,*,offset=0,limit=4000):
        project_id,document_id=UUID(str(project_id)),UUID(str(document_id))
        if any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None
               for v in (build_id,parent_id,index_version)):
            raise ValueError('引用身份或版本格式无效。')
        if type(offset) is not int or not 0<=offset<=4000 or type(limit) is not int or not 1<=limit<=4000:
            raise ValueError('父块读取偏移／字符预算无效。')
        with self.database.connect() as conn:
            # 身份可见性与来源有效性使用同一快照；越项目始终 404，不能用失效
            # 状态探测其他项目。旧引用可以定位，但源修订变化后绝不自动跳新块。
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            exists=conn.execute('''SELECT 1 FROM dd_agent.parent_index_parents p
                JOIN dd_agent.parent_index_builds b ON b.id=p.build_id
                JOIN dd_agent.documents d ON d.id=b.document_id
                WHERE d.project_id=%s AND d.id=%s AND b.id=%s AND p.id=%s''',
                (project_id,document_id,build_id,parent_id)).fetchone()
            if not exists:raise ParentReferenceError('not_found')
            row=self.queries.context(project_id,document_id,index_version,build_id,parent_id,
                offset=offset,limit=limit,conn=conn)
            if row is None:raise ParentReferenceError('stale_reference')
        continuation=None
        if row['next_offset'] is not None:
            continuation=dict(url=reference_url(project_id,document_id,build_id,parent_id),
                params=dict(index_version=index_version,offset=row['next_offset']))
        return dict(row,index_kind='parent_child',snapshot='version_pinned',continuation=continuation)
