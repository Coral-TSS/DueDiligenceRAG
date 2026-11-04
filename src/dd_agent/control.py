"""Cooperative control, separate from recognition failures."""
# 两种异常只负责把“暂停／服务停止”从深层 worker 回调传回 JobManager。
# 不应在普通识别失败的 except 中吞掉它们，否则暂停会被误记成页面失败。
# 它们表示安全边界上的控制流，不会瞬间冻结正在执行的一次模型计算。


class ProcessingPaused(Exception):
    """The user requested a persistent pause at a safe boundary."""


class ProcessingStopped(Exception):
    """Service shutdown; leave the job recoverable on startup."""


class ProcessingDeferred(Exception):
    """Resource contention: return work to pending, without charging OCR failures."""
