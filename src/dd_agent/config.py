# 全部本机配置的集中入口：Settings() 采用默认值，Settings.from_env() 读取项目 .env。
# 主应用、PDF、OCR、视觉、检索、Harness 使用不同 Python 环境，避免依赖冲突。
# 模型目录配置只指向本地权重，不表示本模块会下载模型或立即加载它们。
from dataclasses import dataclass, field
import os
from pathlib import Path
import math
from uuid import UUID

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    # repr=False 避免打印配置对象时顺带输出数据库连接凭据。
    database_url: str = field(default="", repr=False)
    data_dir: Path = ROOT / ".local" / "data"
    # 空值表示缓存跟随数据目录：data_dir/cache（cache/fingerprints、cache/preflight）。
    cache_dir: Path | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    port: int = 8765
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
    vision_regions_per_page: int = 3
    performance_enabled: bool = True
    db_pool_size: int = 4
    db_pool_timeout: float = 3.
    db_pool_max_waiting: int = 8
    stage_window_pages: int = 10
    stage_backlog_pages: int = 40
    stage_flush_seconds: float = 3.
    # 影子索引必须同时显式开启和指定材料；不允许用 * 启动客户整卷。
    parent_index_mode: str = 'off'
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
    harness_python: Path = ROOT / '.venv-harness' / 'Scripts' / 'python.exe'
    # 下面是项目内 Harness 测试基线，与 D:\Tools\Deepseek Harness 桌面版分开。
    harness_home: Path = ROOT / '.local' / 'harness' / 'home'
    harness_workspace: Path = ROOT / '.local' / 'harness' / 'workspace'
    harness_dsh_bin: Path = ROOT / '.local/harness/cli/node_modules/.bin/dsh.cmd'
    harness_node: Path = ROOT / '.local/harness/runtime/node-v22.19.0-win-x64/node.exe'

    def __post_init__(self):
        if self.parent_index_mode not in ('off','shadow'):
            raise ValueError('跨页索引模式只支持 off 或 shadow。')
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
        if any(not math.isfinite(v) or not .05<=v<=1200 for v in (self.model_idle_seconds,self.model_load_timeout,self.model_queue_timeout)) or type(self.model_max_waiting) is not int or not 1<=self.model_max_waiting<=128 or type(self.model_cpu_threads) is not int or not 1<=self.model_cpu_threads<=16:
            raise ValueError('模型等待、空闲时间或线程预算无效。')
        if type(self.stage_window_pages) is not int or not 1<=self.stage_window_pages<=20 or type(self.stage_backlog_pages) is not int or not self.stage_window_pages<=self.stage_backlog_pages<=200 or not math.isfinite(self.stage_flush_seconds) or not .1<=self.stage_flush_seconds<=30:
            raise ValueError('阶段窗口、积压上限或冲刷时间无效。')
        if not 1 <= self.db_pool_size <= 32 or not math.isfinite(self.db_pool_timeout) or not .05 <= self.db_pool_timeout <= 30 or not 1 <= self.db_pool_max_waiting <= 128:
            raise ValueError('数据库连接池容量、等待队列或超时配置无效。')
        if self.max_upload_bytes <= 0 or not 1 <= self.port <= 65535 or not 1 <= self.vision_timeout_seconds <= 1200 or not math.isfinite(self.pdf_render_scale) or not 1 <= self.pdf_render_scale <= 4 or not 1<=self.vision_regions_per_page<=10:
            raise ValueError("文件大小上限须大于 0，端口须在 1–65535 之间。")

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
            vision_regions_per_page=int(os.getenv('DDA_VISION_REGIONS_PER_PAGE') or '3'),
            performance_enabled=os.getenv('DDA_PERFORMANCE_ENABLED', '1').strip().lower() not in {'0', 'false', 'no'},
            db_pool_size=int(os.getenv('DDA_DB_POOL_SIZE') or '4'),
            db_pool_timeout=float(os.getenv('DDA_DB_POOL_TIMEOUT') or '3'),
            db_pool_max_waiting=int(os.getenv('DDA_DB_POOL_MAX_WAITING') or '8'),
            stage_window_pages=int(os.getenv('DDA_STAGE_WINDOW_PAGES') or '10'),
            stage_backlog_pages=int(os.getenv('DDA_STAGE_BACKLOG_PAGES') or '40'),
            stage_flush_seconds=float(os.getenv('DDA_STAGE_FLUSH_SECONDS') or '3'),
            parent_index_mode=os.getenv('DDA_PARENT_INDEX_MODE','off').strip().lower(),
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
            harness_python=Path(os.getenv('DDA_HARNESS_PYTHON') or ROOT / '.venv-harness/Scripts/python.exe'),
            harness_home=Path(os.getenv('DDA_HARNESS_HOME') or ROOT / '.local/harness/home'),
            harness_workspace=Path(os.getenv('DDA_HARNESS_WORKSPACE') or ROOT / '.local/harness/workspace'),
            harness_dsh_bin=Path(os.getenv('DDA_HARNESS_DSH_BIN') or ROOT / '.local/harness/cli/node_modules/.bin/dsh.cmd'),
            harness_node=Path(os.getenv('DDA_HARNESS_NODE') or ROOT / '.local/harness/runtime/node-v22.19.0-win-x64/node.exe'),
        )
