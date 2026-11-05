"""精确召回与有界最终复验。只返回候选，覆盖统计在 SQL 内聚合。

保持页级当前来源契约；未来父子块接入时在此替换依赖查询，不能跳过复验。
"""
from contextlib import nullcontext

def _recall_limit(limit):
    if type(limit) is not int or not 1<=limit<=50:
        raise ValueError('召回条数无效。')


CURRENT = '''FROM dd_agent.indexed_pages i
 JOIN dd_agent.documents d ON d.id=i.document_id
 JOIN dd_agent.effective_document_pages p ON p.document_id=i.document_id AND p.page_number=i.page_number
   AND p.pipeline_version=i.pipeline_version AND p.updated_at=i.source_updated_at
   AND p.source_revision=i.source_revision AND p.status='succeeded'
 WHERE d.project_id=%s AND i.index_version=%s'''


class SearchQueries:
    def __init__(self,database):self.database=database

    def names(self,project_id,documents,*,conn):
        """仅最终候选材料的来源文件名，不读取原件路径或整项目材料清单。"""
        documents=list(set(documents))
        if len(documents)>12:raise ValueError('Document metadata budget exceeded')
        if not documents:return {}
        rows=conn.execute('''WITH names AS (
            SELECT s.document_id,s.filename,
              row_number() OVER(PARTITION BY s.document_id ORDER BY s.first_seen_at,s.filename) AS ordinal
            FROM dd_agent.document_sources s JOIN dd_agent.documents d ON d.id=s.document_id
            WHERE d.project_id=%s AND d.id=ANY(%s::uuid[]))
            SELECT document_id,array_agg(filename ORDER BY ordinal) FILTER(WHERE ordinal<=50) AS filenames,
              count(*)>50 AS sources_truncated FROM names GROUP BY document_id''',(project_id,documents)).fetchall()
        return {r['document_id']:r for r in rows}

    def coverage(self,project_id,version,conn=None,*,excluded_documents=()):
        if conn is None:
            with self.database.connect() as connection:return self.coverage(project_id,version,conn=connection,excluded_documents=excluded_documents)
        exclude=' AND NOT i.document_id=ANY(%s::uuid[])' if excluded_documents else ''
        args=(project_id,version)+((list(excluded_documents),) if excluded_documents else ())+(project_id,)
        row=conn.execute('''SELECT totals.*, (SELECT count(*) '''+CURRENT+exclude+''')::integer AS indexed_pages
            FROM (SELECT coalesce(sum(r.page_count),0)::integer AS total_pages,
              count(*) FILTER (WHERE r.page_count IS NULL)::integer AS unread_documents
              FROM dd_agent.documents d LEFT JOIN dd_agent.document_readings r ON r.document_id=d.id
              WHERE d.project_id=%s AND d.status='registered') totals''',args).fetchone()
        return dict(row,complete=bool(row['indexed_pages']) and row['indexed_pages']==row['total_pages'] and not row['unread_documents'])

    def candidates(self,project_id,version,vector,*,excluded_documents=(),limit=12):
        _recall_limit(limit)
        exclude=' AND NOT i.document_id=ANY(%s::uuid[])' if excluded_documents else ''
        args=(vector,project_id,version)+((list(excluded_documents),) if excluded_documents else ())+(vector,limit)
        with self.database.connect() as conn:
            return conn.execute('''SELECT c.id,c.document_id,c.page_number,c.text,c.start_offset,c.end_offset,
                live.method,live.pipeline_version,live.verification,1-(c.embedding <=> %s::public.vector) AS similarity
                FROM dd_agent.evidence_chunks c
                JOIN (SELECT i.document_id,i.page_number,i.index_version,p.method,p.pipeline_version,p.verification '''+CURRENT+exclude+''') live
                ON c.document_id=live.document_id AND c.page_number=live.page_number AND c.index_version=live.index_version
                ORDER BY c.embedding <=> %s::public.vector,c.id LIMIT %s''',args).fetchall()

    def keyword_candidates(self,project_id,version,tsquery,needles,*,excluded_documents=(),limit=12):
        """全文索引命中。字面包含优先，其次是词项紧密度；条数有上界。"""
        _recall_limit(limit)
        if not tsquery:
            return []
        needles=tuple(needles)
        if len(needles)>8:raise ValueError('关键词核对项过多。')
        exclude=' AND NOT i.document_id=ANY(%s::uuid[])' if excluded_documents else ''
        exact=('('+' OR '.join(['strpos(lower(c.text),%s)>0']*len(needles))+') DESC,') if needles else ''
        args=(tsquery,project_id,version)+((list(excluded_documents),) if excluded_documents else ())+needles+(limit,)
        with self.database.connect() as conn:
            return conn.execute('''WITH q AS (SELECT %s::tsquery AS tsq), live AS (
                SELECT i.document_id,i.page_number,i.index_version,p.method,p.pipeline_version,p.verification '''+CURRENT+exclude+''')
                SELECT c.id,c.document_id,c.page_number,c.text,c.start_offset,c.end_offset,
                  live.method,live.pipeline_version,live.verification,ts_rank_cd(c.lexemes,q.tsq) AS keyword_score
                FROM q JOIN dd_agent.evidence_chunks c ON c.lexemes @@ q.tsq
                JOIN live ON c.document_id=live.document_id AND c.page_number=live.page_number AND c.index_version=live.index_version
                ORDER BY '''+exact+''' ts_rank_cd(c.lexemes,q.tsq) DESC,c.id LIMIT %s''',args).fetchall()

    def finalize(self,project_id,version,ids,*,conn=None,excluded_documents=()):
        if len(ids)>12:raise ValueError('Candidate budget exceeded')
        owned=conn is None
        with (self.database.connect() if owned else nullcontext(conn)) as conn:
            # One short snapshot for final coverage and candidate metadata;
            # no model calls or file reads occur while this transaction is open.
            if owned:conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            coverage=self.coverage(project_id,version,conn=conn,excluded_documents=excluded_documents)
            rows=conn.execute('''WITH live AS (
                SELECT c.id,c.document_id,p.method,p.pipeline_version,p.verification
                FROM dd_agent.evidence_chunks c
                JOIN (SELECT i.document_id,i.page_number,i.index_version,p.method,p.pipeline_version,p.verification '''+CURRENT+''') p
                ON c.document_id=p.document_id AND c.page_number=p.page_number AND c.index_version=p.index_version
                WHERE c.id=ANY(%s) LIMIT 12
              ), names AS (
                SELECT s.document_id,s.filename,
                  row_number() OVER (PARTITION BY s.document_id ORDER BY s.first_seen_at,s.filename) AS ordinal
                FROM dd_agent.document_sources s JOIN (SELECT DISTINCT document_id FROM live) docs USING(document_id)
              ), sources AS (
                SELECT document_id,array_agg(filename ORDER BY ordinal) FILTER (WHERE ordinal<=50) AS filenames,
                  count(*)>50 AS sources_truncated FROM names GROUP BY document_id
              ) SELECT live.*,coalesce(sources.filenames,ARRAY[]::text[]) AS filenames,
                coalesce(sources.sources_truncated,false) AS sources_truncated
              FROM live LEFT JOIN sources USING(document_id)''',(project_id,version,ids)).fetchall() if ids else []
        return coverage,{row['id']:row for row in rows}
