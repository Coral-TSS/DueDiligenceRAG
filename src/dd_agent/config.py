# 全部本机配置的集中入口：Settings() 采用默认值，Settings.from_env() 读取项目 .env。
# 主应用、PDF、OCR、视觉、检索使用不同 Python 环境，避免依赖冲突。
# 模型目录配置只指向本地权重，不表示本模块会下载模型或立即加载它们。
from dataclasses import dataclass, field
import os
from pathlib import Path
import math
from uuid import UUID

from dotenv import load_dotenv

from .devices import DTYPES, PADDLE_DEVICES, TORCH_DEVICES


ROOT = Path(__file__).resolve().parents[2]


def vision_token_ceiling(settings):
    """常驻视觉模型允许的最长生成。三类区域可以各自更短，但不能超过加载时的上限。"""
    return max(settings.vision_max_new_tokens, settings.vision_tokens_low, settings.vision_tokens_value)


@dataclass(frozen=True)
class Settings:
    # repr=False 避免打印配置对象时顺带输出数据库连接凭据。
    database_url: str = field(default="", repr=False)
    data_dir: Path = ROOT / ".local" / "data"
    # 空值表示缓存跟随数据目录：data_dir/cache（cache/fingerprints、cache/preflight）。
    cache_dir: Path | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    port: int = 8765
    # MCP 与网页分开监听。默认只绑本机；DDA_MCP_HOST=0.0.0.0 才对局域网开放。
    mcp_host: str = '127.0.0.1'
    mcp_port: int = 8766
    mcp_api_key: str = field(default='', repr=False)
    pdf_python: Path = ROOT / '.venv-docling' / 'Scripts' / 'python.exe'
    ocr_python: Path = ROOT / '.venv-ocr' / 'Scripts' / 'python.exe'
    ocr_models_dir: Path = ROOT / 'models' / 'ocr' / 'official_models'
    retrieval_python: Path = ROOT / '.venv-retrieval' / 'Scripts' / 'python.exe'
    embedding_dir: Path = Path('D:/AgentData/dd-agent/models/Qwen3-Embedding-0.6B')
    reranker_dir: Path = Path('D:/AgentData/dd-agent/models/Qwen3-Reranker-0.6B')
    vision_python: Path = ROOT / '.venv-vision' / 'Scripts' / 'python.exe'
    vision_models_dir: Path = ROOT / 'models' / 'vision' / 'PaddleOCR-VL-1.6'
    vision_timeout_seconds: int = 300
    pdf_render_scale: float = 2.0
    # 扫描卷默认只增强最需要的 1 个区域；需要更密的复核时再调高。
    vision_regions_per_page: int = 1
    vision_max_new_tokens: int = 128
    # 低置信行、金额／日期行可以单独缩短生成长度；默认金额行更短，关键词行沿用上面的 128。
    vision_tokens_low: int = 64
    vision_tokens_value: int = 96
    vision_batch: int = 2
    vision_low_score: float = 0.85
    # 1 表示数字／日期／金额行参与选区。分数上限 1 表示不限置信度，和当前规则一致。
    vision_value_lines: bool = True
    vision_value_max_score: float = 1.0
    vision_keyword_max_score: float = 1.0
    # 单次处理里视觉超时达到该次数后，其余区域直接标未知，不再各等满超时。
    vision_timeout_budget: int = 3
    ocr_device: str = 'auto'
    ocr_enable_mkldnn: bool = False
    # 0 表示沿用 model_cpu_threads。oneDNN 默认关闭，见 devices.describe_paddle。
    ocr_cpu_threads: int = 0
    ocr_rec_batch: int = 16
    ocr_page_batch: int = 4
    embed_token_budget: int = 2048
    embed_batch_limit: int = 32
    rerank_token_budget: int = 4096
    vision_device: str = 'auto'
    embedding_device: str = 'auto'
    reranker_device: str = 'auto'
    vision_dtype: str = 'auto'
    embedding_dtype: str = 'auto'
    reranker_dtype: str = 'auto'
    performance_enabled: bool = True
    db_pool_size: int = 4
    db_pool_timeout: float = 3.
    db_pool_max_waiting: int = 8
    stage_window_pages: int = 10
    stage_backlog_pages: int = 40
    stage_flush_seconds: float = 3.
    # 影子索引必须同时显式开启和指定材料；不允许用 * 启动客户整卷。
    # on：每份材料都建父子索引，检索优先用子片段召回、父片段做上下文。
    # shadow 仍要指定最多 20 份材料；off 只保留页级索引。
    parent_index_mode: str = 'on'
    parent_index_documents: tuple[str, ...] = ()
    # 查询与后台建库独立；默认自动发现完整父子材料，可选名单缩小范围。
    search_index_mode: str = 'mixed'
    search_parent_documents: tuple[str, ...] = ()
    model_runtime_enabled: bool = True
    model_idle_seconds: float = 30.
    model_load_timeout: float = 300.
    model_queue_timeout: float = 3.
    model_max_waiting: int = 8
    model_cpu_threads: int = 4
    search_timeout_seconds: float = 120.
    # hybrid 同时走向量和关键词；vector / keyword 只留一路，便于对照。融合发生在重排之前。
    retrieval_mode: str = 'hybrid'
    retrieval_fusion: str = 'rrf'
    retrieval_vector_limit: int = 12
    retrieval_keyword_limit: int = 12
    retrieval_candidate_limit: int = 12
    retrieval_rrf_k: int = 60
    retrieval_vector_weight: float = 1.
    retrieval_keyword_weight: float = 1.

    def __post_init__(self):
        if self.parent_index_mode not in ('on','off','shadow'):
            raise ValueError('跨页索引模式只支持 on、off 或 shadow。')
        if not isinstance(self.parent_index_documents,tuple) or len(self.parent_index_documents)>20:
            raise ValueError('跨页试运行材料名单须为最多 20 个 UUID。')
        try:documents=tuple(str(UUID(str(value))) for value in self.parent_index_documents)
        except (ValueError,TypeError,AttributeError):
            raise ValueError('跨页试运行材料名单必须使用有效 UUID。') from None
        if len(set(documents))!=len(documents) or (self.parent_index_mode=='shadow' and not documents):
            raise ValueError('跨页试运行须指定不重复的材料 UUID，不能使用空名单。')
        object.__setattr__(self,'parent_index_documents',documents)
        if self.search_index_mode not in ('page','mixed'):
            raise ValueError('查询索引模式只支持 page 或 mixed。')
        if not isinstance(self.search_parent_documents,tuple) or len(self.search_parent_documents)>20:
            raise ValueError('查询材料名单须为最多 20 个 UUID。')
        try:selected=tuple(str(UUID(str(value))) for value in self.search_parent_documents)
        except (ValueError,TypeError,AttributeError):
            raise ValueError('查询材料名单必须使用有效 UUID。') from None
        if len(set(selected))!=len(selected):
            raise ValueError('查询材料 UUID 不能重复。')
        object.__setattr__(self,'search_parent_documents',selected)
        if not math.isfinite(self.search_timeout_seconds) or not .05<=self.search_timeout_seconds<=1200:raise ValueError('查询超时预算无效。')
        if self.retrieval_mode not in ('hybrid', 'vector', 'keyword'):
            raise ValueError('检索模式只支持 hybrid、vector 或 keyword。')
        if self.retrieval_fusion not in ('rrf', 'weighted'):
            raise ValueError('召回融合只支持 rrf 或 weighted。')
        if any(type(v) is not int or not 1 <= v <= 50 for v in (self.retrieval_vector_limit, self.retrieval_keyword_limit)):
            raise ValueError('各路召回条数须为 1–50。')
        if type(self.retrieval_candidate_limit) is not int or not 1 <= self.retrieval_candidate_limit <= 12:
            raise ValueError('送入重排的候选条数须为 1–12。')
        if type(self.retrieval_rrf_k) is not int or not 1 <= self.retrieval_rrf_k <= 1000:
            raise ValueError('RRF 平滑常数须为 1–1000。')
        weights = (self.retrieval_vector_weight, self.retrieval_keyword_weight)
        if any(isinstance(v, bool) or type(v) not in (int, float) or not math.isfinite(v) or not 0 < v <= 100 for v in weights):
            raise ValueError('召回权重须为大于 0 且不超过 100 的数。')
        if any(not math.isfinite(v) or not .05<=v<=1200 for v in (self.model_idle_seconds,self.model_load_timeout,self.model_queue_timeout)) or type(self.model_max_waiting) is not int or not 1<=self.model_max_waiting<=128 or type(self.model_cpu_threads) is not int or not 1<=self.model_cpu_threads<=16:
            raise ValueError('模型等待、空闲时间或线程预算无效。')
        if type(self.stage_window_pages) is not int or not 1<=self.stage_window_pages<=20 or type(self.stage_backlog_pages) is not int or not self.stage_window_pages<=self.stage_backlog_pages<=200 or not math.isfinite(self.stage_flush_seconds) or not .1<=self.stage_flush_seconds<=30:
            raise ValueError('阶段窗口、积压上限或冲刷时间无效。')
        if not 1 <= self.db_pool_size <= 32 or not math.isfinite(self.db_pool_timeout) or not .05 <= self.db_pool_timeout <= 30 or not 1 <= self.db_pool_max_waiting <= 128:
            raise ValueError('数据库连接池容量、等待队列或超时配置无效。')
        if self.max_upload_bytes <= 0 or not 1 <= self.port <= 65535 or not 1 <= self.vision_timeout_seconds <= 1200 or not math.isfinite(self.pdf_render_scale) or not 1 <= self.pdf_render_scale <= 4 or not 1<=self.vision_regions_per_page<=10:
            raise ValueError("文件大小上限须大于 0，端口须在 1–65535 之间。")
        if type(self.mcp_port) is not int or not 1 <= self.mcp_port <= 65535:
            raise ValueError('MCP 端口须在 1–65535 之间。')
        if not isinstance(self.mcp_api_key, str):
            raise ValueError('MCP API 密钥无效。')
        host = self.mcp_host.strip() if isinstance(self.mcp_host, str) else ''
        if not host or len(host) > 255 or any(ch.isspace() or ch in '/\\' for ch in host):
            raise ValueError('MCP 监听地址无效。')
        object.__setattr__(self, 'mcp_host', host)
        object.__setattr__(self, 'mcp_api_key', self.mcp_api_key.strip())
        if self.ocr_device not in PADDLE_DEVICES or any(v not in TORCH_DEVICES for v in (self.vision_device, self.embedding_device, self.reranker_device)):
            raise ValueError('模型设备配置无效。OCR 用 cpu 或 gpu:0，PyTorch 模型用 cpu 或 cuda，也可用 auto。')
        if any(v not in DTYPES for v in (self.vision_dtype, self.embedding_dtype, self.reranker_dtype)):
            raise ValueError('模型精度只支持 auto、float32、float16 或 bfloat16。')
        if any(type(v) is not int or not 16 <= v <= 512 for v in (self.vision_max_new_tokens, self.vision_tokens_low, self.vision_tokens_value)):
            raise ValueError('视觉生成长度须为 16–512。')
        if type(self.vision_batch) is not int or not 1 <= self.vision_batch <= 8:
            raise ValueError('视觉批大小须为 1–8。')
        if type(self.vision_value_lines) is not bool:
            raise ValueError('金额行开关无效。')
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (self.vision_value_max_score, self.vision_keyword_max_score)):
            raise ValueError('视觉分数上限须在 0–1 之间。')
        if type(self.ocr_rec_batch) is not int or not 1 <= self.ocr_rec_batch <= 64 or type(self.ocr_page_batch) is not int or not 1 <= self.ocr_page_batch <= 16:
            raise ValueError('OCR 批大小无效。')
        if type(self.embed_token_budget) is not int or not 128 <= self.embed_token_budget <= 8192 or type(self.embed_batch_limit) is not int or not 1 <= self.embed_batch_limit <= 64:
            raise ValueError('向量批预算无效。')
        if type(self.rerank_token_budget) is not int or not 128 <= self.rerank_token_budget <= 8192:
            raise ValueError('重排序批预算无效。')
        if type(self.vision_timeout_budget) is not int or not 0 <= self.vision_timeout_budget <= 50:
            raise ValueError('视觉超时预算须为 0–50。')
        if not math.isfinite(self.vision_low_score) or not 0 <= self.vision_low_score <= 1:
            raise ValueError('视觉低置信阈值须在 0–1 之间。')
        if type(self.ocr_cpu_threads) is not int or not 0 <= self.ocr_cpu_threads <= 16 or type(self.ocr_enable_mkldnn) is not bool:
            raise ValueError('OCR 线程或 oneDNN 开关无效。')

    @classmethod
    def from_env(cls):
        # 空的可选模型路径会回退默认值；业务数据目录的相对路径以项目根目录为基准。
        load_dotenv(ROOT / ".env")
        directory = os.getenv("DDA_DATA_DIR", "").strip()
        path = Path(directory).expanduser() if directory else ROOT / ".local" / "data"
        if not path.is_absolute():
            path = ROOT / path
        cache = os.getenv("DDA_CACHE_DIR", "").strip()
        cache_path = Path(cache).expanduser() if cache else None
        if cache_path is not None and not cache_path.is_absolute():
            cache_path = ROOT / cache_path
        return cls(
            database_url=os.getenv("DDA_DATABASE_URL", "").strip(),
            data_dir=path.resolve(),
            cache_dir=cache_path.resolve() if cache_path else None,
            max_upload_bytes=int(os.getenv("DDA_MAX_UPLOAD_MB", "100")) * 1024 * 1024,
            port=int(os.getenv("DDA_PORT", "8765")),
            mcp_host=os.getenv('DDA_MCP_HOST', '127.0.0.1').strip() or '127.0.0.1',
            mcp_port=int(os.getenv('DDA_MCP_PORT') or '8766'),
            mcp_api_key=os.getenv('DDA_MCP_API_KEY', ''),
            pdf_python=Path(os.getenv('DDA_PDF_PYTHON') or ROOT / '.venv-docling' / 'Scripts' / 'python.exe'),
            ocr_python=Path(os.getenv('DDA_OCR_PYTHON') or ROOT / '.venv-ocr' / 'Scripts' / 'python.exe'),
            ocr_models_dir=Path(os.getenv('DDA_OCR_MODELS_DIR') or ROOT / 'models' / 'ocr' / 'official_models'),
            retrieval_python=Path(os.getenv('DDA_RETRIEVAL_PYTHON') or ROOT / '.venv-retrieval' / 'Scripts' / 'python.exe'),
            embedding_dir=Path(os.getenv('DDA_EMBEDDING_DIR') or 'D:/AgentData/dd-agent/models/Qwen3-Embedding-0.6B'),
            reranker_dir=Path(os.getenv('DDA_RERANKER_DIR') or 'D:/AgentData/dd-agent/models/Qwen3-Reranker-0.6B'),
            vision_python=Path(os.getenv('DDA_VISION_PYTHON') or ROOT / '.venv-vision' / 'Scripts' / 'python.exe'),
            vision_models_dir=Path(os.getenv('DDA_VISION_MODELS_DIR') or ROOT / 'models' / 'vision' / 'PaddleOCR-VL-1.6'),
            vision_timeout_seconds=int(os.getenv('DDA_VISION_TIMEOUT_SECONDS') or '300'),
            pdf_render_scale=float(os.getenv('DDA_PDF_RENDER_SCALE') or '2'),
            vision_regions_per_page=int(os.getenv('DDA_VISION_REGIONS_PER_PAGE') or '1'),
            vision_max_new_tokens=int(os.getenv('DDA_VISION_MAX_NEW_TOKENS') or '128'),
            vision_tokens_low=int(os.getenv('DDA_VISION_TOKENS_LOW') or '64'),
            vision_tokens_value=int(os.getenv('DDA_VISION_TOKENS_VALUE') or '96'),
            vision_batch=int(os.getenv('DDA_VISION_BATCH') or '2'),
            vision_low_score=float(os.getenv('DDA_VISION_LOW_SCORE') or '0.85'),
            vision_value_lines=os.getenv('DDA_VISION_VALUE_LINES', '1').strip().lower() not in {'0', 'false', 'no'},
            vision_value_max_score=float(os.getenv('DDA_VISION_VALUE_MAX_SCORE') or '1'),
            vision_keyword_max_score=float(os.getenv('DDA_VISION_KEYWORD_MAX_SCORE') or '1'),
            vision_timeout_budget=int(os.getenv('DDA_VISION_TIMEOUT_BUDGET') or '3'),
            ocr_device=os.getenv('DDA_OCR_DEVICE', 'auto').strip().lower(),
            ocr_enable_mkldnn=os.getenv('DDA_OCR_ENABLE_MKLDNN', '0').strip().lower() in {'1', 'true', 'yes'},
            ocr_cpu_threads=int(os.getenv('DDA_OCR_CPU_THREADS') or '0'),
            ocr_rec_batch=int(os.getenv('DDA_OCR_REC_BATCH') or '16'),
            ocr_page_batch=int(os.getenv('DDA_OCR_PAGE_BATCH') or '4'),
            embed_token_budget=int(os.getenv('DDA_EMBED_TOKEN_BUDGET') or '2048'),
            embed_batch_limit=int(os.getenv('DDA_EMBED_BATCH_LIMIT') or '32'),
            rerank_token_budget=int(os.getenv('DDA_RERANK_TOKEN_BUDGET') or '4096'),
            vision_device=os.getenv('DDA_VISION_DEVICE', 'auto').strip().lower(),
            embedding_device=os.getenv('DDA_EMBEDDING_DEVICE', 'auto').strip().lower(),
            reranker_device=os.getenv('DDA_RERANKER_DEVICE', 'auto').strip().lower(),
            vision_dtype=os.getenv('DDA_VISION_DTYPE', 'auto').strip().lower(),
            embedding_dtype=os.getenv('DDA_EMBEDDING_DTYPE', 'auto').strip().lower(),
            reranker_dtype=os.getenv('DDA_RERANKER_DTYPE', 'auto').strip().lower(),
            performance_enabled=os.getenv('DDA_PERFORMANCE_ENABLED', '1').strip().lower() not in {'0', 'false', 'no'},
            db_pool_size=int(os.getenv('DDA_DB_POOL_SIZE') or '4'),
            db_pool_timeout=float(os.getenv('DDA_DB_POOL_TIMEOUT') or '3'),
            db_pool_max_waiting=int(os.getenv('DDA_DB_POOL_MAX_WAITING') or '8'),
            stage_window_pages=int(os.getenv('DDA_STAGE_WINDOW_PAGES') or '10'),
            stage_backlog_pages=int(os.getenv('DDA_STAGE_BACKLOG_PAGES') or '40'),
            stage_flush_seconds=float(os.getenv('DDA_STAGE_FLUSH_SECONDS') or '3'),
            parent_index_mode=os.getenv('DDA_PARENT_INDEX_MODE','on').strip().lower(),
            parent_index_documents=tuple(value.strip() for value in os.getenv('DDA_PARENT_INDEX_DOCUMENT_IDS','').split(',') if value.strip()),
            search_index_mode=os.getenv('DDA_SEARCH_INDEX_MODE','mixed').strip().lower(),
            search_parent_documents=tuple(value.strip() for value in os.getenv('DDA_SEARCH_PARENT_DOCUMENT_IDS','').split(',') if value.strip()),
            model_runtime_enabled=os.getenv('DDA_MODEL_RUNTIME_ENABLED','1').strip().lower() in {'1','true','yes'},
            model_idle_seconds=float(os.getenv('DDA_MODEL_IDLE_SECONDS') or '30'),
            model_load_timeout=float(os.getenv('DDA_MODEL_LOAD_TIMEOUT') or '300'),
            model_queue_timeout=float(os.getenv('DDA_MODEL_QUEUE_TIMEOUT') or '3'),
            model_max_waiting=int(os.getenv('DDA_MODEL_MAX_WAITING') or '8'),
            model_cpu_threads=int(os.getenv('DDA_MODEL_CPU_THREADS') or '4'),
            search_timeout_seconds=float(os.getenv('DDA_SEARCH_TIMEOUT_SECONDS') or '120'),
            retrieval_mode=os.getenv('DDA_RETRIEVAL_MODE', 'hybrid').strip().lower(),
            retrieval_fusion=os.getenv('DDA_RETRIEVAL_FUSION', 'rrf').strip().lower(),
            retrieval_vector_limit=int(os.getenv('DDA_RETRIEVAL_VECTOR_LIMIT') or '12'),
            retrieval_keyword_limit=int(os.getenv('DDA_RETRIEVAL_KEYWORD_LIMIT') or '12'),
            retrieval_candidate_limit=int(os.getenv('DDA_RETRIEVAL_CANDIDATE_LIMIT') or '12'),
            retrieval_rrf_k=int(os.getenv('DDA_RETRIEVAL_RRF_K') or '60'),
            retrieval_vector_weight=float(os.getenv('DDA_RETRIEVAL_VECTOR_WEIGHT') or '1'),
            retrieval_keyword_weight=float(os.getenv('DDA_RETRIEVAL_KEYWORD_WEIGHT') or '1'),
        )
