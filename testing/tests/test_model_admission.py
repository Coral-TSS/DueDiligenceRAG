"""有限优先且后台不饥饿；只测试调度顺序，不依赖模型耗时。"""
from threading import Event, Thread
from time import monotonic, sleep
import pytest


def queue(**options):
    try:
        from dd_agent.model_admission import FairAdmission
    except ImportError:pytest.fail('RG-05i fair admission missing')
    return FairAdmission(**options)


def wait(check):
    end=monotonic()+3
    while monotonic()<end:
        if check():return
        sleep(.005)
    pytest.fail('Admission fixture did not reach checkpoint')


def test_interactive_priority_is_fifo_and_yields_after_three(tmp_path):
    q=queue(max_waiting=8)
    token=q.acquire('background',timeout=1)
    order=[];threads=[]
    def run(name,kind):
        held=q.acquire(kind,timeout=3)
        try:order.append(name)
        finally:q.release(held)
    try:
        for n,(name,kind) in enumerate([('b1','background'),('i1','interactive'),('i2','interactive'),('i3','interactive'),('i4','interactive'),('b2','background')],1):
            thread=Thread(target=run,args=(name,kind));threads.append(thread);thread.start()
            wait(lambda:q.snapshot()['waiting']==n)
        q.release(token);token=None
        for thread in threads:thread.join(4)
        assert order==['i1','i2','i3','b1','i4','b2']
    finally:
        if token:q.release(token)
        q.close()
        for thread in threads:thread.join(4)


def test_cancel_timeout_close_and_capacity_do_not_consume_a_turn():
    from dd_agent.model_admission import AdmissionError
    q=queue(max_waiting=1);active=q.acquire('background',timeout=1)
    cancelled=Event();errors=[]
    def run():
        try:q.acquire('interactive',timeout=2,cancel=cancelled)
        except AdmissionError as exc:errors.append(exc.code)
    thread=Thread(target=run);thread.start()
    try:
        wait(lambda:q.snapshot()['waiting']==1)
        with pytest.raises(AdmissionError,match='busy'):q.acquire('background',timeout=.02)
        cancelled.set();thread.join(2)
        assert errors==['cancelled'] and q.snapshot()['waiting']==0
        with pytest.raises(AdmissionError,match='busy'):q.acquire('interactive',timeout=.02)
        q.release(active);active=None
        next_token=q.acquire('background',timeout=1);q.release(next_token)
        q.close()
        with pytest.raises(AdmissionError,match='closed'):q.acquire('interactive',timeout=1)
    finally:
        cancelled.set()
        if active:q.release(active)
        q.close();thread.join(2)
