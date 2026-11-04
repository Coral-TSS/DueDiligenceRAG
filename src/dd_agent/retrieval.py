"""Versioned local evidence retrieval; model scores never confirm facts."""
# RAG 主入口：EvidenceService.index() 负责建库，search() 负责查库。
# 分工：本文件管理来源／版本／SQL；LocalModels 经独立 worker 执行实际模型推理。
# 主要数据：indexed_pages 是页级发布记录，evidence_chunks 保存文字片段及 1024 维向量。
# 生成报告不在这里执行；返回的是候选证据，不是模型生成的最终回答。
from contextlib import contextmanager
import hashlib
import json
from time import monotonic
import math
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from .cache import cache_directory, file_entries, resolve_fingerprint, venv_runtime_descriptor
from .model_results import ModelResults
from .model_admission import request_scope, current_control, AdmissionError
from .control import ProcessingDeferred
from .search_queries import CURRENT, SearchQueries
from .config import Settings
from .db import Database
from .reading import worker_records, ReadingError
from .performance import observed, timed, span, count, mark


CHUNK_VERSION = 'page-paragraph-700-overlap80-v1'
# 改变分块规则时同步修改版本，避免相同模型下的新旧分块被视为相同索引。
# 与 workers/retrieval_worker.py 的 profile() 筛选保持一致：改动任一侧须同步调整。
PROFILE_SUFFIXES = {'.json', '.safetensors', '.txt'}
WORKER_SCRIPT = Path(__file__).parent / 'workers' / 'retrieval_worker.py'


def model_files(settings):
    """指纹参与文件：权重清单不含正文，且只按后缀与 .cache 目录过滤。"""
    directory = Path(settings.embedding_dir)
    return sorted(p for p in directory.rglob('*')
                  if p.is_file() and p.suffix in PROFILE_SUFFIXES and '.cache' not in p.parts)


class RetrievalError(Exception):
    pass


class RetrievalBusy(RetrievalError):
    pass


class SearchCancelled(RetrievalError):
    pass


class SearchTimeout(RetrievalError):
    pass


@timed('chunk')
def split_text(text):
    """Prefer paragraph boundaries; preserve exact source slices and long-paragraph overlap."""
    # 输入是一页有效文字，长度单位是 Python 字符，不是 tokenizer 的 token。
    # 每块最多 700 字符，优先在后半段的换行处分开；硬切长段落时重叠 80 字符。
    # start_offset 从 0 起，end_offset 不包含末尾字符，可直接 text[start:end] 回查原文。
    # 当前不跨页，也没有识别法律条款／表格结构；不要误以为这是语义分块。
    chunks = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start == len(text):
            break
        end = min(start + 700, len(text))
        boundary = text.rfind('\n', start + 350, end)
        if end < len(text) and boundary > start:
            end = boundary + 1
        trimmed = end
        while trimmed > start and text[trimmed - 1].isspace():
            trimmed -= 1
        chunks.append(dict(text=text[start:trimmed], start_offset=start, end_offset=trimmed))
        if end == len(text):
            break
        start = end if text[end - 1].isspace() else end - 80
    count('chunks.created', len(chunks))
    return chunks


def valid_vector(values):
    # 维度与数据库 vector(1024) 保持一致；阻止 NaN、无穷及零向量进入距离计算。
    if not isinstance(values, list) or len(values) != 1024:
        raise RetrievalError('向量维度不符，请检查本地检索模型。')
    if any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in values):
        raise RetrievalError('向量含无效数值。')
    if not any(values):
        raise RetrievalError('向量不能全为零。')
    return '[' + ','.join(str(float(v)) for v in values) + ']'


@contextmanager
def model_slot(directory, *, managed=False):
    """One local retrieval operation across application/CLI processes sharing data_dir."""
    # 这是跨进程的文件锁，保护本机 CPU／内存预算；不是数据库队列执行锁。
    # review.py 的视觉调用也复用它，所以视觉增强、索引和查询会竞争同一资源。
    # 一次性路径非阻塞拒绝；常驻路径的有界等待由共享运行时负责。
    if managed:
        # The shared runtime holds this same file lease for the whole residency.
        yield
        return
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'retrieval.lock').open('a+b') as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        try:
            with span('resource.acquire'):
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            count('resource.busy')
            if current_control().defer_busy:raise ProcessingDeferred() from None
            raise RetrievalBusy('本地检索模型正在处理其他任务，请稍后重试。') from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


class LocalModels:
    # 将环境、模型目录和输入交给共享运行时或一次性 worker；实例由运行时持有。
    def __init__(self, settings):
        self.settings = settings

    @property
    def managed(self):return self.settings.model_runtime_enabled

    def cache_profile(self,kind):
        from .model_runtime import worker_spec
        return worker_spec(self.settings,kind).profile

    def records(self, mode, **payload):
        from .model_runtime import runtime_records, ModelRuntimeError
        manifest = dict(mode=mode, embedding=str(self.settings.embedding_dir.resolve()),
                        reranker=str(self.settings.reranker_dir.resolve()), **payload)
        try:
            if self.managed and mode!='profile':
                yield from runtime_records(self.settings,'reranker' if mode=='rerank' else 'embedding',mode,payload)
                return
            with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as directory:
                if mode=='profile':
                    yield from worker_records(self.settings.retrieval_python,'retrieval_worker',manifest,directory)
                else:
                    with model_slot(self.settings.data_dir):
                        # Exhaust this one-shot response before releasing its model lease.
                        records=list(worker_records(self.settings.retrieval_python,'retrieval_worker',manifest,directory))
                    yield from records
        except ModelRuntimeError as exc:
            current_control().check()
            if str(exc)=='model_busy':
                if current_control().defer_busy:raise ProcessingDeferred() from None
                raise RetrievalBusy('本地模型繁忙，请稍后重试。') from None
            raise RetrievalError('本地检索模型运行失败，请稍后重试。') from None
        except ReadingError:
            raise RetrievalError('本地检索模型运行失败；请检查模型路径、环境或缩小批次后重试。') from None

    @timed('fingerprint.embedding')
    def profile(self, verify=False):
        # worker 会读取权重计算指纹；它可防止版本混用，但每次重算都要启动进程并读全量权重。
        # 这里按“worker 环境依赖版本＋权重文件清单”缓存；命中时不启动 worker（RG-05b／K01）。
        def compute():
            return self.one('profile')['version']
        # worker 脚本参与清单：指纹算法改动即失效；环境版本无法识别时退化为完整复验。
        entries = file_entries(model_files(self.settings) + [WORKER_SCRIPT, Path(__file__)])
        return resolve_fingerprint('embedding_weights', venv_runtime_descriptor(self.settings.retrieval_python),
            'embedding-profile-v1', dict(embedding=str(self.settings.embedding_dir.resolve())),
            entries, compute, verify=verify, directory=cache_directory(self.settings))['value']

    def one(self, mode, **payload):
        # 不只取第一条输出：必须确认进程完整退出，防止成功一行后崩溃仍被当作完成。
        # Exhaust the generator: a first JSON line does not prove a successful exit.
        records = list(self.records(mode, **payload))
        if len(records) != 1:
            raise RetrievalError('检索工作进程返回结果数量不符。')
        return records[0]

    def embed_pages(self, pages):
        # 按页流式返回向量；上层可以逐页发布，后续页失败不回滚先前已提交页。
        for page in pages:
            # A page is a scheduling boundary: keep the resident, release admission.
            # Consume done before the caller opens its page publication transaction.
            yield from list(self.records('embed_pages',pages=[page]))

    @timed('embedding.request')
    def embed(self, texts, query=False):
        return self.one('embed', texts=texts, query=query)['vectors']

    @timed('rerank.request')
    def rerank(self, query, texts):
        return self.one('rerank', query=query, texts=texts)['scores']


# Both search and coverage require the current reading AND unchanged source page.
# CURRENT 是建库跳过与查询过滤共用的来源有效性条件：
# 当前阅读版本＋源时间＋源修订摘要＋Embedding/分块版本必须匹配，且页面成功。
# 不匹配的旧向量可能仍在表中留档，但不会被当前检索返回。



class EvidenceService:
    def __init__(self, settings=None, models=None, database=None):
        self.settings = settings or Settings.from_env()
        self.database = database if database is not None else Database(self.settings.database_url)
        self.models = models or LocalModels(self.settings)
        self.model_results = ModelResults()

    def version(self):
        # 索引版本由“分块规则＋Embedding 指纹”组成，不是任务进度或上传时间。
        return hashlib.sha256((CHUNK_VERSION + self.models.profile()).encode()).hexdigest()

    @observed('index')
    def index(self, project_id, document_id, pages, checkpoint=None):
        # 外部调用即使传了合法材料 UUID，也必须再次检查其项目归属。
        project_id, document_id = UUID(str(project_id)), UUID(str(document_id))
        if not pages or len(set(pages)) > 20 or any(not isinstance(n, int) or n < 1 for n in pages):
            raise ValueError('请指定 1–20 个有效物理页码。')
        if self.database.document(project_id, document_id) is None:
            raise RetrievalError('材料不存在或不属于当前项目。')
        version = self.version()
        snapshots = {p['page_number']: p for p in self.database.evidence_pages(document_id,pages=pages) if p['status'] == 'succeeded'}
        # 这里取的是当前有效文字快照（可能含机器增强），不是原始 OCR 表。
        # 仅读取本批最多 20 页，避免每批都把全卷文字搬进内存。
        with self.database.connect() as conn:
            indexed = {r['page_number'] for r in conn.execute('SELECT i.page_number ' + CURRENT + ' AND i.document_id=%s AND i.page_number=ANY(%s)', (project_id, version, document_id,pages))}
        result = dict(index_version=version, indexed_pages=[], skipped_pages=[], failed_pages=[])
        pending = {}
        empty = []   # 有效文字为空的页：没有可向量化片段，但仍要发布页级状态。
        for number in sorted(set(pages)):
            if number in indexed:
                result['skipped_pages'].append(number)
                count('pages.index.reused')
            elif number not in snapshots:
                result['failed_pages'].append(number)
            else:
                page = snapshots[number]
                if len(page['text']) > 100000:
                    result['failed_pages'].append(number)
                    continue
                chunks = split_text(page['text'])
                if chunks: pending[number] = chunks
                else: empty.append(number)
        # 空文字页直接发布零片段页状态，不进入 Embedding（PF-08）。
        # 页级发布记录不等于存在可检索的实质文字；不发布会被每批反复重试。
        for number in empty:
            try:
                self.publish(document_id, snapshots[number], version, [], [])
                result['indexed_pages'].append(number)
                count('pages.index.published_empty')
            except RetrievalError:
                result['failed_pages'].append(number)
        if not pending:
            # 全空批到此结束：没有片段就不加载模型，也不启动 worker。
            result['indexed_pages'] = sorted(result['indexed_pages'])
            count('pages.index.failed', len(result['failed_pages']))
            return result
        # 只有非空片段页进入模型；requests 不含空页。
        requests = [dict(page_number=n, texts=[c['text'] for c in chunks]) for n, chunks in pending.items()]
        try:
            outputs = self.models.embed_pages(requests) if hasattr(self.models, 'embed_pages') else (
                dict(page_number=p['page_number'], vectors=self.models.embed(p['texts'])) for p in requests)
            for output in outputs:
                number = output['page_number']
                if number not in pending or number in result['indexed_pages']:
                    raise RetrievalError('向量工作进程返回页码不符。')
                try:
                    self.publish(document_id, snapshots[number], version, pending[number], output['vectors'])
                    if pending[number]: mark('index.first_searchable')
                    count('pages.index.published')
                    # publish 已提交本页事务后才检查暂停；单页未发布的向量需下次重算。
                    result['indexed_pages'].append(number)
                    if checkpoint:checkpoint('embedding',number)
                except RetrievalError:
                    result['failed_pages'].append(number)
        except RetrievalError:
            pass  # Successfully committed pages survive a later worker failure.
        finally:
            if 'outputs' in locals() and hasattr(outputs,'close'):outputs.close()
        result['failed_pages'] = sorted(set(result['failed_pages']) | (set(pending) - set(result['indexed_pages'])))
        result['indexed_pages'] = sorted(result['indexed_pages'])
        count('pages.index.failed', len(result['failed_pages']))
        return result

    @timed('index.publish')
    def publish(self, document_id, snapshot, version, chunks, vectors):
        # 原子发布一页：替换该页同索引版本的记录，并写入全部片段。
        # 识别／增强可能在 Embedding 推理期间更新来源，因此写入前还要再次比较修订。
        if len(chunks) != len(vectors):
            raise RetrievalError('片段与向量数量不符。')
        encoded = [valid_vector(v) for v in vectors]
        number = snapshot['page_number']
        with self.database.publication('embedding',number,document_id=document_id) as conn:
            current = conn.execute('''SELECT p.updated_at,p.pipeline_version FROM dd_agent.document_pages p
                JOIN dd_agent.document_readings r ON r.document_id=p.document_id AND r.pipeline_version=p.pipeline_version
                WHERE p.document_id=%s AND p.page_number=%s AND p.status='succeeded' FOR UPDATE OF p,r''', (document_id, number)).fetchone()
            effective = conn.execute('SELECT * FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=%s', (document_id, number)).fetchone()
            if current is None or effective is None or effective['status'] != 'succeeded' or effective['source_revision'] != snapshot['source_revision']:
                raise RetrievalError('页面已更新，请重新建立该页索引。')
            conn.execute('DELETE FROM dd_agent.indexed_pages WHERE document_id=%s AND page_number=%s AND index_version=%s', (document_id, number, version))
            conn.execute('''INSERT INTO dd_agent.indexed_pages
                (document_id,page_number,index_version,pipeline_version,source_updated_at,source_revision) VALUES (%s,%s,%s,%s,%s,%s)''',
                (document_id, number, version, snapshot['pipeline_version'], snapshot['updated_at'], snapshot['source_revision']))
            for ordinal, (chunk, vector) in enumerate(zip(chunks, encoded, strict=True)):
                conn.execute('''INSERT INTO dd_agent.evidence_chunks
                    (id,document_id,page_number,index_version,ordinal,start_offset,end_offset,text,embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::public.vector)''',
                    (uuid4(), document_id, number, version, ordinal, chunk['start_offset'], chunk['end_offset'], chunk['text'], vector))

    def _model_result(self,kind,project_id,query,texts,compute,validate):
        control=current_control();control.check()
        profile=self.models.cache_profile(kind) if hasattr(self.models,'cache_profile') else None
        key=hashlib.sha256(json.dumps([str(project_id),kind,profile,query,texts],ensure_ascii=False).encode()).hexdigest()
        value=self.model_results.get(key) if profile is not None else None
        metric='search.'+kind+'_cache.'
        if value is not None:
            count(metric+'hit');validate(value);return value
        count(metric+'miss');value=compute();control.check();validate(value)
        if profile is not None:self.model_results.put(key,value)
        return value

    @observed('search')
    def search(self,project_id,query,*,cancel=None):
        project_id=UUID(str(project_id));query=query.strip()
        if not 1<=len(query)<=500:raise ValueError('查询须为 1–500 个字符。')
        try:
            with request_scope('interactive',cancel=cancel,deadline=monotonic()+self.settings.search_timeout_seconds):
                current_control().check()
                if self.settings.search_index_mode=='mixed':
                    # 延迟创建，不在启动诊断／page 路径加载父子 tokenizer 或模型。
                    # 同一服务共享数据库、模型与纯模型缓存；HTTP／CLI 都走此入口。
                    from .parent_index import ParentIndex
                    from .mixed_search import MixedSearch
                    index=ParentIndex(self.settings,database=self.database,models=self.models)
                    return MixedSearch(self,index,self.settings.search_parent_documents)._search(project_id,query)
                return self._search(project_id,query)
        except AdmissionError as exc:
            if exc.code=='cancelled':raise SearchCancelled('查询已取消。') from None
            if exc.code=='deadline':raise SearchTimeout('本次查询超时，请稍后重试。') from None
            raise RetrievalBusy('本地模型繁忙，请稍后重试。') from None

    def _search(self,project_id,query,*,project_checked=False):
        control=current_control();control.check()
        if not project_checked and not self.database.project_exists(project_id):raise RetrievalError('项目不存在。')
        version=self.version();control.check()
        queries=SearchQueries(self.database)
        coverage=queries.coverage(project_id,version)
        response=dict(index_version=version,coverage=coverage,results=[],status='not_indexed')
        if not coverage['indexed_pages']:return response
        def embed():
            values=self.models.embed([query],query=True)
            if len(values)!=1:raise RetrievalError('查询向量数量不符。')
            return values[0]
        vector=self._model_result('embedding',project_id,query,[],embed,valid_vector)
        control.check()
        rows=queries.candidates(project_id,version,valid_vector(vector))
        if rows:
            texts=[r['text'] for r in rows]
            def validate_scores(scores):
                if not isinstance(scores,list) or len(scores)!=len(rows) or any(not isinstance(v,(float,int)) or not math.isfinite(v) or not 0<=v<=1 for v in scores):
                    raise RetrievalError('重排序输出无效，请重试。')
            scores=self._model_result('reranker',project_id,query,texts,
                lambda:self.models.rerank(query,texts),validate_scores)
            for row,score in zip(rows,scores,strict=True):row['rerank_score']=score
        control.check()
        # Even a warm model cache never skips fresh SQL recall or source checks.
        coverage,live=queries.finalize(project_id,version,[r['id'] for r in rows])
        control.check()
        current=[]
        for row in rows:
            if row['id'] not in live:continue
            row.update(live[row['id']])
            row['source_url']=f"/reader?project={project_id}&document={row['document_id']}&page={row['page_number']}"
            current.append(row)
        response.update(coverage=coverage,results=sorted(current,key=lambda r:(-r['rerank_score'],-r['similarity'],str(r['id'])))[:5],
            status='candidates' if current else 'no_matches')
        return response
