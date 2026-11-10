# 本地 Web 应用入口：负责参数检查、项目边界、HTTP 错误与页面资源，不直接跑模型算法。
# 用户动作 -> 本文件路由 -> Storage／Database／Pipeline／EvidenceService。
# 三个主页面：/ 材料工作区，/reader 识别与任务进度，/search 证据查询。
# 上传成功只是保存／登记；自动处理接口返回 202 也只是任务已接受，不表示全卷完成。
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal
from uuid import UUID
from threading import Event
import anyio

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import psycopg
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .db import Database
from .storage import Storage, UploadTooLarge
from .retrieval import EvidenceService, RetrievalError, RetrievalBusy, SearchCancelled, SearchTimeout
from .review import ReviewService, ReviewError, ReviewConflict, ReviewNotFound
from .automatic import AutomaticProcessor
from .pipeline import ProcessingPipeline, JobManager
from .materials import MaterialCatalog
from .material_metadata import MetadataError, parse_filters, validate_category, validate_date_fields
from .page_queries import PageReader, PageQueryError, ERROR_STATUS
from .coverage_gaps import CoverageGaps
from .evidence_read import EvidenceReader
from .office import OfficeIndex
from .parent_references import ParentReferences, ParentReferenceError


# 旧无界页列表的安全范围：超出后必须改用 page-catalog／pages/{number}，不静默返回残缺数组。
LEGACY_PAGES_LIMIT = 100
from .performance import PerformanceMiddleware
from .db_connections import ConnectionProvider, DatabaseBusy


class ProjectInput(BaseModel):
    # HTTP 输入先验证，避免空白名称等无意义数据进入数据库。
    name: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 200:
            raise ValueError("项目名称须为 1–200 个字符，不能只包含空格。")
        return value


class SearchInput(BaseModel):
    # 限制问题长度；当前查询返回候选证据，不是调用生成模型写回答。
    # 新过滤字段都可选。旧客户端只传 query 时行为不变。
    query: str
    document_id: UUID | None = None
    document_ids: list[UUID] | None = None
    page_from: int | None = None
    page_to: int | None = None
    categories: list[str] | None = None
    subject_ids: list[UUID] | None = None
    date_point: date | None = None
    period_start: date | None = None
    period_end: date | None = None

    @field_validator('query')
    @classmethod
    def valid_query(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 500:
            raise ValueError('查询须为 1–500 个字符。')
        return value


class SubjectInput(BaseModel):
    name: str
    role: str | None = None


class SubjectRenameInput(BaseModel):
    name: str


class SingleMetadataInput(BaseModel):
    category: str
    subject_id: UUID | None = None
    date_kind: str
    point_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None


class FlaggedCategory(BaseModel):
    set: bool = False
    value: str | None = None


class FlaggedSubject(BaseModel):
    set: bool = False
    value: UUID | None = None


class FlaggedDate(BaseModel):
    set: bool = False
    kind: str | None = None
    point_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None


class BatchMetadataInput(BaseModel):
    document_ids: list[UUID]
    category: FlaggedCategory | None = None
    subject: FlaggedSubject | None = None
    date: FlaggedDate | None = None






class UploadRequestLimit:
    """Bound the incoming multipart stream before it can fill temporary storage."""
    # 在请求流入口先限制整体体积，Storage.save() 再核对实际文件大小。
    # 不能只等 multipart 全部写到临时磁盘后再检查文件上限。
    def __init__(self, app, limit):
        self.app = app
        self.limit = limit + 64 * 1024  # Multipart headers; exact file limit is checked by Storage.

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not scope["path"].endswith("/documents"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "请求长度无效。"}, status_code=400)(scope, receive, send)
        if declared > self.limit:
            return await JSONResponse({"detail": "上传请求超过大小上限。"}, status_code=413)(scope, receive, send)
        received = 0
        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise HTTPException(413, "上传请求超过大小上限。")
            return message
        await self.app(scope, bounded_receive, send)


def public_document(document):
    # 存储键是内部路径，不交给浏览器拼接文件位置。
    return {k: v for k, v in document.items() if k != "storage_key"}


def create_app(settings=None):
    settings = settings or Settings.from_env()
    @asynccontextmanager
    async def lifespan(app):
        # 数据库就绪才启动队列；队列会收束中断任务，正常排队任务则继续调度。
        # 关闭浏览器页面不会触发这里的退出，停止后端服务才会调用 jobs.close()。
        provider.start()
        try:
            if app.state.database.health()['status']=='ready':app.state.jobs.wake()
            yield
        finally:
            app.state.jobs.close()
            from .model_runtime import close_runtime
            close_runtime(settings)
            # 已借出连接由 pool 在归还时关闭；在途后台的后续安全保存使用短连接。
            provider.close()
    app = FastAPI(title="尽调材料工作区", docs_url=None, redoc_url=None, lifespan=lifespan)
    provider = ConnectionProvider(settings.database_url, max_size=settings.db_pool_size,
                                  timeout=settings.db_pool_timeout, max_waiting=settings.db_pool_max_waiting)
    database = app.state.database = Database(settings.database_url, provider=provider)
    # 服务对象不加载权重；模型稍后按需进入共享运行时，最多驻留一个重模型。
    app.state.evidence = EvidenceService(settings, database=database)
    app.state.review = ReviewService(settings, database=database)
    app.state.automatic = AutomaticProcessor(settings, database=database)
    app.state.pipeline = ProcessingPipeline(settings, database=database)
    app.state.jobs = JobManager(settings, pipeline_factory=lambda: app.state.pipeline, database=database)
    storage = Storage(settings.data_dir, settings.max_upload_bytes)
    static = Path(__file__).parent / "static"
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.add_middleware(UploadRequestLimit, limit=settings.max_upload_bytes)

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        # 本机单人版的来源检查，防止其他网站在浏览器中直接发起写操作。
        # 这不是多人登录／授权系统；不要据此把服务直接暴露成公共服务。
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            expected = str(request.base_url).rstrip("/")
            if (origin is not None and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "请从本机工作区页面操作。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        return JSONResponse({"detail": "数据库暂不可用或尚未初始化，请检查页面顶部的连接提示后重试。"}, status_code=503)

    @app.exception_handler(DatabaseBusy)
    async def database_busy(request, exc):
        return JSONResponse({'detail': '数据库连接繁忙或暂不可用，请稍后重试。'}, status_code=503)

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": "无法读写本地文件，请检查数据目录权限和磁盘空间后重试。"}, status_code=503)

    @app.exception_handler(ReviewError)
    async def review_error(request, exc):
        code = 404 if isinstance(exc, ReviewNotFound) else 409 if isinstance(exc, ReviewConflict) else 503
        return JSONResponse({'detail':str(exc)}, status_code=code)

    @app.exception_handler(PageQueryError)
    async def page_query_error(request, exc):
        # 服务层只给领域错误码；状态码映射集中在这里，端口不散落在各路由。
        return JSONResponse({'detail': str(exc), 'code': exc.code},
                            status_code=ERROR_STATUS.get(exc.code, 503))

    @app.exception_handler(MetadataError)
    async def metadata_error(request, exc):
        return JSONResponse({'detail': str(exc), 'fields': exc.fields}, status_code=exc.status)

    @app.get("/api/health")
    def health():
        return {**database.health(), "max_upload_bytes": settings.max_upload_bytes}

    @app.get("/api/projects")
    def projects():
        return database.projects()

    @app.post('/api/projects/{project_id}/search')
    async def search(project_id: UUID, body: SearchInput, request: Request):
        # 和后续 MCP 共用的检索服务：召回、重排、版本排除都在 EvidenceService 内。
        cancelled=Event();result=None;error=None
        def execute():
            require_project(project_id)
            scope = parse_filters(
                document_id=body.document_id, document_ids=body.document_ids,
                page_from=body.page_from, page_to=body.page_to, categories=body.categories,
                subject_ids=body.subject_ids, date_point=body.date_point,
                period_start=body.period_start, period_end=body.period_end)
            return app.state.evidence.search(project_id, body.query, cancel=cancelled, scope=scope)
        async def disconnected():
            while True:
                # FastAPI has already consumed/validated the JSON body. Wait on
                # receive directly: is_disconnected's immediate cancellation
                # can miss disconnects through BaseHTTPMiddleware's stream.
                message=await request.receive()
                if message['type']=='http.disconnect':cancelled.set();return
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(disconnected)
                try:result=await anyio.to_thread.run_sync(execute,abandon_on_cancel=True)
                except Exception as exc:error=exc
                finally:
                    cancelled.set();tasks.cancel_scope.cancel()
            if error is not None:raise error
            return result
        except RetrievalBusy as exc:
            return JSONResponse({'detail':str(exc),'error_code':'model_busy'},status_code=503,headers={'Retry-After':'1'})
        except SearchTimeout as exc:
            return JSONResponse({'detail':str(exc),'error_code':'search_timeout'},status_code=504)
        except SearchCancelled as exc:
            return JSONResponse({'detail':str(exc),'error_code':'search_cancelled'},status_code=499)
        except RetrievalError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.get('/search', include_in_schema=False)
    def search_page():
        return FileResponse(static / 'search.html')

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectInput):
        return database.create_project(body.name)

    def require_project(project_id):
        # 前端传入的 project_id 不能直接信任；材料读取还要额外校验材料所属项目。
        if not database.project_exists(project_id):
            raise HTTPException(404, "项目不存在。")

    @app.get("/api/projects/{project_id}/documents")
    def documents(project_id: UUID):
        # 原有页面使用的完整材料列表。material-catalog 才是新增的有界分页接口。
        require_project(project_id)
        return [{**public_document(d), 'reading': database.reading(d['id'])} for d in database.documents(project_id)]

    @app.get('/api/projects/{project_id}/material-catalog')
    def material_catalog(project_id: UUID, limit: int = Query(default=50,ge=1,le=100), after: UUID | None = None,
                         categories: list[str] | None = Query(default=None),
                         subject_ids: list[UUID] | None = Query(default=None),
                         date_point: date | None = Query(default=None),
                         period_start: date | None = Query(default=None),
                         period_end: date | None = Query(default=None)):
        # 这里只列登记信息和已知阅读版本，不扫描全文，也不等于有效索引覆盖统计。
        # 类别、主体、日期过滤写在目录 SQL 的 WHERE 里。页范围不属于列表。
        try:
            scope = parse_filters(purpose='list', categories=categories, subject_ids=subject_ids,
                                  date_point=date_point, period_start=period_start, period_end=period_end)
            return MaterialCatalog(database,project_id).list_materials(limit,after,scope)
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from None

    @app.get('/api/projects/{project_id}/coverage')
    def coverage_gaps(project_id: UUID, kind: str | None = Query(default=None, max_length=32),
                      limit: int = Query(default=50, ge=1, le=100),
                      cursor: str | None = Query(default=None, max_length=120)):
        # 不经过检索。统计只读当前项目，索引版本只看已有指纹，不在这里启动模型。
        require_project(project_id)
        return jsonable_encoder(bounded_call(lambda: CoverageGaps(database, app.state.evidence).page(
            project_id, kind=kind, limit=limit, cursor=cursor)))

    @app.get('/api/projects/{project_id}/subjects')
    def list_subjects(project_id: UUID):
        require_project(project_id)
        return database.subjects(project_id)

    @app.post('/api/projects/{project_id}/subjects', status_code=201)
    def create_subject(project_id: UUID, body: SubjectInput):
        require_project(project_id)
        return database.create_subject(project_id, body.name, body.role)

    @app.patch('/api/projects/{project_id}/subjects/{subject_id}')
    def rename_subject(project_id: UUID, subject_id: UUID, body: SubjectRenameInput):
        require_project(project_id)
        try:
            return database.rename_subject(project_id, subject_id, body.name)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    def metadata_changes(body: SingleMetadataInput):
        changes = validate_date_fields(body.date_kind, body.point_date, body.period_start, body.period_end)
        changes['category'] = validate_category(body.category)
        changes['subject_id'] = body.subject_id
        return changes

    @app.patch('/api/projects/{project_id}/documents/{document_id}/metadata')
    def update_metadata(project_id: UUID, document_id: UUID, body: SingleMetadataInput):
        require_project(project_id)
        try:
            updated = database.apply_metadata(project_id, [document_id], metadata_changes(body))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return {'document': public_document(updated[0])}

    @app.post('/api/projects/{project_id}/documents/metadata')
    def update_metadata_batch(project_id: UUID, body: BatchMetadataInput):
        require_project(project_id)
        changes = {}
        if body.category is not None and body.category.set:
            changes['category'] = validate_category(body.category.value)
        if body.subject is not None and body.subject.set:
            changes['subject_id'] = body.subject.value
        if body.date is not None and body.date.set:
            changes.update(validate_date_fields(body.date.kind, body.date.point_date, body.date.period_start, body.date.period_end))
        try:
            updated = database.apply_metadata(project_id, body.document_ids, changes)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return {'documents': [public_document(row) for row in updated]}

    @app.post("/api/projects/{project_id}/documents")
    def register(project_id: UUID, file: UploadFile):
        # 先落盘再登记元数据；Storage 不持有数据库，Database 不负责读取上传流。
        require_project(project_id)
        try:
            stored = storage.save(project_id, file.filename, file.file)
        except UploadTooLarge:
            raise HTTPException(413, "文件超过大小上限，请缩小文件或调整 DDA_MAX_UPLOAD_MB。") from None
        finally:
            file.file.close()
        doc, duplicate = database.register(project_id, stored)
        return JSONResponse(jsonable_encoder({"document": public_document(doc), "duplicate": duplicate}), status_code=200 if duplicate else 201)

    @app.get("/api/projects/{project_id}/documents/{document_id}/file")
    def download(project_id: UUID, document_id: UUID):
        document = database.document(project_id, document_id)
        if document is None:
            raise HTTPException(404, "材料不存在或不属于当前项目。")
        path = storage.path(document["storage_key"])
        if not path.is_file():
            raise HTTPException(404, "原件文件缺失，请检查数据目录。")
        return FileResponse(path, filename=document["filenames"][0], media_type="application/octet-stream")

    def require_document(project_id, document_id):
        # 同时带项目和材料查询，防止仅知道别的项目材料 UUID 就取回原件或页面。
        document = database.document(project_id, document_id)
        if document is None:
            raise HTTPException(404, '材料不存在或不属于当前项目。')
        return document

    @app.get('/api/projects/{project_id}/documents/{document_id}/reading')
    def reading_status(project_id: UUID, document_id: UUID):
        document = require_document(project_id, document_id)
        return {**database.reading(document_id), 'filenames': document['filenames']}

    @app.get('/api/projects/{project_id}/documents/{document_id}/pages')
    def reading_pages(project_id: UUID, document_id: UUID):
        # 旧前端的无界接口，仅保留给安全范围内的材料；超出时明确要求改用有界入口。
        require_document(project_id, document_id)
        reading = database.reading_version(document_id)
        if reading and reading['page_count'] > LEGACY_PAGES_LIMIT:
            raise PageQueryError(
                'bounded_read_required',
                f'该材料共 {reading["page_count"]} 页，旧接口不再返回整卷；'
                '请改用 page-catalog 分页与 pages/{number} 单页读取。')
        return [{k:v for k,v in page.items() if k!='image_key'} for page in database.pages(document_id)]

    def bounded_call(function):
        # 服务层只返回领域错误；参数类 ValueError 在这里统一转 422。
        try:
            return function()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/projects/{project_id}/documents/{document_id}/parent-builds/{build_id}/parents/{parent_id}')
    def parent_reference(project_id: UUID, document_id: UUID,
                         build_id: str = PathParam(pattern='^[0-9a-f]{64}$'),
                         parent_id: str = PathParam(pattern='^[0-9a-f]{64}$'),
                         index_version: str = Query(pattern='^[0-9a-f]{64}$'),
                         offset: int = Query(default=0, ge=0, le=4000),
                         limit: int = Query(default=4000, ge=1, le=4000)):
        try:
            return bounded_call(lambda: ParentReferences(database).read(project_id,document_id,
                build_id,parent_id,index_version,offset=offset,limit=limit))
        except ParentReferenceError as exc:
            return JSONResponse({'detail':str(exc),'error_code':exc.code},
                                status_code=404 if exc.code=='not_found' else 409)

    @app.get('/api/projects/{project_id}/evidence/{evidence_id}')
    def read_evidence(project_id: UUID, evidence_id: UUID,
                      offset: int = Query(default=0, ge=0),
                      limit: int = Query(default=4000, ge=1, le=8000),
                      index_version: str | None = Query(default=None, min_length=64, max_length=64)):
        reader = EvidenceReader(database, settings)
        return jsonable_encoder(bounded_call(lambda: reader.by_id(
            project_id, evidence_id, offset=offset, limit=limit, index_version=index_version)))

    @app.get('/api/projects/{project_id}/documents/{document_id}/evidence-range')
    def read_evidence_range(project_id: UUID, document_id: UUID,
                            page_from: int | None = Query(default=None, ge=1),
                            page_to: int | None = Query(default=None, ge=1),
                            resume_page: int | None = Query(default=None, ge=1),
                            offset: int = Query(default=0, ge=0),
                            limit: int = Query(default=4000, ge=1, le=8000),
                            source_revision: str | None = Query(default=None, max_length=64),
                            resume_unit: UUID | None = None):
        reader = EvidenceReader(database, settings)
        return jsonable_encoder(bounded_call(lambda: reader.by_range(
            project_id, document_id, page_from=page_from, page_to=page_to, resume_page=resume_page,
            offset=offset, limit=limit, source_revision=source_revision, resume_unit=resume_unit)))

    @app.get('/api/projects/{project_id}/documents/{document_id}/source-units/{unit_id}')
    def source_unit(project_id: UUID, document_id: UUID, unit_id: UUID, index_version: str = Query(min_length=64, max_length=64)):
        row = OfficeIndex(settings, database, app.state.evidence.models).read(project_id, document_id, unit_id, index_version)
        if row is None:
            raise HTTPException(404, '材料不存在或不属于当前项目。')
        return {k: v for k, v in row.items() if k != 'storage_key'}

    @app.get('/api/projects/{project_id}/documents/{document_id}/page-catalog')
    def page_catalog(project_id: UUID, document_id: UUID,
                     limit: int = Query(default=50, ge=1, le=100),
                     after: int | None = Query(default=None, ge=0)):
        # 有界页目录（K02）：只给页号／状态／来源版本，不含正文、行框或存储路径。
        reader = PageReader(database, project_id, document_id)
        return bounded_call(lambda: reader.catalog(limit, after))

    @app.get('/api/projects/{project_id}/documents/{document_id}/pages/{page_number}')
    def page_read(project_id: UUID, document_id: UUID,
                  page_number: int = PathParam(ge=1),
                  view: Literal['raw', 'effective'] = 'effective',
                  offset: int = Query(default=0, ge=0),
                  source_revision: str | None = Query(default=None, max_length=64),
                  include_lines: bool = False):
        # 深链接直接取该页，不遍历前面的页；超过 100,000 字符按 next_offset 续读。
        reader = PageReader(database, project_id, document_id)
        return bounded_call(lambda: reader.page(page_number, view, offset, source_revision, include_lines))

    @app.get('/api/projects/{project_id}/documents/{document_id}/pages/{page_number}/history')
    def page_history(project_id: UUID, document_id: UUID,
                     page_number: int = PathParam(ge=1),
                     limit: int = Query(default=10, ge=1, le=50),
                     after: str | None = Query(default=None, max_length=90)):
        # 最新状态在单页接口；这里只列该页有界的机器增强历史。
        reader = PageReader(database, project_id, document_id)
        return bounded_call(lambda: reader.history(page_number, limit, after))

    @app.get('/api/projects/{project_id}/documents/{document_id}/pages/{page_number}/summary')
    def page_summary(project_id: UUID, document_id: UUID, page_number: int = PathParam(ge=1),
                     source_revision: str | None = Query(default=None, max_length=64)):
        reader = PageReader(database, project_id, document_id)
        return bounded_call(lambda: reader.summary(page_number, source_revision))

    @app.get('/api/projects/{project_id}/documents/{document_id}/pages/{page_number}/image')
    def page_image(project_id: UUID, document_id: UUID, page_number: int):
        require_document(project_id, document_id)
        # 单页定位：不再先取回整卷页记录再筛某一页。
        key = database.page_image_key(document_id, page_number)
        path = storage.path(key) if key else None
        from .reading import Reader, ReadingError
        # 识别图键还在、文件丢了：按原参数补图，不重新 OCR。
        # 文字页没有图键：第一次浏览再生成预览。没有阅读结果仍是 404。
        if key and (path is None or not path.is_file()):
            try:
                key = Reader(settings, database).ensure_preview(project_id, document_id, page_number)
            except ReadingError as exc:
                raise HTTPException(404, str(exc)) from None
            path = storage.path(key)
        elif not key:
            row = database.page_row(document_id, page_number)
            if row and row['status'] == 'succeeded' and row['method'] == 'text':
                try:
                    key = Reader(settings, database).ensure_preview(project_id, document_id, page_number)
                    path = storage.path(key)
                except ReadingError:
                    raise HTTPException(404, '该页尚无预览图，请先完成该页阅读。') from None
            else:
                raise HTTPException(404, '该页尚无预览图，请先完成该页阅读。')
        if not path or not path.is_file():
            raise HTTPException(404, '页面预览缺失，请重新读取该页。')
        return FileResponse(path, media_type='image/png')

    @app.get('/reader', include_in_schema=False)
    def reader_page():
        return FileResponse(static / 'reader.html')

    @app.get('/api/projects/{project_id}/documents/{document_id}/pages/{page_number}/review')
    def review_state(project_id: UUID, document_id: UUID, page_number: int):
        return app.state.review.state(project_id, document_id, page_number)

    @app.get('/api/projects/{project_id}/documents/{document_id}/pages/{page_number}/automatic')
    def automatic_state(project_id: UUID, document_id: UUID, page_number: int):
        require_document(project_id, document_id)
        return app.state.automatic.state(project_id, document_id, page_number)

    @app.get('/api/projects/{project_id}/documents/{document_id}/processing')
    def processing_state(project_id: UUID, document_id: UUID):
        # latest() 返回数据库状态＋执行锁的派生判断；读取状态不强制继续任务。
        require_document(project_id, document_id)
        return app.state.jobs.latest(project_id, document_id)

    @app.post('/api/projects/{project_id}/documents/{document_id}/processing', status_code=202)
    def process_document(project_id: UUID, document_id: UUID):
        # 耗时工作交给数据库队列，避免一个 HTTP 请求一直等到几百页 OCR 完成。
        require_document(project_id, document_id)
        return app.state.jobs.enqueue(project_id, document_id)

    @app.post('/api/projects/{project_id}/documents/{document_id}/processing/pause')
    def pause_document(project_id: UUID, document_id: UUID):
        require_document(project_id, document_id)
        return app.state.jobs.pause(project_id, document_id)

    @app.post('/api/projects/{project_id}/documents/{document_id}/processing/resume', status_code=202)
    def resume_document(project_id: UUID, document_id: UUID):
        require_document(project_id, document_id)
        return app.state.jobs.resume(project_id, document_id)



    @app.post('/api/projects/{project_id}/documents/{document_id}/processing/stages/{stage}/{action}')
    def control_processing_stage(project_id: UUID, document_id: UUID,
                                 stage: Literal['reading','vision','embedding'], action: Literal['pause','resume']):
        require_document(project_id,document_id)
        result=app.state.jobs.control_stage(project_id,document_id,stage,pause=action=='pause')
        if action=='resume':app.state.jobs.wake()
        return result

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    app.add_middleware(PerformanceMiddleware, directory=settings.data_dir / 'performance', enabled=settings.performance_enabled)
    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
