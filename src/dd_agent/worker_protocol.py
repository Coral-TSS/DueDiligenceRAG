"""模型无关的常驻服务端。load/infer 回调才允许导入各自的模型库。

hello 之后等待宿主先验证 PID／纳入作业对象；收到 load 才加载权重。
宿主通过结束所属作业对象取消不可中断的推理，不把 cancel 当作完成。
"""
from contextlib import redirect_stdout
import json
import os
import sys

from . import performance

FIELDS=('request_id','runtime_generation','model_profile','operation')
MAX_FRAME=16*1024*1024


def serve(load, infer, nonce):
    output=sys.__stdout__
    current=None
    def emit(value):
        line=json.dumps(value,ensure_ascii=False)
        if len(line.encode('utf-8'))+1>MAX_FRAME:raise ValueError('Output budget')
        print(line,file=output,flush=True)
    def metric(value):
        if current is not None:emit(dict(current,event='metric',value=value))
    old_emit=performance._emit
    old_enabled=os.environ.get('DDA_WORKER_METRICS')
    performance._emit=metric
    os.environ['DDA_WORKER_METRICS']='1'
    try:
        emit(dict(event='hello',nonce=nonce,pid=os.getpid(),ppid=os.getppid()))
        engine=None;identity=None
        while True:
            line=sys.stdin.buffer.readline(MAX_FRAME+1)
            if not line:return  # 宿主在加载前死亡时，不加载权重，直接退出。
            if len(line)>MAX_FRAME:raise ValueError('Input budget')
            frame=json.loads(line)
            if not isinstance(frame,dict) or any(not isinstance(frame.get(k),str) or not frame[k] for k in FIELDS) or not isinstance(frame.get('payload'),dict):
                raise ValueError('Invalid frame')
            current={k:frame[k] for k in FIELDS}
            candidate=(frame['runtime_generation'],frame['model_profile'])
            if frame['operation']=='load':
                if identity is not None:raise ValueError('Already loaded')
                with redirect_stdout(sys.stderr):engine=load(frame['payload'])
                identity=candidate
                emit(dict(current,event='ready'))
            else:
                if candidate!=identity:raise ValueError('Wrong model generation')
                if frame['operation']=='shutdown':return
                # Keep library progress bars/logs away from stdout, including
                # when a generator does its actual work during iteration.
                with redirect_stdout(sys.stderr):
                    for value in infer(engine,frame['operation'],frame['payload']):
                        if not isinstance(value,dict):raise ValueError('Invalid business result')
                        emit(dict(current,event='record',value=value))
                emit(dict(current,event='done'))
            current=None
    except Exception:
        if current is not None:emit(dict(current,event='error',code='worker_failed'))
        return 1
    finally:
        performance._emit=old_emit
        if old_enabled is None:os.environ.pop('DDA_WORKER_METRICS',None)
        else:os.environ['DDA_WORKER_METRICS']=old_enabled
