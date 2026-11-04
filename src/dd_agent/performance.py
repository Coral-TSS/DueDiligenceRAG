"""本地性能记录。只记阶段、数量与资源，不接收业务正文或 SQL 参数。"""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import sys
from threading import Event, Lock, Thread
from time import perf_counter
from uuid import uuid4


_active = ContextVar('dda_performance', default=None)
_NAME = re.compile(r'[a-z][a-z0-9_.]{0,79}\Z')


def _status(error):
    return {'ProcessingPaused': 'paused', 'ProcessingStopped': 'stopped',
            'GeneratorExit': 'closed'}.get(type(error).__name__, 'error') if error else 'ok'


def _memory(pid):
    """返回当前／峰值 RSS；Windows 使用 OS 计数，不需要安装监控依赖。"""
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                    'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x410, False, pid)
            if not handle: return None
            try:
                value = Counters(); value.cb = ctypes.sizeof(value)
                psapi = ctypes.WinDLL('psapi', use_last_error=True)
                psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(value), value.cb):
                    return value.WorkingSetSize / 1024**2, value.PeakWorkingSetSize / 1024**2
            finally:
                kernel.CloseHandle(handle)
        elif Path(f'/proc/{pid}/status').exists():
            values = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
            return float(values['VmRSS'].split()[0])/1024, float(values['VmHWM'].split()[0])/1024
    except (OSError, ValueError, KeyError):
        pass
    return None


class Trace:
    def __init__(self, directory, operation, max_events, sample_memory):
        self.directory, self.operation = Path(directory), operation
        self.id = uuid4().hex
        self.start = perf_counter()
        self.events, self.stages, self.counters = [], {}, defaultdict(float)
        self.milestones = {}
        self.max_events, self.dropped = max_events, 0
        self.lock, self.stop = Lock(), Event()
        self.pids, self.peaks = {os.getpid()}, {}
        self.sampled_peak, self.samples = 0., 0
        self.thread = None
        if sample_memory:
            self.thread = Thread(target=self._sample, daemon=True, name='dda-memory-sampler')
            self.thread.start()

    def _sample(self):
        # 这是同时点 RSS 的采样峰值，不是把各进程历史峰值相加。
        while True:
            with self.lock: pids = tuple(self.pids)
            readings = [(pid, _memory(pid)) for pid in pids]
            with self.lock:
                present = [value[0] for _, value in readings if value is not None]
                if present:
                    self.sampled_peak = max(self.sampled_peak, sum(present)); self.samples += 1
                for pid, value in readings:
                    if value: self.peaks[str(pid)] = max(self.peaks.get(str(pid), 0), value[1])
            if self.stop.wait(.1): return

    def event(self, value):
        name = value.get('stage', '')
        seconds = value.get('seconds')
        status = value.get('status')
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            return
        if status not in ('ok', 'error', 'paused', 'stopped', 'closed'): return
        # 显式投影；即使工作进程误带 text／query，也不落进性能记录。
        safe = dict(stage=name, seconds=seconds, status=status)
        pid = value.get('pid')
        peak = value.get('peak_rss_mb')
        with self.lock:
            if isinstance(pid, int) and isinstance(peak, (int, float)) and math.isfinite(peak) and peak >= 0:
                self.peaks[str(pid)] = max(self.peaks.get(str(pid), 0), peak)
            totals = self.stages.setdefault(name, dict(calls=0, seconds=0., ok=0, error=0, paused=0, stopped=0, closed=0))
            totals['calls'] += 1; totals['seconds'] += seconds; totals[status] += 1
            if len(self.events) < self.max_events: self.events.append(safe)
            else: self.dropped += 1

    def increment(self, name, value):
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0: return
        with self.lock:
            if name in self.counters or len(self.counters) < 128: self.counters[name] += value

    def finish(self, status):
        self.stop.set()
        if self.thread: self.thread.join(timeout=1)
        own = _memory(os.getpid())
        if own: self.peaks[str(os.getpid())] = own[1]
        value = dict(schema_version=1, trace_id=self.id, operation=self.operation, status=status,
                     wall_seconds=perf_counter()-self.start, counters=dict(self.counters), stages=self.stages,
                     events=self.events, dropped_events=self.dropped, milestones=self.milestones,
                     memory=dict(scope='main_and_registered_workers', sample_interval_seconds=.1,
                                 samples=self.samples, sampled_peak_rss_mb=self.sampled_peak if self.samples else None,
                                 process_peak_rss_mb=self.peaks))
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / (self.id+'.json')
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            temporary.replace(target)
        except OSError:
            # 性能文件不可写时，绝不能覆盖业务结果或原本的控制异常。
            pass


@contextmanager
def capture(directory, operation, *, max_events=1024, sample_memory=False):
    if _active.get() is not None:
        yield _active.get(); return
    if not _NAME.fullmatch(operation): raise ValueError('Invalid performance operation')
    trace = Trace(directory, operation, max_events, sample_memory)
    token = _active.set(trace); status = 'ok'
    try:
        yield trace
    except BaseException as exc:
        status = _status(exc); raise
    finally:
        _active.reset(token); trace.finish(status)


def active():
    return _active.get() is not None


def mark(name):
    """记录首次发生的时刻；用于首次发布等不能由阶段耗时相加得到的指标。"""
    trace = _active.get()
    if trace and isinstance(name, str) and _NAME.fullmatch(name):
        with trace.lock:
            if name not in trace.milestones and len(trace.milestones) < 64:
                trace.milestones[name] = perf_counter()-trace.start


def _emit(value):
    # 模型库可能 redirect_stdout；协议始终走原始 stdout，不混入其诊断日志。
    try: print(json.dumps({'_dda_metric': value}), file=sys.__stdout__, flush=True)
    except (OSError, ValueError): pass


def count(name, value=1):
    trace = _active.get()
    if trace: trace.increment(name, value)
    elif os.environ.get('DDA_WORKER_METRICS') == '1': _emit(dict(counter=name, value=value))


def accept_worker(value):
    trace = _active.get()
    if trace and isinstance(value, dict):
        if 'counter' in value: trace.increment(value['counter'], value.get('value'))
        else: trace.event(value)


@contextmanager
def worker_process(pid):
    trace = _active.get()
    registered = set()
    def register(value):
        # Windows venv 的 Popen.pid 可能只是 redirector；协议补报实际解释器 PID。
        if trace and isinstance(value, int) and value > 0:
            with trace.lock:
                registered.add(value); trace.pids.add(value)
    register(pid)
    try: yield register
    finally:
        if trace:
            with trace.lock:
                trace.pids.difference_update(registered)


@contextmanager
def span(name):
    if not active() and os.environ.get('DDA_WORKER_METRICS') != '1':
        yield; return
    start, status = perf_counter(), 'ok'
    try: yield
    except BaseException as exc:
        status = _status(exc); raise
    finally:
        value = dict(stage=name, seconds=perf_counter()-start, status=status, pid=os.getpid())
        memory = _memory(os.getpid())
        if memory: value['peak_rss_mb'] = memory[1]
        trace = _active.get()
        if trace: trace.event(value)
        else: _emit(value)


def timed(name):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with span(name): return function(*args, **kwargs)
        return call
    return decorate


def observed(name):
    """外层操作写一份记录，内部嵌套操作共享 trace；CLI 与后台线程均可使用。"""
    def decorate(function):
        @wraps(function)
        def call(self, *args, **kwargs):
            if not getattr(self.settings, 'performance_enabled', True): return function(self, *args, **kwargs)
            with capture(self.settings.data_dir / 'performance', name):
                with span(name): return function(self, *args, **kwargs)
        return call
    return decorate


class PerformanceMiddleware:
    """统计实际 ASGI 字节数，不缓存／重复消费请求或响应。"""
    def __init__(self, app, directory, enabled=True):
        self.app, self.directory, self.enabled = app, directory, enabled

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope['type'] != 'http' or not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)
        async def measured_receive():
            message = await receive()
            if message['type'] == 'http.request': count('http.request_bytes', len(message.get('body', b'')))
            return message
        async def measured_send(message):
            if message['type'] == 'http.response.start': count('http.status.'+str(message['status']))
            elif message['type'] == 'http.response.body': count('http.response_bytes', len(message.get('body', b'')))
            await send(message)
        with capture(self.directory, 'http'):
            with span('http'):
                await self.app(scope, measured_receive, measured_send)


if os.environ.get('DDA_WORKER_METRICS') == '1':
    # 导入无模型依赖；在加载权重之前登记 PID，使采样覆盖加载期间的内存。
    _emit({'runtime_pid': os.getpid()})
