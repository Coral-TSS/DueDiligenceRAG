# 基础阅读层：登记原件 -> PDFium 预检／取文字／渲染 -> 必要时 PaddleOCR -> 保存基础页。
# PDF 页码在业务接口中从 1 开始；原始 OCR 与后续机器增强分开保存。
# “.venv-docling”是当前环境目录名，实际 pdf_worker 使用 pypdfium2，不等于已接 Docling 全能力。
import json
from contextlib import closing
from contextvars import copy_context
import hashlib
import math
import os
from pathlib import Path
from queue import Queue
import re
import subprocess
import tempfile
from threading import Lock, Thread, Timer
from uuid import UUID, uuid4

from .cache import cache_directory, file_entries, inspect_document, resolve_fingerprint
from .config import Settings
from .db import Database
from .storage import Storage, _resolved_path
from .performance import active, accept_worker, worker_process, span, timed, observed, count
from .model_admission import current_control
from .control import ProcessingDeferred
from .maintenance import LogPump, append_error, rotate_log, split_log_paths
from .image_artifacts import (
    DISK_BYTES, INFLIGHT_PIXELS, SINGLE_IMAGE_PIXELS, ArtifactError, ArtifactLimitError,
    TRANSFORM_VERSION, GenerationLease, admit_image, assert_recognition_input, artifact_key,
    dpi_for_scale, inspect_artifact, plan_pages, png_bytes, seal_rendered_png,
)


# 字符串里的 cpu 是历史算法编号，不是运行设备。GPU 与 CPU 的 OCR 文本已核对为一致，
# 设备、线程、oneDNN 都不写入阅读版本，避免换卡后把已成功页全部重跑。
PIPELINE_VERSION='pdfium-text-v2-ppocr5-cpu-v1'
OCR_MODEL_NAMES = ['PP-OCRv5_mobile_det','PP-OCRv5_server_rec','PP-LCNet_x1_0_doc_ori','PP-LCNet_x1_0_textline_ori']
OCR_MODEL_SUFFIXES = ['pdiparams','json','yml']
# 预检结果依赖 PDF 解析器行为；更换解析栈须同步调整该标记，不能只靠文件时间判断。
PREFLIGHT_PARSER = PIPELINE_VERSION


class ReadingError(Exception):
    pass


def parse_pages(value,total,limit=20):
    # 阅读／索引的单次调用仍最多 20 页。process-pages 传入材料总页数，由流水线按阶段拆开。
    if type(limit) is not int or limit<1:
        raise ValueError('页数上限无效。')
    pages=set()
    for item in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?',item.strip()):
            raise ValueError('页码格式须为 1-5,8。')
        parts=[int(x) for x in item.strip().split('-')]
        first,last=parts[0],parts[-1]
        if first<1 or last<first or last>total or last-first>=limit:
            raise ValueError('页码超出范围，或超过本次页数上限。')
        pages.update(range(first,last+1))
        if len(pages)>limit:raise ValueError('超过本次页数上限。')
    return sorted(pages)


def normalize_ocr(payload):
    # 将 Paddle 输出统一成 text＋lines。文字、分数、四点坐标必须一一对应。
    # score 只是 OCR 识别分数，不是法律事实正确率，后续不能据此直接确认事实。
    lines=[]
    try:
        for text,score,box in zip(payload.get('rec_texts',[]),payload.get('rec_scores',[]),payload.get('rec_polys',[]),strict=True):
            if not text.strip():continue
            score=float(score)
            if not math.isfinite(score) or not 0<=score<=1:raise ValueError()
            if len(box)!=4 or any(len(point)!=2 or any(not math.isfinite(float(v)) for v in point) for point in box):raise ValueError()
            lines.append({'text':text,'score':score,'box':box})
    except (ValueError,TypeError,AttributeError):
        raise ReadingError('OCR 输出的文字、分数或区域不完整，请重试该页。') from None
    return {'text':'\n'.join(line['text'] for line in lines),'lines':lines}


def worker_records(python,worker,manifest,directory,timeout_seconds=1200):
    # 主应用不直接 import 各套模型依赖，而是使用对应虚拟环境的解释器启动 worker。
    # 输入：一个本地 JSON 清单；输出：stdout 每行一条 JSON；诊断：stderr 写本地日志。
    # 采用生成器才能一页完成就保存，不必等整个子进程运行完才得到全部结果。
    if not Path(python).is_file():raise ReadingError('工作进程 Python 未配置，请检查 DDA_PDF_PYTHON／DDA_OCR_PYTHON。')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    manifest_path=directory/(worker+'-'+uuid4().hex+'.json')
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    # 只传必要系统变量，不把主应用的数据库连接或模型服务凭据放进 worker 环境。
    env={key:value for key,value in os.environ.items() if key.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','COMSPEC'}}
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',HF_HUB_OFFLINE='1',PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True',PADDLE_PDX_CACHE_HOME=str(directory/'cache'),
        PYTORCH_CUDA_ALLOC_CONF=os.environ.get('PYTORCH_CUDA_ALLOC_CONF','expandable_segments:True'))
    if active(): env['DDA_WORKER_METRICS'] = '1'
    script=Path(__file__).parent/'workers'/(worker+'.py')
    logs=split_log_paths(directory, worker)
    rotate_log(logs['output'])
    process=subprocess.Popen([str(python),str(script),str(manifest_path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',env=env)
    # 普通诊断进 worker.log；失败摘要另写 worker.error.log，不抄页面正文。
    pump=LogPump(process.stderr, logs['output'])
    count('worker.'+worker+'.starts')
    timer=Timer(timeout_seconds,process.kill);timer.daemon=True;timer.start()
    try:
        with worker_process(process.pid) as register, span('worker.'+worker):
            for line in process.stdout:
                try: record = json.loads(line)
                except json.JSONDecodeError:raise ReadingError('工作进程输出无效，请检查本地处理日志。') from None
                if isinstance(record, dict) and '_dda_metric' in record:
                    metric = record['_dda_metric']
                    if isinstance(metric, dict) and 'runtime_pid' in metric:
                        register(metric['runtime_pid'])
                    else: accept_worker(metric)
                else: yield record
            if process.wait()!=0:
                append_error(logs['error'], category='worker_failed', location=worker, request_id='')
                raise ReadingError('PDF／OCR 工作进程失败，请检查本地处理日志。')
    finally:
        # 即使上层因暂停而提前关闭生成器，也要结束／回收子进程。
        # 否则文件锁已释放，旧模型却仍占着内存，下一次调用可能与它叠加。
        timer.cancel();process.stdout.close()
        if process.poll() is None:process.kill()
        process.wait()
        pump.close()


def ocr_records(settings,manifest,directory):
    from .model_runtime import runtime_records, ModelRuntimeError
    from .retrieval import model_slot, RetrievalError
    try:
        if settings.model_runtime_enabled:
            if manifest.get('pages') and any(not (settings.ocr_models_dir/name/'inference.pdiparams').is_file() for name in OCR_MODEL_NAMES):
                for page in manifest['pages']:yield dict(page_number=page['page_number'],error_code='ocr_models_missing')
                return
            pages=list(manifest.get('pages') or [])
            batch=max(1, int(getattr(settings, 'ocr_page_batch', 1) or 1))
            payload=dict(manifest, rec_batch=settings.ocr_rec_batch, page_batch=batch)
            if pages:
                for start in range(0, len(pages), batch):
                    group=pages[start:start+batch]
                    # 一批推理结束后再把结果交给保存，避免发布事务占着模型许可。
                    ready=list(runtime_records(settings,'ocr','recognize',dict(payload,pages=group)))
                    yield from ready
            else:yield from runtime_records(settings,'ocr','recognize',payload)
        else:
            with model_slot(settings.data_dir):
                yield from worker_records(settings.ocr_python,'ocr_worker',manifest,directory)
    except ModelRuntimeError as exc:
        if str(exc)=='model_busy' and current_control().defer_busy:raise ProcessingDeferred() from None
        raise ReadingError('本地 OCR 暂未完成，请稍后重试。') from None
    except RetrievalError:
        raise ReadingError('本地 OCR 暂未完成，请稍后重试。') from None


class PdfWorker:
    # inspect 会遍历 PDF 页面判断文字层及页数；它不执行视觉模型，也不建立索引。
    def __init__(self,settings=None):
        self.settings=settings or Settings()

    @timed('pdf.inspect.request')
    def inspect(self,source,identity=None,sha256=None,verify=False):
        # identity/sha256 为项目内材料身份与内容摘要；缺一则不缓存（例如 CLI 诊断）。
        def compute():
            with tempfile.TemporaryDirectory(prefix='dda-preflight-') as directory:
                results=list(worker_records(self.settings.pdf_python,'pdf_worker',{'mode':'inspect','source':str(Path(source).resolve())},directory))
            if not results or 'error_code' in results[0]:raise ReadingError('PDF 无法打开，可能损坏或加密。请检查原件。')
            return results[0]
        return inspect_document(identity, sha256, PREFLIGHT_PARSER, source, compute,
                                verify=verify, directory=cache_directory(self.settings))['value']


def ocr_model_files(settings):
    return [settings.ocr_models_dir/name/('inference.'+suffix)
            for name in OCR_MODEL_NAMES for suffix in OCR_MODEL_SUFFIXES]


@timed('fingerprint.ocr')
def ocr_fingerprint(settings,verify=False):
    # 摘要算法保持位级一致，只把结果放进缓存；命中时不读取权重内容。
    def compute():
        digest = hashlib.sha256()
        for name in OCR_MODEL_NAMES:
            for suffix in OCR_MODEL_SUFFIXES:
                file = settings.ocr_models_dir/name/('inference.'+suffix)
                digest.update((name+suffix).encode())
                if file.is_file():
                    with file.open('rb') as stream:
                        for block in iter(lambda:stream.read(1024*1024),b''):
                            digest.update(block); count('fingerprint.ocr.bytes', len(block))
                else: digest.update(b'missing')
        return digest.hexdigest()
    # 算法本体所在文件参与清单：改动实现即失效，不靠模型目录时间判断。
    entries = file_entries(ocr_model_files(settings) + [Path(__file__)])
    return resolve_fingerprint('ocr_weights', 'python-stdlib', 'ocr-weights-digest-v1',
        dict(names=OCR_MODEL_NAMES, suffixes=OCR_MODEL_SUFFIXES, models=str(settings.ocr_models_dir.resolve())),
        entries, compute, verify=verify, directory=cache_directory(settings))['value']


def render_plan(numbers, routes, exists, force_ocr, **kwargs):
    """识别页有完整图则复用。文字页默认不画，调用方传入 preview=True 才生成预览。"""
    return plan_pages(numbers, routes, exists, force_ocr, **kwargs)


def stable_render_path(root, document_id, number, scale, sha256):
    stamp = (sha256 or 'nosha')[:12]
    return Path(root) / 'readings' / str(document_id) / 'rendered' / f'page-{number}-x{scale:g}-{stamp}.png'


def image_key_for(root, image_path):
    """把渲染图收成相对数据目录的键。Windows 的 \\\\?\\ 前缀先去掉，避免和存储根对不上。"""
    image = _resolved_path(Path(image_path))
    base = root if isinstance(root, Path) else _resolved_path(Path(root))
    return image.relative_to(base).as_posix()


class Reader:
    # read() 写 document_pages（基础结果），不负责决定增强文字是否可信。
    def __init__(self,settings,database=None):
        self.settings=settings;self.database=database if database is not None else Database(settings.database_url)
        self.storage=Storage(settings.data_dir,settings.max_upload_bytes)

    def prepare(self,project_id,document_id,force_ocr=False,activate=True):
        project_id,document_id=UUID(str(project_id)),UUID(str(document_id))
        document=self.database.document(project_id,document_id)
        if document is None:raise ReadingError('材料不存在或不属于当前项目。')
        if document['status']!='registered':raise ReadingError('材料不是可处理的 PDF，请检查登记状态。')
        source=self.storage.path(document['storage_key'])
        # 预检按材料身份＋原件摘要缓存；同一原件不再逐批遍历全卷。
        profile=PdfWorker(self.settings).inspect(source,identity=str(document_id),sha256=document.get('sha256'))
        # 阅读版本绑定基础 OCR 权重、渲染比例及是否强制 OCR。
        # 参数变了就选择新版本，不能把旧结果直接算作本次已完成；旧版本仍保留。
        version=PIPELINE_VERSION+'-'+ocr_fingerprint(self.settings)[:16]+f'-render{self.settings.pdf_render_scale:g}'+('-force-ocr' if force_ocr else '')
        if activate:self.database.set_reading(document_id,profile['page_count'],version)
        return profile,version

    @observed('reading')
    def read(self,project_id,document_id,pages,force_ocr=False,checkpoint=None,prepared=None):
        project_id,document_id=UUID(str(project_id)),UUID(str(document_id))
        document=self.database.document(project_id,document_id)
        if document is None:raise ReadingError('材料不存在或不属于当前项目。')
        source=self.storage.path(document['storage_key'])
        profile,version=prepared if prepared is not None else self.prepare(project_id,document_id,force_ocr)
        pages=parse_pages(','.join(map(str,pages)),profile['page_count'])
        # 本批页状态用字段投影读取，不先取回整卷正文再判断跳过。
        existing={r['page_number'] for r in self.database.page_status(document_id,version,pages) if r['status']=='succeeded'}
        # “跳过”只针对当前阅读版本的成功页；失败页会重新进入 pending。
        skipped=sorted(existing.intersection(pages));pending=sorted(set(pages)-existing)
        count('pages.read.reused', len(skipped))
        if not pending:return {**self.database.reading(document_id),'skipped_pages':skipped}
        directory=self.storage.root/'readings'/str(document_id)/uuid4().hex
        directory.mkdir(parents=True)
        saved=set(); persist_lock=Lock()
        def persist(record,method):
            # 渲染线程和 OCR 线程都会保存。先提交数据库，再检查暂停。
            with persist_lock:
                record['method']=method
                record['status']='failed' if record.get('error_code') else 'succeeded'
                if record.get('image_path'):
                    record['image_key']=image_key_for(self.storage.root, record['image_path'])
                self.database.save_page(document_id,version,record)
                count('pages.'+method+'.'+record['status'])
                saved.add(record['page_number'])
                if checkpoint:checkpoint('ocr' if method=='ocr' else 'reading',record['page_number'])
        routes={item['page_number']:item.get('route') for item in profile.get('pages') or [] if isinstance(item, dict)}
        scale=self.settings.pdf_render_scale
        stamp=document.get('sha256')
        def image_for(number):
            return stable_render_path(self.storage.root, document_id, number, scale, stamp)
        def image_exists(number):
            path=image_for(number)
            return path.is_file() and not path.is_symlink() and path.stat().st_size>0
        def image_intact(number):
            # 损坏文件不能复用。缩略图即使落在同路径也不能当成识别输入。
            report=inspect_artifact(image_for(number))
            if report['state']=='ok' and report['descriptor'].get('kind')=='thumbnail':
                return False
            return report['state'] in {'ok','unsealed'}
        reuse, render = render_plan(pending, routes, image_exists, force_ocr, intact=image_intact)
        finished=object()
        batch_limit=max(1, self.settings.ocr_page_batch)
        incoming=Queue(); done=Queue()
        enqueued=set(reuse)
        def consume():
            # 只跑 OCR。保存和暂停检查留在调用线程，避免检查点异常留在后台线程里出不来。
            group=[]
            def density(result):
                rows=result.get('lines') or []
                texts=[row.get('text') or '' for row in rows if isinstance(row, dict)]
                return len(texts), sum(len(text) for text in texts)
            def flush():
                if not group:return
                pages=list(group); group.clear()
                detail={}
                number=pages[0].get('page_number') if type(pages[0].get('page_number')) is int else None
                with span('page.ocr', page=number, detail=detail):
                    with closing(ocr_records(self.settings,{
                            'models':str(self.settings.ocr_models_dir.resolve()),'pages':pages,
                            'device':self.settings.ocr_device,'rec_batch':self.settings.ocr_rec_batch,
                            'page_batch':self.settings.ocr_page_batch,
                            'threads':self.settings.ocr_cpu_threads or self.settings.model_cpu_threads,
                            'enable_mkldnn':self.settings.ocr_enable_mkldnn},directory)) as records:
                        for result in records:
                            if 'payload' in result:result.update(normalize_ocr(result.pop('payload')))
                            lines, chars=density(result)
                            detail['lines']=detail.get('lines', 0)+lines
                            detail['chars']=detail.get('chars', 0)+chars
                            count('ocr.lines', lines)
                            count('ocr.chars', chars)
                            if type(result.get('page_number')) is int:
                                done.put(dict(result, _ocr_lines=lines, _ocr_chars=chars))
                            else:
                                done.put(result)
            try:
                while True:
                    with span('ocr.render_wait'):
                        item=incoming.get()
                    if item is None:
                        flush();break
                    group.append(item)
                    if len(group)>=batch_limit:flush()
            except Exception as exc:
                done.put(exc)
            finally:
                # 无论成功或失败都给调用线程一个结束标记，避免它停在空队列上。
                done.put(finished)
        def finish_ocr():
            while True:
                item=done.get()
                if item is finished:return
                if isinstance(item, BaseException):raise item
                lines, chars=item.pop('_ocr_lines', None), item.pop('_ocr_chars', None)
                number=item.get('page_number') if type(item.get('page_number')) is int else None
                if type(lines) is int:
                    with span('ocr.page', page=number, lines=lines, chars=chars if type(chars) is int else 0):
                        pass
                persist(item,'ocr')
        worker=Thread(target=copy_context().run, args=(consume,), name='ocr-pipeline', daemon=True)
        worker.start()
        leases=[]
        try:
            owner=f'{os.getpid()}:{uuid4().hex}'
            still=[]
            for number in render:
                # 同一描述键同时只允许一个生成者。后来者拿到租约后复查，避免重复渲染。
                key=artifact_key(source_sha256=stamp or 'nosha', page=number, kind='recognition',
                    engine='pdfium', dpi=dpi_for_scale(scale), color='rgb', orientation=0, crop=None,
                    transform_version=TRANSFORM_VERSION)
                lease=GenerationLease(self.storage.root, key, owner)
                lease.acquire()
                leases.append(lease)
                if image_exists(number) and image_intact(number):
                    reuse.append(number)
                else:
                    still.append(number)
            render=still
            extract=[number for number in pending if number not in set(reuse)]
            used=png_bytes(self.storage.root/'readings')
            if render and used>=DISK_BYTES:
                for number in render:
                    persist({'page_number':number,'error_code':'disk_budget_exceeded'},'text')
                render=[]
                extract=[number for number in extract if routes.get(number)!='ocr' and not force_ocr]
            if extract:
                pattern='page-{number}-x'+f'{scale:g}-'+(stamp or 'nosha')[:12]+'.png'
                rendered=image_for(extract[0]).parent
                manifest={'mode':'read','source':str(source),'pages':extract,'output_dir':str(rendered),
                    'render_scale':scale,'image_pattern':pattern,'render_pages':render,
                    'max_pixels':SINGLE_IMAGE_PIXELS,'max_inflight_pixels':INFLIGHT_PIXELS,
                    'max_file_bytes':max(1, DISK_BYTES-used)}
                with closing(worker_records(self.settings.pdf_python,'pdf_worker',manifest,directory)) as records:
                    render_set=set(render)
                    for page in records:
                        number=page.get('page_number')
                        if page.get('error_code') in {'image_budget_exceeded','disk_budget_exceeded'}:
                            persist(page,'text')
                        elif page.get('error_code'):
                            persist(page,'text')
                        elif number in render_set and page.get('image_path'):
                            try:
                                self._seal_recognition(page['image_path'], stamp, number, scale)
                            except ArtifactLimitError:
                                persist({'page_number':number,'error_code':'image_budget_exceeded'},'text')
                                continue
                            except ArtifactError:
                                persist({'page_number':number,'error_code':'image_corrupt'},'text')
                                continue
                            if force_ocr or page.get('route')=='ocr':
                                enqueued.add(number); incoming.put(page)
                            else:
                                persist(page,'text')
                        else:
                            persist(page,'text')
            for number in reuse:
                try:
                    self._seal_recognition(image_for(number), stamp, number, scale)
                except (ArtifactError, OSError):
                    persist({'page_number':number,'error_code':'image_corrupt'},'ocr')
                    continue
                count('pdf.render.reused')
                incoming.put({'page_number':number,'route':'ocr','text':'','image_path':str(image_for(number))})
            incoming.put(None)
            finish_ocr()
        except ReadingError:
            if worker.is_alive():
                incoming.put(None)
            try:
                finish_ocr()
            except ReadingError:
                pass
            # 工作进程失败时，已经保存的页保留，只为剩余页补失败记录。
            # ProcessingPaused／ProcessingStopped 不在这里捕获，让调度层正确收束状态。
            for number in set(pending)-saved:
                persist({'page_number':number,'error_code':'worker_failed'},'ocr' if number in enqueued else 'text')
        finally:
            for lease in leases:
                lease.release()
            if worker.is_alive():
                incoming.put(None)
                worker.join()
        return {**self.database.reading(document_id),'skipped_pages':skipped}

    def _seal_recognition(self, image_path, stamp, number, scale):
        path=Path(image_path)
        report=inspect_artifact(path)
        if report['state'] in {'missing','corrupt'}:
            raise ArtifactError('图像缺失或损坏，不能登记。')
        width,height=report['artifact_size']
        file_bytes=path.stat().st_size
        used=png_bytes(self.storage.root/'readings')
        admit_image(width, height, single_limit=SINGLE_IMAGE_PIXELS, inflight=0,
            inflight_limit=INFLIGHT_PIXELS, disk_used=max(0, used-file_bytes),
            incoming_bytes=file_bytes, disk_limit=DISK_BYTES)
        relative=image_key_for(self.storage.root, path)
        descriptor=seal_rendered_png(path, root=self.storage.root, relative_path=relative,
            source_sha256=stamp or 'nosha', page=number, kind='recognition', scale=scale)
        return assert_recognition_input(descriptor)

    def _usable_image(self, path, kind):
        report=inspect_artifact(path)
        if report['state'] not in {'ok','unsealed'}:
            return False
        if report['state']=='unsealed':
            return True
        found=report['descriptor'].get('kind')
        if found=='thumbnail':
            return False
        if kind=='recognition':
            return found=='recognition'
        return found in {'preview','recognition'}

    def _render_single(self, document, document_id, page_number, kind):
        """按同一渲染参数补一张图。不调用 OCR。"""
        source=self.storage.path(document['storage_key'])
        scale=self.settings.pdf_render_scale
        stamp=document.get('sha256')
        target=stable_render_path(self.storage.root, document_id, page_number, scale, stamp)
        used=png_bytes(self.storage.root/'readings')
        if used>=DISK_BYTES:
            raise ReadingError('磁盘字节上限已超过，未降低画质。')
        key=artifact_key(source_sha256=stamp or 'nosha', page=page_number, kind=kind,
            engine='pdfium', dpi=dpi_for_scale(scale), color='rgb', orientation=0, crop=None,
            transform_version=TRANSFORM_VERSION)
        directory=self.storage.root/'readings'/str(document_id)/uuid4().hex
        directory.mkdir(parents=True)
        pattern='page-{number}-x'+f'{scale:g}-'+(stamp or 'nosha')[:12]+'.png'
        owner=f'{os.getpid()}:{uuid4().hex}'
        with GenerationLease(self.storage.root, key, owner):
            if self._usable_image(target, kind):
                if inspect_artifact(target)['state']=='unsealed':
                    if kind=='recognition':
                        self._seal_recognition(target, stamp, page_number, scale)
                    else:
                        seal_rendered_png(target, root=self.storage.root,
                            relative_path=image_key_for(self.storage.root, target),
                            source_sha256=stamp or 'nosha', page=page_number, kind=kind, scale=scale)
                return image_key_for(self.storage.root, target)
            records=list(worker_records(self.settings.pdf_python,'pdf_worker',{
                'mode':'read','source':str(source),'pages':[page_number],'output_dir':str(target.parent),
                'render_scale':scale,'image_pattern':pattern,'render_pages':[page_number],
                'max_pixels':SINGLE_IMAGE_PIXELS,'max_inflight_pixels':INFLIGHT_PIXELS,
                'max_file_bytes':max(1, DISK_BYTES-used)}, directory))
            if not records or records[0].get('error_code') in {'image_budget_exceeded','disk_budget_exceeded'}:
                raise ReadingError('单图或磁盘上限已超过，未降低画质。')
            if records[0].get('error_code') or not records[0].get('image_path'):
                raise ReadingError('页面预览无法生成。')
            path=records[0]['image_path']
            if kind=='recognition':
                self._seal_recognition(path, stamp, page_number, scale)
            else:
                seal_rendered_png(path, root=self.storage.root, relative_path=image_key_for(self.storage.root, path),
                    source_sha256=stamp or 'nosha', page=page_number, kind=kind, scale=scale)
            return image_key_for(self.storage.root, path)

    def ensure_preview(self, project_id, document_id, page_number):
        """浏览时补预览或按原参数补识别图。已有 OCR 文本时不重新识别。"""
        project_id,document_id=UUID(str(project_id)),UUID(str(document_id))
        page_number=int(page_number)
        document=self.database.document(project_id, document_id)
        if document is None:
            raise ReadingError('材料不存在或不属于当前项目。')
        version=self.database.reading_version(document_id)
        if version is None:
            raise ReadingError('该页尚无阅读结果。')
        row=self.database.page_row(document_id, page_number)
        if row is None or row['status']!='succeeded':
            raise ReadingError('该页尚无阅读结果。')
        kind='recognition' if row['method']=='ocr' else 'preview'
        scale=self.settings.pdf_render_scale
        stamp=document.get('sha256')
        current=self.storage.path(row['image_key']) if row.get('image_key') else stable_render_path(
            self.storage.root, document_id, page_number, scale, stamp)
        if row.get('image_key') and self._usable_image(current, kind):
            if inspect_artifact(current)['state']=='unsealed':
                if kind=='recognition':
                    self._seal_recognition(current, stamp, page_number, scale)
                else:
                    seal_rendered_png(current, root=self.storage.root,
                        relative_path=image_key_for(self.storage.root, current),
                        source_sha256=stamp or 'nosha', page=page_number, kind=kind, scale=scale)
            return row['image_key']
        key=self._render_single(document, document_id, page_number, kind)
        saved=self.database.set_page_image(document_id, version['pipeline_version'], page_number, key)
        if not saved:
            raise ReadingError('页面预览无法登记。')
        return saved
