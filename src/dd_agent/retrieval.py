"""Versioned local evidence retrieval; model scores never confirm facts."""
# RAG 主入口：EvidenceService.index() 负责建库，search() 负责查库。
# 分工：本文件管理来源／版本／SQL；LocalModels 经独立 worker 执行实际模型推理。
# 主要数据：indexed_pages 是页级发布记录，evidence_chunks 保存文字片段及 1024 维向量。
# 生成报告不在这里执行；返回的是候选证据，不是模型生成的最终回答。
from contextlib import contextmanager
import hashlib
import json
import psycopg
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


def reuse_vectors(chunks, stored):
    """同一页、同一索引版本里文本完全相同的片段直接复用向量。返回 (向量或 None, 缺失文本, 缺失下标)。"""
    found = {}
    for text, vector in stored:
        found.setdefault(text, vector)
    vectors, missing_text, missing_at = [], [], []
    for index, chunk in enumerate(chunks):
        hit = found.get(chunk['text'])
        if hit is None:
            vectors.append(None)
            missing_text.append(chunk['text'])
            missing_at.append(index)
        else:
            vectors.append(hit)
    return vectors, missing_text, missing_at


def parse_stored_vector(value):
    if isinstance(value, list):
        return [float(item) for item in value]
    text = str(value).strip()
    if text.startswith('[') and text.endswith(']'):
        body = text[1:-1].strip()
        return [] if not body else [float(part) for part in body.split(',')]
    raise RetrievalError('已保存的向量无法读取。')


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
        # 调用方传入的 device／dtype 覆盖配置。必须先取出，不能和下面的同名参数一起展开。
        device = payload.pop('device', None)
        dtype = payload.pop('dtype', None)
        if device is None:
            device = self.settings.reranker_device if mode == 'rerank' else self.settings.embedding_device
        if dtype is None:
            dtype = self.settings.reranker_dtype if mode == 'rerank' else self.settings.embedding_dtype
        manifest = dict(mode=mode, embedding=str(self.settings.embedding_dir.resolve()),
                        reranker=str(self.settings.reranker_dir.resolve()), threads=self.settings.model_cpu_threads,
                        device=device, dtype=dtype, manifest_dir=str(cache_directory(self.settings)), **payload)
        if mode in {'embed', 'embed_pages'}:
            manifest.setdefault('token_budget', self.settings.embed_token_budget)
            manifest.setdefault('batch_limit', self.settings.embed_batch_limit)
        elif mode == 'rerank':
            manifest.setdefault('token_budget', self.settings.rerank_token_budget)
        try:
            if self.managed and mode!='profile':
                forward = dict(payload)
                if mode in {'embed', 'embed_pages'}:
                    forward.setdefault('token_budget', self.settings.embed_token_budget)
                    forward.setdefault('batch_limit', self.settings.embed_batch_limit)
                elif mode == 'rerank':
                    forward.setdefault('token_budget', self.settings.rerank_token_budget)
                yield from runtime_records(self.settings,'reranker' if mode=='rerank' else 'embedding',mode,forward)
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
        # float32 的缓存键不包含设备：GPU 与 CPU 共用已有索引。只有实际精度离开 float32 才换键。
        from .devices import describe_torch, probe_torch
        probed = probe_torch(self.settings.retrieval_python)
        capability = tuple(probed['capability']) if probed.get('capability') else None
        spec = describe_torch(self.settings.embedding_device, self.settings.embedding_dtype, cuda_available=probed['cuda'],
                              capability=capability, bf16_supported=bool(probed.get('bf16')))
        def compute():
            return self.one('profile', device=spec['device'], dtype=spec['dtype'])['version']
        parameters = dict(embedding=str(self.settings.embedding_dir.resolve()))
        if spec['dtype'] != 'float32':
            parameters['dtype'] = spec['dtype']
        # worker 脚本参与清单：指纹算法改动即失效；环境版本无法识别时退化为完整复验。
        entries = file_entries(model_files(self.settings) + [WORKER_SCRIPT, Path(__file__)])
        return resolve_fingerprint('embedding_weights', venv_runtime_descriptor(self.settings.retrieval_python),
            'embedding-profile-v1', parameters,
            entries, compute, verify=verify, directory=cache_directory(self.settings))['value']

    def one(self, mode, **payload):
        # 不只取第一条输出：必须确认进程完整退出，防止成功一行后崩溃仍被当作完成。
        # Exhaust the generator: a first JSON line does not prove a successful exit.
        records = list(self.records(mode, **payload))
        if len(records) != 1:
            raise RetrievalError('检索工作进程返回结果数量不符。')
        return records[0]

    def embed_pages(self, pages):
        # 多页片段一次送进模型，按 token 预算合批。先取完结果再交给发布，避免发布事务占着模型许可。
        if not pages:
            return
        with span('page.embedding', page=pages[0].get('page_number') if type(pages[0].get('page_number')) is int else None):
            records = list(self.records('embed_pages', pages=list(pages),
                token_budget=self.settings.embed_token_budget, batch_limit=self.settings.embed_batch_limit))
        yield from records

    @timed('embedding.request')
    def embed(self, texts, query=False):
        return self.one('embed', texts=texts, query=query, token_budget=self.settings.embed_token_budget,
                       batch_limit=self.settings.embed_batch_limit)['vectors']

    @timed('rerank.request')
    def rerank(self, query, texts):
        return self.one('rerank', query=query, texts=texts, token_budget=self.settings.rerank_token_budget)['scores']


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
        # 视觉之后文本没变的片段复用原向量。整批都命中时不再加载嵌入模型。
        plans = {}
        requests = []
        for number, chunks in pending.items():
            stored = []
            try:
                with self.database.connect() as conn:
                    rows = conn.execute('''SELECT text, embedding::text AS embedding FROM dd_agent.evidence_chunks
                        WHERE document_id=%s AND page_number=%s AND index_version=%s ORDER BY ordinal''',
                        (document_id, number, version)).fetchall()
                for row in rows:
                    try: stored.append((row['text'], parse_stored_vector(row['embedding'])))
                    except (RetrievalError, TypeError, ValueError): continue
            except (OSError, psycopg.Error):
                stored = []
            vectors, missing_text, missing_at = reuse_vectors(chunks, stored)
            plans[number] = (vectors, missing_at)
            count('embedding.chunks.reused', len(chunks) - len(missing_at))
            if missing_text:
                requests.append(dict(page_number=number, texts=missing_text))
                count('embedding.chunks.missed', len(missing_text))
        if not requests:
            count('embedding.reload_skipped')

        def publish_number(number, vectors):
            try:
                self.publish(document_id, snapshots[number], version, pending[number], vectors)
                if pending[number]: mark('index.first_searchable')
                count('pages.index.published')
                result['indexed_pages'].append(number)
                if checkpoint:checkpoint('embedding', number)
            except RetrievalError:
                result['failed_pages'].append(number)

        for number, (vectors, missing_at) in plans.items():
            if not missing_at:
                publish_number(number, vectors)
        outputs = None
        try:
            if requests:
                outputs = self.models.embed_pages(requests) if hasattr(self.models, 'embed_pages') else (
                    dict(page_number=p['page_number'], vectors=self.models.embed(p['texts'])) for p in requests)
            fresh = {}
            for output in outputs or []:
                number = output['page_number']
                if number not in plans or number in fresh:
                    raise RetrievalError('向量工作进程返回页码不符。')
                fresh[number] = output['vectors']
            for number, (vectors, missing_at) in plans.items():
                if not missing_at or number in result['indexed_pages'] or number in result['failed_pages']:
                    continue
                rows = fresh.get(number)
                if rows is None or len(rows) != len(missing_at):
                    result['failed_pages'].append(number)
                    continue
                for slot, vector in zip(missing_at, rows):
                    vectors[slot] = vector
                publish_number(number, vectors)
        except RetrievalError:
            pass  # Successfully committed pages survive a later worker failure.
        finally:
            if outputs is not None and hasattr(outputs,'close'):outputs.close()
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
