"""只调度模型请求。SQL、缓存读取和 HTTP 轮询不进入这里。

交互与后台分别 FIFO；连续交互最多三个，随后优先给已等待的后台一次。
令牌只在页／区域边界释放，不会为插队中断另一项有效计算。
"""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Condition
from time import monotonic
import math

from .performance import count, span


class AdmissionError(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


@dataclass(frozen=True)
class RequestControl:
    priority: str = 'background'
    cancel: object = None
    deadline: float | None = None
    defer_busy: bool = False

    def check(self):
        if self.cancel is not None and self.cancel.is_set():raise AdmissionError('cancelled')
        if self.deadline is not None and monotonic()>=self.deadline:raise AdmissionError('deadline')


_control=ContextVar('model_request_control',default=RequestControl())


def current_control():return _control.get()


@contextmanager
def request_scope(priority='background',*,cancel=None,deadline=None,defer_busy=False):
    if priority not in ('interactive','background'):raise ValueError('Unknown request priority')
    token=_control.set(RequestControl(priority,cancel,deadline,defer_busy))
    try:yield _control.get()
    finally:_control.reset(token)


class FairAdmission:
    def __init__(self,*,max_waiting=8):
        if type(max_waiting) is not int or not 1<=max_waiting<=128:raise ValueError('Invalid admission capacity')
        self.limit=max_waiting;self.condition=Condition()
        self.queues={kind:deque() for kind in ('interactive','background')}
        self.active=None;self.streak=0;self.closed=False

    def snapshot(self):
        with self.condition:
            return dict(waiting=sum(map(len,self.queues.values())),interactive=len(self.queues['interactive']),
                background=len(self.queues['background']),active=self.active is not None,closed=self.closed)

    def _next(self):
        interactive,background=self.queues['interactive'],self.queues['background']
        if background and (not interactive or self.streak>=3):return background[0]
        return interactive[0] if interactive else background[0] if background else None

    def acquire(self,priority='background',*,timeout=3.,cancel=None):
        if priority not in self.queues or not math.isfinite(timeout) or timeout<=0:raise ValueError('Invalid admission request')
        deadline=monotonic()+timeout;ticket=object()
        with span('resource.queue_wait'),self.condition:
            if self.closed:raise AdmissionError('closed')
            if sum(map(len,self.queues.values()))>=self.limit:
                count('resource.queue_full');raise AdmissionError('busy')
            self.queues[priority].append(ticket)
            try:
                while True:
                    if self.closed:raise AdmissionError('closed')
                    if cancel is not None and cancel.is_set():raise AdmissionError('cancelled')
                    if monotonic()>=deadline:raise AdmissionError('busy')
                    if self.active is None and self._next() is ticket:
                        self.queues[priority].popleft();self.active=ticket
                        self.streak=min(3,self.streak+1) if priority=='interactive' else 0
                        count('resource.admitted.'+priority)
                        return ticket
                    self.condition.wait(min(.05,deadline-monotonic()))
            finally:
                if ticket in self.queues[priority]:self.queues[priority].remove(ticket)
                self.condition.notify_all()

    def release(self,ticket):
        with self.condition:
            if self.active is not ticket:raise ValueError('Admission token is no longer owned')
            self.active=None;self.condition.notify_all()

    def close(self):
        with self.condition:self.closed=True;self.condition.notify_all()
