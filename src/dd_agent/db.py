# 持久化访问层：当前使用 psycopg 直接执行 SQL，没有 ORM，也没有“完整事实库”。
# 数据分层：projects/documents/document_sources（登记）
#         -> document_readings/document_pages（基础阅读）
#         -> machine_pages/effective_document_pages（增强历史／有效文字视图）
#         -> indexed_pages/evidence_chunks（页级索引／片段向量）。
# processing_jobs 保存调度状态；vision_candidates 保存视觉候选，历史人工记录仅供回看。
# 本文件部分内部方法只收 document_id，调用它们之前须由服务层验证项目归属。
from pathlib import Path
from contextlib import contextmanager, nullcontext
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from .db_connections import MeasuredCursor, open_connection


class Database:
    def __init__(self, url, provider=None):
        self._url = url
        self.provider = provider

    @contextmanager
    def connect(self):
        # 调用方的 with connect() 管理事务：正常提交，异常回滚；随后归还池或关闭短连接。
        # dict_row 使 SQL 结果可按字段名读取，不在调用处依赖列顺序。
        if not self._url:
            raise psycopg.OperationalError("Database not configured")
        if self.provider is not None and self.provider.active:
            with self.provider.connection() as conn: yield conn
        else:
            # 独立服务、作用域外兼容用法与停机中的安全保存使用短连接，不隐式重开池。
            with open_connection(self._url) as conn: yield conn

    @contextmanager
    def publication(self, stage, number, *, document_id=None, final=True, success=True):
        from .stage_publication import binding, StageSuperseded
        context = binding.get()
        if context is None:
            with self.connect() as conn: yield conn
            return
        store, expected, tasks = context
        task = tasks.get(number)
        if expected != stage or task is None or (document_id is not None and str(document_id)!=str(task['document_id'])):
            raise StageSuperseded('阶段发布范围已失效。')
        with store.publish_scope(task['id'],task['owner_token'],final=final,success=success) as conn:
            yield conn

    def lease(self):
        # 队列 session advisory lock 必须随专用连接生命周期，不经普通池重置。
        return open_connection(self._url, lease=True)

    def health(self):
        # 健康检查只核对连接和 schema 版本，不读取客户正文，也不启动推理。
        if not self._url:
            return {"status": "needs_configuration", "message": "请在工程 .env 中设置 DDA_DATABASE_URL，然后重启服务。"}
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT max(version) AS version FROM dd_agent.schema_version").fetchone()
                if row["version"] != 15:
                    return {"status": "needs_migration", "message": "数据库版本与应用不符，请检查迁移版本。"}
        except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
            return {"status": "needs_migration", "message": "数据库已连接，请先运行 python -m dd_agent init-db 初始化表。"}
        except (psycopg.Error, ValueError):
            return {"status": "database_unavailable", "message": "数据库无法连接或权限不足，请检查服务、账号及 .env 配置后重试。"}
        return {"status": "ready", "message": "材料工作区已就绪。文件和 OCR 处理保存在本机，不调用云端模型。"}

    def migrate(self):
        # 迁移文件按名称执行；数据库锁避免两个应用同时初始化。
        # 当前迁移采用可重复执行的 SQL，而非仅看“文件执行过”就跳过。
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(74332101)")
            for source in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
                conn.execute(source.read_text(encoding='utf-8'))

    def projects(self):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM dd_agent.projects ORDER BY created_at DESC, id").fetchall()

    def create_project(self, name):
        with self.connect() as conn:
            return conn.execute("INSERT INTO dd_agent.projects (id, name) VALUES (%s, %s) RETURNING *", (uuid4(), name)).fetchone()

    def project_exists(self, project_id):
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM dd_agent.projects WHERE id = %s", (project_id,)).fetchone() is not None

    @staticmethod
    def _with_sources(conn, document):
        # 一份相同内容的文件可能有多个原文件名，它们不是多份独立材料。
        if document:
            names = conn.execute("SELECT filename FROM dd_agent.document_sources WHERE document_id = %s ORDER BY first_seen_at, filename", (document["id"],)).fetchall()
            document["filenames"] = [n["filename"] for n in names]
        return document

    def documents(self, project_id):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM dd_agent.documents WHERE project_id = %s ORDER BY created_at DESC, id", (project_id,)).fetchall()
            return [self._with_sources(conn, row) for row in rows]

    def document(self, project_id, document_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM dd_agent.documents WHERE project_id = %s AND id = %s", (project_id, document_id)).fetchone()
            return self._with_sources(conn, row)

    def material_catalog(self, project_id, limit, after=None):
        """Metadata only, bounded at SQL level; UUID ordering supports keyset paging."""
        # UUID 升序续读，不把 after 当偏移量；游标本身也必须属于当前项目。
        # 多取一条判断是否还有下一批，实际只返回 limit 条，不加载页面正文。
        with self.connect() as conn:
            if not conn.execute('SELECT 1 FROM dd_agent.projects WHERE id=%s',(project_id,)).fetchone():
                raise LookupError('项目不存在。')
            if after is not None and not conn.execute(
                'SELECT 1 FROM dd_agent.documents WHERE project_id=%s AND id=%s',(project_id,after)).fetchone():
                raise LookupError('材料游标不存在或不属于当前项目。')
            total=conn.execute('SELECT count(*) AS n FROM dd_agent.documents WHERE project_id=%s',
                               (project_id,)).fetchone()['n']
            rows=conn.execute('''SELECT d.id AS document_id,d.status,d.reason,d.byte_size,
                r.page_count,r.pipeline_version AS reading_version,
                ARRAY(SELECT s.filename FROM dd_agent.document_sources s
                      WHERE s.document_id=d.id ORDER BY s.first_seen_at,s.filename) AS filenames
                FROM dd_agent.documents d LEFT JOIN dd_agent.document_readings r ON r.document_id=d.id
                WHERE d.project_id=%s AND (%s::uuid IS NULL OR d.id>%s::uuid)
                ORDER BY d.id LIMIT %s''',(project_id,after,after,limit+1)).fetchall()
            items=rows[:limit]
            return dict(items=items,total_documents=total,
                        next_cursor=str(items[-1]['document_id']) if len(rows)>limit else None)

    def register(self, project_id, stored):
        # 项目＋内容摘要的唯一约束解决并发去重；不要先查“没有”再无条件插入。
        with self.connect() as conn:
            row = conn.execute(
                """INSERT INTO dd_agent.documents (id, project_id, sha256, byte_size, storage_key, status, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, sha256) DO NOTHING RETURNING *""",
                (uuid4(), project_id, stored["sha256"], stored["byte_size"], stored["storage_key"], stored["status"], stored["reason"]),
            ).fetchone()
            duplicate = row is None
            if duplicate:
                row = conn.execute("SELECT * FROM dd_agent.documents WHERE project_id = %s AND sha256 = %s", (project_id, stored["sha256"])).fetchone()
            conn.execute("INSERT INTO dd_agent.document_sources (document_id, filename) VALUES (%s, %s) ON CONFLICT DO NOTHING", (row["id"], stored["filename"]))
            return self._with_sources(conn, row), duplicate

    def set_reading(self, document_id, page_count, version, conn=None):
        # 这里切换材料的当前阅读版本；历史 document_pages 仍保留，不是删除后重建。
        if conn is None:
            with self.connect() as transaction:return self.set_reading(document_id,page_count,version,conn=transaction)
        with nullcontext(conn) as conn:
            conn.execute('''INSERT INTO dd_agent.document_readings (document_id,page_count,pipeline_version)
                VALUES (%s,%s,%s) ON CONFLICT (document_id) DO UPDATE SET
                page_count=EXCLUDED.page_count,pipeline_version=EXCLUDED.pipeline_version,updated_at=now()''', (document_id,page_count,version))

    def save_page(self, document_id, version, page, conn=None):
        # 保存基础页，并更新来源时间；索引会用时间和修订摘要检查是否仍对应此页。
        if conn is None:
            with self.publication('reading',page['page_number'],document_id=document_id,success=page['status']=='succeeded') as transaction:
                return self.save_page(document_id, version, page, conn=transaction)
        with nullcontext(conn) as conn:
            # 在插入之前取得阅读记录的共享锁：组装保存用排他锁保护缺页，
            # 不存在的 document_pages 行无法单独加行锁。始终先阅读、后页。
            conn.execute('SELECT document_id FROM dd_agent.document_readings WHERE document_id=%s FOR SHARE',
                         (document_id,)).fetchone()
            conn.execute('''INSERT INTO dd_agent.document_pages
                (document_id,page_number,pipeline_version,status,method,text,lines,image_key,error_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (document_id,page_number,pipeline_version) DO UPDATE SET
                status=EXCLUDED.status,method=EXCLUDED.method,text=EXCLUDED.text,lines=EXCLUDED.lines,
                image_key=EXCLUDED.image_key,error_code=EXCLUDED.error_code,
                updated_at=greatest(clock_timestamp(),dd_agent.document_pages.updated_at+interval '1 microsecond')''',
                (document_id,page['page_number'],version,page['status'],page['method'],page.get('text',''),Jsonb(page.get('lines',[])),page.get('image_key',''),page.get('error_code','')))

    def reading(self, document_id):
        # successful_pages 只表示当前基础阅读版本成功，不等于视觉增强或向量化成功。
        with self.connect() as conn:
            row=conn.execute('SELECT * FROM dd_agent.document_readings WHERE document_id=%s',(document_id,)).fetchone()
            if row is None:
                return {'status':'not_started','page_count':None,'successful_pages':0,'failed_pages':0}
            counts=conn.execute('''SELECT count(*) FILTER (WHERE status='succeeded') AS successful_pages,
                count(*) FILTER (WHERE status='failed') AS failed_pages FROM dd_agent.document_pages
                WHERE document_id=%s AND pipeline_version=%s''',(document_id,row['pipeline_version'])).fetchone()
            row.update(counts)
            row['status']='complete' if row['successful_pages']==row['page_count'] else 'partial' if row['successful_pages'] else 'failed' if row['failed_pages'] else 'not_started'
            return row

    def pages(self, document_id):
        # JOIN 当前阅读版本，避免把同一物理页的多个历史版本一并返回给当前页面。
        with self.connect() as conn:
            return conn.execute('''SELECT p.* FROM dd_agent.document_pages p
                JOIN dd_agent.document_readings r ON p.document_id=r.document_id AND p.pipeline_version=r.pipeline_version
                WHERE p.document_id=%s ORDER BY p.page_number''',(document_id,)).fetchall()

    def page_status(self, document_id, version, numbers):
        """本批页的状态投影：只取页号／状态／方式，不加载正文与行框。"""
        numbers = sorted({int(number) for number in numbers})
        if not numbers:
            return []
        with self.connect() as conn:
            return conn.execute('''SELECT page_number,status,method FROM dd_agent.document_pages
                WHERE document_id=%s AND pipeline_version=%s AND page_number=ANY(%s)''',
                (document_id, version, numbers)).fetchall()

    def reading_version(self, document_id):
        """只取页数与当前阅读版本；页读取边界用它，不做全卷进度统计。"""
        with self.connect() as conn:
            return conn.execute('''SELECT page_count,pipeline_version FROM dd_agent.document_readings
                WHERE document_id=%s''', (document_id,)).fetchone()

    def page_catalog(self, document_id, limit, after=0):
        """有界页目录（K02）：一条聚合＋一条本批页元数据，返回行数与全卷页数无关。"""
        with self.connect() as conn:
            summary = conn.execute('''SELECT r.page_count,r.pipeline_version AS reading_version,
                clock_timestamp() AS as_of,a.total_pages,a.succeeded_pages,a.failed_pages,
                a.status_revision,a.content_revision
                FROM dd_agent.document_readings r
                LEFT JOIN LATERAL (
                    SELECT count(*) AS total_pages,
                        count(*) FILTER (WHERE status='succeeded') AS succeeded_pages,
                        count(*) FILTER (WHERE status='failed') AS failed_pages,
                        md5(coalesce(string_agg(page_number::text||':'||status||':'||error_code,','
                            ORDER BY page_number),'')) AS status_revision,
                        md5(coalesce(string_agg(page_number::text||':'||source_revision,','
                            ORDER BY page_number) FILTER (WHERE status='succeeded'),'')) AS content_revision
                    FROM dd_agent.effective_document_pages WHERE document_id=r.document_id
                ) a ON true
                WHERE r.document_id=%s''', (document_id,)).fetchone()
            if summary is None:
                return None
            rows = conn.execute('''SELECT page_number,status,method,error_code,verification,source_revision,
                (image_key<>'') AS has_image FROM dd_agent.effective_document_pages
                WHERE document_id=%s AND page_number>%s ORDER BY page_number LIMIT %s''',
                (document_id, after, limit + 1)).fetchall()
            # 多取一条判断是否还有下一批，实际只返回 limit 条。
            return dict(summary=summary, items=rows[:limit], has_more=len(rows) > limit)

    def page_row(self, document_id, number, effective=True):
        """单页定位：一条 SQL 只取该物理页，不遍历整卷。"""
        with self.connect() as conn:
            if effective:
                # 与有效视图相同的最新记录选择；基础修订不匹配时没有生效机器记录。
                return conn.execute('''SELECT p.page_number,p.status,p.method,p.text,p.lines,p.image_key,p.error_code,
                    p.verification,p.raw_revision,p.source_revision,
                    CASE WHEN m.base_revision=p.raw_revision THEN m.id END AS machine_id
                    FROM dd_agent.effective_document_pages p
                    LEFT JOIN LATERAL (SELECT id,base_revision FROM dd_agent.machine_pages
                        WHERE document_id=p.document_id AND page_number=p.page_number
                        ORDER BY created_at DESC,id DESC LIMIT 1) m ON true
                    WHERE p.document_id=%s AND p.page_number=%s''', (document_id, number)).fetchone()
            return conn.execute('''SELECT p.page_number,p.status,p.method,p.text,p.lines,p.image_key,p.error_code,
                'unverified' AS verification,
                md5(p.pipeline_version||':'||p.updated_at::text) AS raw_revision,
                md5(p.pipeline_version||':'||p.updated_at::text) AS source_revision
                FROM dd_agent.document_pages p
                JOIN dd_agent.document_readings r ON r.document_id=p.document_id AND r.pipeline_version=p.pipeline_version
                WHERE p.document_id=%s AND p.page_number=%s''', (document_id, number)).fetchone()

    def page_history(self, document_id, number, limit, after=None, preview=500):
        """单页机器增强历史：只给条数与预览，不返回整段历史正文。"""
        stamp, identity = after if after else (None, None)
        with self.connect() as conn:
            return conn.execute('''SELECT id,created_at,profile_version,base_revision,
                result->>'verification' AS verification,
                length(result->>'text') AS text_length,
                left(result->>'text',%s) AS text_preview
                FROM dd_agent.machine_pages
                WHERE document_id=%s AND page_number=%s
                  AND (%s::timestamptz IS NULL OR (created_at,id)<(%s::timestamptz,%s::uuid))
                ORDER BY created_at DESC,id DESC LIMIT %s''',
                (preview, document_id, number, stamp, stamp, identity, limit + 1)).fetchall()

    def page_image_key(self, document_id, number):
        """预览图定位只查该页；取不到空串，调用方据此给 404，不加载其它页。"""
        with self.connect() as conn:
            row = conn.execute('''SELECT p.image_key FROM dd_agent.document_pages p
                JOIN dd_agent.document_readings r ON r.document_id=p.document_id AND r.pipeline_version=p.pipeline_version
                WHERE p.document_id=%s AND p.page_number=%s''', (document_id, number)).fetchone()
            return row['image_key'] if row else ''

    def page_machine_summary(self, document_id, number, machine_id):
        """只投影实际生效记录的元信息与历史数量，不取正文、历史数组或候选输出。"""
        with self.connect() as conn:
            return conn.execute('''SELECT
                (SELECT jsonb_build_object('outcome',result->'outcome','route',result->'route',
                    'regions',result->'regions') FROM dd_agent.machine_pages
                    WHERE id=%s AND document_id=%s AND page_number=%s) AS current,
                (SELECT count(*) FROM dd_agent.vision_candidates WHERE document_id=%s AND page_number=%s) AS candidate_count,
                (SELECT count(*) FROM dd_agent.page_adoptions WHERE document_id=%s AND page_number=%s) AS adoption_count''',
                (machine_id,document_id,number,document_id,number,document_id,number)).fetchone()

    def evidence_pages(self, document_id, pages=None):
        # 数据库视图按基础修订选择可用机器文字，否则回退基础文字；这里不重新推理。
        # 索引调用传入本批最多 20 页；不传 pages 的兼容路径仍返回全材料。
        # 这是内部仓储方法，尚不是供 Agent 使用的证据补读接口。
        with self.connect() as conn:
            if pages is not None:
                if not 1<=len(set(pages))<=20 or any(type(n) is not int or n<1 for n in pages):raise ValueError('页范围须为 1–20 页。')
                return conn.execute('SELECT * FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=ANY(%s) ORDER BY page_number',(document_id,pages)).fetchall()
            return conn.execute('SELECT * FROM dd_agent.effective_document_pages WHERE document_id=%s ORDER BY page_number', (document_id,)).fetchall()


@contextmanager
def database_scope(settings):
    """CLI 显式拥有一个池；提前 return／异常也会关闭，不依赖进程退出回收。"""
    from .db_connections import ConnectionProvider
    with ConnectionProvider(settings.database_url, max_size=settings.db_pool_size,
                            timeout=settings.db_pool_timeout, max_waiting=settings.db_pool_max_waiting) as provider:
        yield Database(settings.database_url, provider=provider)
