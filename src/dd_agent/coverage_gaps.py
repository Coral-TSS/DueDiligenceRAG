"""当前项目的覆盖缺口。只读 SQL，不启动嵌入、重排或 OCR，也不读取任务进度。"""
import hashlib
from uuid import UUID

from .page_queries import bounded


KINDS = ('unprocessed', 'failed', 'unknown', 'empty_text', 'pending_rebuild')
DEFINITION = (
    '缺口按各自口径列出：未处理、失败、未知、空文本、待重建。'
    '同页可以同时计入多项，这些数字不能相加为总页数。'
    '已知总页数、页数未知的材料、基础成功页、视觉增强、尚未视觉复核的页和当前有效索引页分开计数。'
    '空项目、页数未知和空文本都不算全文可检索。'
    '不把任务进度里的已处理页数当成有效索引，也不为了统计启动推理模型。'
    '索引版本只读取已经缓存的模型指纹；没有指纹时，有正文的页标为待重建，而不是当成当前可检索。'
)

_GAPS = '''
WITH versions AS (
    SELECT %s::text AS page_version, %s::text AS office_version
),
docs AS (
    SELECT d.id AS document_id, r.page_count,
           EXISTS (SELECT 1 FROM dd_agent.source_units u WHERE u.document_id = d.id) AS has_units
    FROM dd_agent.documents d
    LEFT JOIN dd_agent.document_readings r ON r.document_id = d.id
    WHERE d.project_id = %s AND d.status = 'registered'
),
pdf_pages AS (
    SELECT d.document_id, gs.page_number
    FROM docs d
    JOIN LATERAL generate_series(1, d.page_count) AS gs(page_number) ON d.page_count IS NOT NULL
),
pdf_joined AS (
    SELECT e.document_id, e.page_number, p.status, p.method,
           coalesce(p.error_code, '') AS error_code, p.verification, coalesce(p.text, '') AS text,
           p.document_id IS NOT NULL AS has_page,
           (
             (SELECT page_version FROM versions) IS NOT NULL
             AND p.document_id IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM dd_agent.indexed_pages i
               WHERE i.document_id = e.document_id AND i.page_number = e.page_number
                 AND i.index_version = (SELECT page_version FROM versions)
                 AND i.pipeline_version = p.pipeline_version
                 AND i.source_updated_at = p.updated_at
                 AND i.source_revision = p.source_revision
             )
           ) AS index_current
    FROM pdf_pages e
    LEFT JOIN dd_agent.effective_document_pages p
      ON p.document_id = e.document_id AND p.page_number = e.page_number
),
pdf_gaps AS (
    SELECT j.document_id, j.page_number, NULL::uuid AS unit_id, NULL::jsonb AS locator,
           NULL::text AS formula_state, j.status, j.method, j.error_code, j.verification,
           ARRAY_REMOVE(ARRAY[
             CASE WHEN NOT j.has_page THEN 'unprocessed' END,
             CASE WHEN j.status = 'failed' THEN 'failed' END,
             CASE WHEN j.status = 'succeeded' AND btrim(j.text, E' \t\n\r\f\v　') = '' THEN 'empty_text' END,
             CASE WHEN j.verification = 'machine_uncertain' THEN 'unknown' END,
             CASE WHEN j.status = 'succeeded' AND NOT j.index_current THEN 'pending_rebuild' END
           ]::text[], NULL) AS kinds,
           ARRAY_REMOVE(ARRAY[
             CASE WHEN NOT j.has_page THEN 'not_read' END,
             CASE WHEN j.status = 'failed' THEN 'read_failed' END,
             CASE WHEN j.status = 'succeeded' AND btrim(j.text, E' \t\n\r\f\v　') = '' THEN 'blank_text' END,
             CASE WHEN j.verification = 'machine_uncertain' THEN 'machine_uncertain' END,
             CASE WHEN j.status = 'succeeded' AND NOT j.index_current
                  AND (SELECT page_version FROM versions) IS NULL THEN 'index_version_unknown' END,
             CASE WHEN j.status = 'succeeded' AND NOT j.index_current
                  AND (SELECT page_version FROM versions) IS NOT NULL THEN 'index_not_current' END
           ]::text[], NULL) AS reasons
    FROM pdf_joined j
),
material_gaps AS (
    SELECT d.document_id, NULL::integer AS page_number, NULL::uuid AS unit_id, NULL::jsonb AS locator,
           NULL::text AS formula_state, NULL::text AS status, NULL::text AS method, ''::text AS error_code,
           NULL::text AS verification,
           ARRAY['unprocessed']::text[] AS kinds,
           ARRAY['not_prechecked']::text[] AS reasons
    FROM docs d
    WHERE d.page_count IS NULL AND NOT d.has_units
),
unit_indexed AS (
    SELECT u.id, u.document_id, u.locator, u.formula_state, u.status, u.error_code, u.text,
           (
             (SELECT office_version FROM versions) IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM dd_agent.source_unit_chunks c
               WHERE c.unit_id = u.id AND c.index_version = (SELECT office_version FROM versions)
                 AND c.text = u.text
             )
           ) AS index_current
    FROM dd_agent.source_units u
    JOIN dd_agent.documents d ON d.id = u.document_id
    WHERE d.project_id = %s AND d.status = 'registered'
),
unit_gaps AS (
    SELECT u.document_id, NULL::integer AS page_number, u.id AS unit_id, u.locator,
           u.formula_state, u.status, NULL::text AS method, u.error_code, NULL::text AS verification,
           ARRAY_REMOVE(ARRAY[
             CASE WHEN u.status = 'failed' THEN 'failed' END,
             CASE WHEN u.formula_state = 'unknown' THEN 'unknown' END,
             CASE WHEN u.status = 'succeeded' AND u.formula_state <> 'unknown' AND btrim(u.text, E' \t\n\r\f\v　') = '' THEN 'empty_text' END,
             CASE WHEN u.status = 'succeeded' AND u.formula_state <> 'unknown' AND NOT u.index_current THEN 'pending_rebuild' END
           ]::text[], NULL) AS kinds,
           ARRAY_REMOVE(ARRAY[
             CASE WHEN u.status = 'failed' THEN 'read_failed' END,
             CASE WHEN u.formula_state = 'unknown' THEN 'formula_unknown' END,
             CASE WHEN u.status = 'succeeded' AND u.formula_state <> 'unknown' AND btrim(u.text, E' \t\n\r\f\v　') = '' THEN 'blank_text' END,
             CASE WHEN u.status = 'succeeded' AND u.formula_state <> 'unknown' AND NOT u.index_current
                  AND (SELECT office_version FROM versions) IS NULL THEN 'index_version_unknown' END,
             CASE WHEN u.status = 'succeeded' AND u.formula_state <> 'unknown' AND NOT u.index_current
                  AND (SELECT office_version FROM versions) IS NOT NULL THEN 'index_not_current' END
           ]::text[], NULL) AS reasons
    FROM unit_indexed u
),
gaps AS (
    SELECT * FROM pdf_gaps WHERE coalesce(cardinality(kinds), 0) > 0
    UNION ALL
    SELECT * FROM material_gaps
    UNION ALL
    SELECT * FROM unit_gaps WHERE coalesce(cardinality(kinds), 0) > 0
)
'''

_COUNTS = _GAPS + '''
SELECT
  (SELECT count(*) FROM docs)::int AS documents,
  (SELECT coalesce(sum(page_count), 0) FROM docs)::int AS known_pages,
  (SELECT count(*) FROM docs WHERE page_count IS NULL AND NOT has_units)::int AS unknown_page_documents,
  (SELECT count(*) FROM pdf_joined WHERE status = 'succeeded')::int AS base_succeeded,
  (SELECT count(*) FROM pdf_joined WHERE index_current)::int AS indexed_pages,
  (SELECT count(*) FROM pdf_joined WHERE status = 'succeeded' AND verification IN ('machine_enhanced', 'machine_checked'))::int AS vision_enhanced,
  (SELECT count(*) FROM pdf_joined WHERE status = 'succeeded' AND verification = 'unverified')::int AS vision_unverified,
  (SELECT count(*) FROM gaps WHERE 'failed' = ANY(kinds))::int AS failed,
  (SELECT count(*) FROM gaps WHERE 'unprocessed' = ANY(kinds))::int AS unprocessed,
  (SELECT count(*) FROM gaps WHERE 'unknown' = ANY(kinds))::int AS unknown,
  (SELECT count(*) FROM gaps WHERE 'empty_text' = ANY(kinds))::int AS empty_text,
  (SELECT count(*) FROM gaps WHERE 'pending_rebuild' = ANY(kinds))::int AS pending_rebuild,
  (SELECT count(*) FROM dd_agent.source_units u
     JOIN dd_agent.documents d ON d.id = u.document_id
     WHERE d.project_id = %s)::int AS office_units,
  (SELECT count(*) FROM unit_indexed WHERE index_current)::int AS indexed_office_units,
  (SELECT count(*) FROM dd_agent.documents WHERE project_id = %s AND status <> 'registered')::int AS not_registered_documents
'''

_PAGE = _GAPS + '''
SELECT document_id, page_number, unit_id, locator, formula_state, status, method, error_code,
       verification, kinds, reasons
FROM gaps
WHERE (%s::text IS NULL OR %s = ANY(kinds))
  AND (%s::bool OR (document_id, coalesce(page_number, 0), coalesce(unit_id::text, ''))
       > (%s::uuid, %s::int, %s::text))
ORDER BY document_id, coalesce(page_number, 0), coalesce(unit_id::text, '')
LIMIT %s
'''


def index_versions(evidence):
    """页索引和 Office 索引共用同一份模型指纹。没有缓存时两个版本都是 None。"""
    from .retrieval import CHUNK_VERSION
    models = evidence.models
    cached = getattr(models, 'cached_profile', None)
    profile = cached() if cached is not None else models.profile()
    if not profile:
        return None, None
    page = hashlib.sha256((CHUNK_VERSION + profile).encode()).hexdigest()
    office = hashlib.sha256(('office-unit-v1' + profile).encode()).hexdigest()
    return page, office


def _cursor(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        raise ValueError('缺口游标无效。')
    parts = value.split('|')
    if len(parts) != 3:
        raise ValueError('缺口游标无效。')
    try:
        document_id = UUID(parts[0])
        page = int(parts[1])
    except (ValueError, TypeError):
        raise ValueError('缺口游标无效。') from None
    if page < 0:
        raise ValueError('缺口游标无效。')
    if parts[2]:
        try:
            UUID(parts[2])
        except ValueError:
            raise ValueError('缺口游标无效。') from None
    return document_id, page, parts[2]


class CoverageGaps:
    def __init__(self, database, evidence):
        self.database = database
        self.evidence = evidence

    def page(self, project_id, *, kind=None, limit=None, cursor=None):
        project_id = UUID(str(project_id))
        if kind is not None and kind not in KINDS:
            raise ValueError('缺口种类无效。')
        limit = bounded(limit, 50, 100, '每批缺口')
        position = _cursor(cursor)
        page_version, office_version = index_versions(self.evidence)
        base = (page_version, office_version, project_id, project_id)
        with self.database.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            as_of = conn.execute('SELECT clock_timestamp() AS as_of').fetchone()['as_of']
            counts = conn.execute(_COUNTS, base + (project_id, project_id)).fetchone()
            if position is None:
                keyset = (True, None, 0, '')
            else:
                keyset = (False, position[0], position[1], position[2])
            rows = conn.execute(
                _PAGE, base + (kind, kind, *keyset, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            names = self._names(conn, project_id, [row['document_id'] for row in rows])
        items = [_item(row, names) for row in rows]
        known = page_version is not None
        complete = bool(
            counts['documents'] and known and not any(counts[name] for name in KINDS))
        return {
            'project_id': str(project_id),
            'complete': complete,
            'as_of': as_of.isoformat(),
            'index_version': page_version,
            'office_index_version': office_version,
            'index_version_known': known,
            'definition': DEFINITION,
            'counts': {key: counts[key] for key in (
                'documents', 'known_pages', 'unknown_page_documents', 'base_succeeded',
                'indexed_pages', 'vision_enhanced', 'vision_unverified',
                'failed', 'unprocessed', 'unknown', 'empty_text', 'pending_rebuild',
                'office_units', 'indexed_office_units', 'not_registered_documents')},
            'items': items,
            'next_cursor': _encode(rows[-1]) if more and rows else None,
            'limit': limit,
        }

    @staticmethod
    def _names(conn, project_id, document_ids):
        unique = list(dict.fromkeys(document_ids))
        if not unique:
            return {}
        rows = conn.execute('''WITH names AS (
            SELECT s.document_id, s.filename,
              row_number() OVER (PARTITION BY s.document_id ORDER BY s.first_seen_at, s.filename) AS ordinal
            FROM dd_agent.document_sources s
            JOIN dd_agent.documents d ON d.id = s.document_id
            WHERE d.project_id = %s AND d.id = ANY(%s::uuid[]))
            SELECT document_id, array_agg(filename ORDER BY ordinal) FILTER (WHERE ordinal <= 50) AS filenames
            FROM names GROUP BY document_id''', (project_id, unique)).fetchall()
        return {row['document_id']: list(row['filenames'] or []) for row in rows}


def _encode(row):
    unit = row['unit_id'] or ''
    page = row['page_number'] or 0
    return f"{row['document_id']}|{page}|{unit}"


def _item(row, names):
    return {
        'document_id': str(row['document_id']),
        'filenames': names.get(row['document_id'], []),
        'page_number': row['page_number'],
        'unit_id': str(row['unit_id']) if row['unit_id'] else None,
        'locator': row['locator'],
        'formula_state': row['formula_state'] or '',
        'status': row['status'],
        'method': row['method'],
        'error_code': row['error_code'] or '',
        'verification': row['verification'],
        'kinds': list(row['kinds'] or []),
        'reasons': list(row['reasons'] or []),
    }
