"""查询切换前的单材料覆盖资格。只返回计数，不读取整卷正文或启动模型。"""
import re
from contextlib import nullcontext
from uuid import UUID
from .assembly import VERSION,digest
from .parent_queries import LIVE


class ParentCoverage:
    def __init__(self,database):self.database=database

    def discover(self,project_id):
        """一次有界元数据查询，同时判断项目存在性；候选仍须 inspect 完整复验。

        不按页读取、不加载正文或模型。拿 21 个身份用于检测 20 份上限，
        不把粗略的 closed 标记等同于可检索资格。
        """
        with self.database.connect() as conn:
            row=conn.execute('''SELECT ARRAY(SELECT d.id FROM dd_agent.documents d
                JOIN dd_agent.assembly_checkpoints c ON c.document_id=d.id AND c.policy_version=%s
                WHERE d.project_id=p.id AND d.status='registered' AND c.state->'closed'='true'::jsonb
                ORDER BY d.id LIMIT 21) AS documents
                FROM dd_agent.projects p WHERE p.id=%s''',(VERSION,UUID(str(project_id)))).fetchone()
        if row is None:return None
        return dict(documents=[str(v) for v in row['documents'][:20]],limited=len(row['documents'])>20)

    def inspect(self,project_id,document_id,index_version,*,conn=None):
        project_id,document_id=UUID(str(project_id)),UUID(str(document_id))
        if not isinstance(index_version,str) or re.fullmatch('[0-9a-f]{64}',index_version) is None:
            raise ValueError('索引版本格式无效。')
        owned=conn is None
        with (self.database.connect() if owned else nullcontext(conn)) as conn:
            # 检查点、当前谱系、来源和发布结果必须来自同一快照。
            if owned:conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            doc=conn.execute('''SELECT r.page_count,c.state FROM dd_agent.documents d
                LEFT JOIN dd_agent.document_readings r ON r.document_id=d.id
                LEFT JOIN dd_agent.assembly_checkpoints c ON c.document_id=d.id AND c.policy_version=%s
                WHERE d.project_id=%s AND d.id=%s AND d.status='registered' ''',
                (VERSION,project_id,document_id)).fetchone()
            if doc is None:return None
            result=dict(document_id=str(document_id),index_version=index_version,
                total_pages=doc['page_count'],covered_pages=0,expected_parents=0,
                unpublished_parents=0,eligible=False,reason='not_closed')
            state=doc['state'];total=doc['page_count']
            if not isinstance(state,dict) or not total:return result
            valid=(state.get('version')==VERSION and state.get('project_id')==str(project_id)
                and state.get('document_id')==str(document_id)
                and state.get('checksum')==digest({k:v for k,v in state.items() if k!='checksum'}))
            if not valid:return dict(result,reason='invalid_checkpoint')
            if state.get('closed') is not True or state.get('next_page')!=total+1 or state.get('tail') is not None:
                return result
            row=conn.execute('''WITH valid_builds AS (SELECT b.id '''+LIVE+'''
                  AND d.id=%s), batches AS (
                SELECT o.receipt_generation,b.result FROM dd_agent.assembly_live_receipts o
                JOIN dd_agent.assembly_batches b ON b.document_id=o.document_id
                  AND b.policy_version=o.assembly_policy AND b.generation=o.receipt_generation
                WHERE o.document_id=%s AND o.assembly_policy=%s AND NOT o.is_reset
            ), deps AS (
                SELECT value AS dep FROM batches CROSS JOIN LATERAL jsonb_array_elements(
                  CASE WHEN jsonb_typeof(result->'input_dependencies')='array'
                    THEN result->'input_dependencies' ELSE '[]'::jsonb END)
            ), checked AS (
                SELECT (dep->>'page_number')::int AS number,
                  coalesce(dep->>'project_id'=%s AND dep->>'document_id'=%s
                    AND dep->>'readiness'='ready' AND e.status='succeeded'
                    AND e.source_revision=dep->>'source_revision'
                    AND e.verification=dep->>'quality'
                    AND (dep->>'page_number')::int BETWEEN 1 AND %s,false) AS valid
                FROM deps LEFT JOIN dd_agent.effective_document_pages e
                  ON e.document_id=%s AND e.page_number=(dep->>'page_number')::int
            ), expected AS (
                SELECT receipt_generation,value->>'id' AS id FROM batches
                CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(result->'parents')='array'
                  THEN result->'parents' ELSE '[]'::jsonb END)
            ) SELECT
                (SELECT count(*) FROM batches) AS receipts,
                (SELECT count(*) FROM batches WHERE NOT coalesce(
                  jsonb_typeof(result->'input_dependencies')='array' AND jsonb_typeof(result->'parents')='array',false)) AS legacy_missing,
                (SELECT count(DISTINCT number) FROM checked WHERE valid) AS covered_pages,
                (SELECT count(*) FROM checked WHERE NOT valid) AS invalid_pages,
                (SELECT count(*) FROM expected) AS expected_parents,
                (SELECT count(*) FROM expected x WHERE NOT EXISTS (
                  SELECT 1 FROM dd_agent.assembly_handoff_parts h
                  JOIN valid_builds v ON v.id=h.build_id
                  JOIN dd_agent.parent_index_parents p ON p.build_id=v.id AND p.id=x.id
                  WHERE h.document_id=%s AND h.assembly_policy=%s
                    AND h.receipt_generation=x.receipt_generation AND h.index_version=%s
                )) AS unpublished_parents''',
                (project_id,index_version,document_id,document_id,VERSION,str(project_id),str(document_id),
                 total,document_id,document_id,VERSION,index_version)).fetchone()
        result.update(covered_pages=row['covered_pages'],expected_parents=row['expected_parents'],
                      unpublished_parents=row['unpublished_parents'])
        if not row['receipts'] or row['legacy_missing']:return dict(result,reason='unverified_receipts')
        if row['invalid_pages'] or row['covered_pages']!=total:return dict(result,reason='source_coverage_gap')
        if row['unpublished_parents']:return dict(result,reason='unpublished_parents')
        return dict(result,eligible=True,reason='complete')
