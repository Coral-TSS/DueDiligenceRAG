"""父子索引查询仓储。每次 SQL 都核实来源；不加载模型或切换活动查询。"""
from .retrieval import valid_vector
from .search_queries import _recall_limit
from contextlib import nullcontext
from .assembly import VERSION


# 与 ParentIndex.metadata 的有效性定义一致，包含缺页边界和接纳谱系。
# 该条件在召回和补读时分别执行，重排期间的来源修改不能绕过补读校验。
LIVE = '''FROM dd_agent.parent_index_builds b
 JOIN dd_agent.documents d ON d.id=b.document_id
 JOIN dd_agent.document_readings r ON r.document_id=d.id
 WHERE d.project_id=%s AND b.index_version=%s
 AND dd_agent.parent_build_origin_current(b.id)
 AND NOT EXISTS(SELECT 1 FROM dd_agent.parent_index_dependencies s
 LEFT JOIN dd_agent.effective_document_pages e
 ON e.document_id=s.document_id AND e.page_number=s.page_number
 WHERE s.build_id=b.id AND NOT coalesce(s.page_number<=r.page_count AND
 CASE WHEN s.readiness='missing' THEN e.page_number IS NULL
   AND s.source_revision='missing:'||r.pipeline_version
 ELSE e.source_revision=s.source_revision AND e.verification=s.quality AND
   (s.readiness='pending' OR s.readiness='ready' AND e.status='succeeded'
    OR s.readiness='failed' AND e.status='failed') END,false))'''


MANAGED = ''' AND EXISTS(SELECT 1 FROM dd_agent.assembly_handoff_parts h
    WHERE h.build_id=b.id AND h.document_id=b.document_id AND h.index_version=b.index_version
      AND h.assembly_policy=%s
      AND dd_agent.assembly_receipt_current(h.document_id,h.assembly_policy,h.receipt_generation))'''


class ParentQueries:
    def __init__(self,database):
        self.database=database

    def candidates(self,project_id,version,vector,*,documents=None,current_receipts_only=False,limit=12):
        """有界命中子块；向量空间由调用者提供的索引版本隔离。"""
        _recall_limit(limit)
        vector=valid_vector(vector)
        where=' AND b.document_id=ANY(%s::uuid[])' if documents is not None else ''
        args=(project_id,version)+((list(documents),) if documents is not None else ())
        if current_receipts_only:where+=MANAGED;args+=(VERSION,)
        with self.database.connect() as conn:
            return conn.execute('''WITH live AS (SELECT b.id,b.document_id '''+LIVE+where+''')
                SELECT c.build_id,c.id,c.parent_id,live.document_id,c.text,
                  c.start_offset,c.end_offset,c.source_spans,
                  1-(c.embedding <=> %s::public.vector) AS similarity
                FROM dd_agent.parent_index_children c JOIN live ON live.id=c.build_id
                ORDER BY c.embedding <=> %s::public.vector,c.build_id,c.id LIMIT %s''',
                args+(vector,vector,limit)).fetchall()

    def keyword_candidates(self,project_id,version,tsquery,needles,*,documents=None,current_receipts_only=False,limit=12):
        _recall_limit(limit)
        if not tsquery:
            return []
        needles=tuple(needles)
        if len(needles)>8:raise ValueError('关键词核对项过多。')
        where=' AND b.document_id=ANY(%s::uuid[])' if documents is not None else ''
        args=(tsquery,project_id,version)+((list(documents),) if documents is not None else ())
        if current_receipts_only:where+=MANAGED;args+=(VERSION,)
        exact=('('+' OR '.join(['strpos(lower(c.text),%s)>0']*len(needles))+') DESC,') if needles else ''
        args+=needles+(limit,)
        with self.database.connect() as conn:
            return conn.execute('''WITH q AS (SELECT %s::tsquery AS tsq),
                live AS (SELECT b.id,b.document_id '''+LIVE+where+''')
                SELECT c.build_id,c.id,c.parent_id,live.document_id,c.text,
                  c.start_offset,c.end_offset,c.source_spans,
                  ts_rank_cd(c.lexemes,q.tsq) AS keyword_score
                FROM q JOIN dd_agent.parent_index_children c ON c.lexemes @@ q.tsq
                JOIN live ON live.id=c.build_id
                ORDER BY '''+exact+''' ts_rank_cd(c.lexemes,q.tsq) DESC,c.build_id,c.id LIMIT %s''',args).fetchall()

    def finalize(self,project_id,version,candidates,*,conn=None,current_receipts_only=False):
        """重排后在单一只读快照核对最多 12 个精确 child 身份及其父块。

        不持事务等待模型。父块最多 4000 字符，内部最多 12 份；响应层另限
        5 个父块。子块和上下文在同一快照读取，避免逐父块查询混合来源版本。
        """
        if len(candidates)>12:raise ValueError('Candidate budget exceeded')
        if not candidates:return []
        builds=[r['build_id'] for r in candidates]
        identities=[r['id'] for r in candidates]
        owned=conn is None
        where=MANAGED if current_receipts_only else ''
        args=(project_id,version)+((VERSION,) if current_receipts_only else ())+(builds,identities)
        with (self.database.connect() if owned else nullcontext(conn)) as conn:
            if owned:conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            return conn.execute('''WITH live AS (SELECT b.id,b.document_id '''+LIVE+where+'''),
                wanted AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS w(build_id,id))
                SELECT c.build_id,c.id,c.parent_id,live.document_id,c.text,c.start_offset,c.end_offset,
                  c.source_spans,p.text AS parent_text,p.source_spans AS parent_spans,
                  p.separators,p.boundary,p.continuation_from
                FROM wanted w JOIN dd_agent.parent_index_children c
                  ON c.build_id=w.build_id AND c.id=w.id
                JOIN live ON live.id=c.build_id
                JOIN dd_agent.parent_index_parents p ON p.build_id=c.build_id AND p.id=c.parent_id
                LIMIT 12''',args).fetchall()

    def context(self,project_id,document_id,version,build_id,parent_id,*,offset=0,limit=4000,conn=None):
        """按不可变 build/parent 续读；过期、越项目和不存在统一返回 None。

        source_spans 定位完整父块，derived 偏移以完整父块为基准；返回 offset
        明确本次文字的位置，不把裁剪后的文字配上错误的页内坐标。
        """
        if type(offset) is not int or not 0<=offset<=4000 or type(limit) is not int or not 1<=limit<=4000:
            raise ValueError('父块读取偏移／字符预算无效。')
        if conn is None:
            with self.database.connect() as connection:
                return self.context(project_id,document_id,version,build_id,parent_id,
                    offset=offset,limit=limit,conn=connection)
        row=conn.execute('''WITH live AS (SELECT b.id,b.document_id,b.index_version '''+LIVE+'''
                  AND d.id=%s AND b.id=%s)
                SELECT p.build_id,p.id AS parent_id,live.document_id,live.index_version,
                  substring(p.text FROM %s FOR %s) AS text,length(p.text) AS total_chars,
                  p.source_spans,p.separators,p.boundary,p.continuation_from
                FROM dd_agent.parent_index_parents p JOIN live ON live.id=p.build_id
                WHERE p.id=%s''',
                (project_id,version,document_id,build_id,offset+1,limit,parent_id)).fetchone()
        if row is None:return None
        if offset>row['total_chars']:raise ValueError('父块读取偏移超出正文。')
        end=offset+len(row['text'])
        return dict(row,offset=offset,next_offset=end if end<row['total_chars'] else None)
