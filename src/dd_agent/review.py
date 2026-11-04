"""Historical text review lookup and local visual candidate persistence."""
# 历史命名说明：review 不表示当前产品需要人工复核。
# AutomaticProcessor 复用这里的 LocalVision／recognize() 保存视觉候选，人工版本只读留档。
# 实际的“采用哪段文字／何时标未知”在 automatic.py，不由本文件替用户确认事实。
import math
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .cache import cache_directory, file_entries, resolve_fingerprint
from .config import Settings
from .db import Database
from .reading import worker_records, ReadingError
from .retrieval import model_slot, RetrievalError
from .storage import Storage
from .performance import timed, observed, count
from .model_admission import current_control
from .control import ProcessingDeferred


class ReviewError(Exception):
    pass


class VisionTimeout(ReviewError):
    """区域推理超过配置时限。自动增强据此计入预算，并避免同一页无限重试。"""


class ReviewConflict(ReviewError):
    pass


class ReviewNotFound(ReviewError):
    pass


def valid_bounds(bounds):
    # 归一化 [左,上,右,下] 坐标；图片像素尺寸由 worker 读取，便于跨渲染尺寸定位。
    if len(bounds) != 4 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in bounds):
        raise ValueError('区域坐标须为 0–1 之间的四个有限数值。')
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError('区域的右下角须位于左上角之后。')
    return list(bounds)


class LocalVision:
    # 轻量调用器：默认经共享运行时复用视觉实例，也可回退独立 worker。
    def __init__(self, settings):
        self.settings = settings

    @property
    def managed(self):return self.settings.model_runtime_enabled

    @timed('fingerprint.vision')
    def profile(self, verify=False):
        # 缓存策略绑定本地权重／配置与超时；改变配置后不应无条件复用旧候选。
        # 命中缓存时不读取权重内容，只比对文件清单（RG-05b／K01）。
        def compute():
            # 前缀中的 cpu-fp32 是历史指纹，不表示本次推理设备。
            # 设备、精度、注意力和 KV cache 的识别文本已核对一致，不能写进指纹，否则换卡会重跑视觉。
            # 生成长度改的是截断边界，由自动增强版本单独绑定，不放进这份权重指纹。
            digest=hashlib.sha256(b'vision-region-v1-cpu-fp32')
            for file in sorted(self.settings.vision_models_dir.glob('*')):
                if file.suffix in {'.safetensors','.json'}:
                    digest.update(file.name.encode())
                    with file.open('rb') as stream:
                        for block in iter(lambda:stream.read(4*1024*1024),b''):
                            digest.update(block); count('fingerprint.vision.bytes', len(block))
            digest.update(str(self.settings.vision_timeout_seconds).encode())
            return digest.hexdigest()
        files = sorted(p for p in self.settings.vision_models_dir.glob('*') if p.suffix in {'.safetensors','.json'})
        # 算法本体所在文件参与清单：改动实现即失效，不靠权重目录时间判断。
        entries = file_entries(files + [Path(__file__)])
        return resolve_fingerprint('vision_weights', 'python-stdlib', 'vision-weights-digest-v1',
            dict(models=str(self.settings.vision_models_dir.resolve()), timeout=self.settings.vision_timeout_seconds),
            entries, compute, verify=verify, directory=cache_directory(self.settings))['value']

    @timed('vision.request')
    def recognize(self, *, image_path, bounds, directory, page_number=None, region=None):
        from .model_runtime import runtime_records, ModelRuntimeError
        # 新 token 上限可配置，默认 128；是否截断由 worker 明确返回，不能静默当全文。
        manifest = dict(image=str(image_path), bounds=bounds, models=str(self.settings.vision_models_dir.resolve()),
                        max_new_tokens=self.settings.vision_max_new_tokens, device=self.settings.vision_device,
                        dtype=self.settings.vision_dtype, threads=self.settings.model_cpu_threads)
        if type(page_number) is int: manifest['page_number'] = page_number
        if type(region) is int: manifest['region'] = region
        try:
            records = list(runtime_records(self.settings,'vision','recognize',manifest,timeout=self.settings.vision_timeout_seconds)) if self.managed else list(worker_records(self.settings.vision_python, 'vision_worker', manifest, directory,timeout_seconds=self.settings.vision_timeout_seconds))
        except ModelRuntimeError as exc:
            if str(exc)=='model_busy' and current_control().defer_busy:raise ProcessingDeferred() from None
            if str(exc)=='worker_timeout':raise VisionTimeout('视觉区域超过时限。') from None
            raise ReviewError('本地视觉模型未完成，请稍后重试。') from None
        except ReadingError:
            raise ReviewError('本地视觉模型未完成，请检查环境或缩小区域后重试。') from None
        if len(records) != 1 or not isinstance(records[0].get('text'), str):
            raise ReviewError('视觉模型返回内容不完整。')
        return records[0]


class ReviewService:
    def __init__(self, settings=None, vision=None, database=None):
        self.settings = settings or Settings.from_env()
        self.database = database if database is not None else Database(self.settings.database_url)
        self.vision = vision or LocalVision(self.settings)

    def state(self, project_id, document_id, number):
        # adoptions 是旧人工原型的历史，candidates 是独立视觉输出；二者不能混成当前事实。
        project_id, document_id = UUID(str(project_id)), UUID(str(document_id))
        if self.database.document(project_id, document_id) is None:
            raise ReviewNotFound('材料不存在或不属于当前项目。')
        with self.database.connect() as conn:
            page = conn.execute('SELECT * FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=%s', (document_id, number)).fetchone()
            if page is None:
                raise ReviewNotFound('该页尚未阅读，请先读取该页。')
            adoptions = conn.execute('SELECT * FROM dd_agent.page_adoptions WHERE document_id=%s AND page_number=%s ORDER BY created_at DESC,id DESC', (document_id, number)).fetchall()
            candidates = conn.execute('SELECT * FROM dd_agent.vision_candidates WHERE document_id=%s AND page_number=%s ORDER BY created_at DESC,id DESC', (document_id, number)).fetchall()
        adoption = adoptions[0] if adoptions else None
        return dict(raw_revision=page['raw_revision'], adoption_id=adoption['id'] if adoption else None,
                    adoption=adoption, adoptions=adoptions, candidates=candidates,
                    conflict=bool(adoption and adoption['base_revision'] != page['raw_revision']),
                    status=page['status'])


    @observed('vision')
    def recognize(self, project_id, document_id, number, bounds, raw_revision, profile_version='', region_index=None):
        # 调用前确认基础修订未变化，调用后再查一次，避免将旧图片输出绑定到新 OCR。
        bounds = valid_bounds(bounds)
        state = self.state(project_id, document_id, number)
        if state['raw_revision'] != raw_revision:
            raise ReviewConflict('页面已更新，请刷新后选择识别区域。')
        raw = next(p for p in self.database.pages(document_id) if p['page_number'] == number)
        if raw['status'] != 'succeeded':
            raise ReviewError('该页基础阅读未成功，不能复核。')
        identifier = uuid4()
        directory = self.settings.data_dir / 'vision' / str(identifier)
        directory.mkdir(parents=True)
        with self.database.publication('vision',number,document_id=document_id,final=False) as conn:
            conn.execute('''INSERT INTO dd_agent.vision_candidates(id,document_id,page_number,base_revision,bounds,status,profile_version)
                VALUES (%s,%s,%s,%s,%s,'running',%s)''', (identifier, document_id, number, raw_revision, Jsonb(bounds),profile_version))
        result, status, error = {}, 'failed', 'vision_failed'
        deferred=None
        timed_out=None
        try:
            # 与向量化／检索共用资源锁，控制本机内存竞争；拿不到时保留失败候选记录。
            with model_slot(self.settings.data_dir,managed=getattr(self.vision,'managed',False)):
                image = Storage(self.settings.data_dir, self.settings.max_upload_bytes).path(raw['image_key']) if raw['image_key'] else directory / 'missing.png'
                result = self.vision.recognize(image_path=image, bounds=bounds, directory=directory, page_number=number, region=region_index)
                if not isinstance(result.get('text'), str) or len(result['text']) > 100000:
                    raise ReviewError('视觉结果不完整。')
                status = 'succeeded' if self.state(project_id, document_id, number)['raw_revision'] == raw_revision else 'stale'
                error = ''
        except ProcessingDeferred as exc:
            deferred=exc;error='model_busy'
        except VisionTimeout as exc:
            timed_out=exc;error='vision_timeout'
        except (ReviewError, RetrievalError, OSError):
            pass
        with self.database.publication('vision',number,document_id=document_id,final=False) as conn:
            candidate=conn.execute('''UPDATE dd_agent.vision_candidates SET status=%s,result=%s,error_code=%s,finished_at=clock_timestamp()
                WHERE id=%s RETURNING *''', (status, Jsonb(result), error, identifier)).fetchone()
        if deferred is not None:raise deferred
        if timed_out is not None:raise timed_out
        return candidate
