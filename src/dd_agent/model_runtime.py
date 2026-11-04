"""四类模型共用的单驻留、有界 JSONL 运行时。

一个实例内同一时刻只执行一个请求。模型驻留期间一直持有跨进程租约；
收到 done 才确认完整响应，任何协议错误、取消或生成器提前关闭都销毁实例。
模型客户端的常驻路径不重复获取外层 model_slot，避免自身二次加锁。
"""
from dataclasses import dataclass, replace
import json
import atexit
import hashlib
import math
import os
from pathlib import Path
from queue import Queue, Empty, Full
import subprocess
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from .performance import accept_worker, count, worker_process
from .process_owner import ProcessOwner
from .model_admission import FairAdmission, AdmissionError, current_control

MAX_FRAME = 16 * 1024 * 1024
FIELDS = ('request_id', 'runtime_generation', 'model_profile', 'operation')


class ModelRuntimeError(Exception):
    """只暴露分类信息，不把 worker stderr／输入材料带回接口。"""


@dataclass(frozen=True)
class WorkerSpec:
    python: str | Path
    script: str | Path
    profile: str
    config: dict


class ModelRuntime:
    def __init__(self, directory, *, idle_seconds=30., load_timeout=300., queue_timeout=3., max_waiting=8):
        if any(not math.isfinite(v) or v <= 0 for v in (idle_seconds, load_timeout, queue_timeout)) or type(max_waiting) is not int or not 1 <= max_waiting <= 128:
            raise ValueError('Invalid model runtime limits')
        self.directory = Path(directory)
        self.idle_seconds,self.load_timeout,self.queue_timeout = idle_seconds,load_timeout,queue_timeout
        self.state=RLock();self.admission=FairAdmission(max_waiting=max_waiting)
        self.closed=Event();self.resident=None;self.request=None;self.loads=0;self.last_used=monotonic()
        self.reaper=Thread(target=self._reap,daemon=True,name='model-idle-reaper');self.reaper.start()

    def snapshot(self):
        with self.state:
            r=self.resident
            return dict(pid=r['pid'] if r else None,generation=r['generation'] if r else None,
                profile=r['spec'].profile if r else None,request_id=self.request,loads=self.loads)

    def _reap(self):
        while not self.closed.wait(min(.5,self.idle_seconds/2)):
            with self.state:
                if self.request is None and monotonic()-self.last_used >= self.idle_seconds:self._discard(release=True)

    def _ask_shutdown(self, r):
        # 只对常驻模型入口发 shutdown，让它在退出前清 CUDA／Paddle 缓存。
        # 测试夹具和其他一次性进程仍直接结束，避免空等。
        script=str(r['spec'].script).replace('\\','/')
        if not script.endswith('workers/resident_worker.py'):return
        process=r['process']
        if process.poll() is not None or process.stdin is None:return
        frame=dict(request_id='release',runtime_generation=r['generation'],model_profile=r['spec'].profile,
                   operation='shutdown',payload={})
        try:
            process.stdin.write((json.dumps(frame)+'\n').encode())
            process.stdin.flush()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
            pass

    def _discard(self, *, release=False):
        # Caller holds state, including cancel/idle paths. Never wait for admission:
        # the active infer may itself be blocked on a dead worker's stdout.
        r=self.resident;self.resident=None
        if not r:return
        if release:self._ask_shutdown(r)
        r['stop'].set()
        if r.get('owner'):r['owner'].close()
        process=r['process']
        if process.poll() is None:process.kill()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:pass
        for pipe in (process.stdin,process.stdout):
            if pipe:
                try:pipe.close()
                except OSError:pass
        r['reader'].join(timeout=1)
        r['log'].close()
        r['lease'].__exit__(None,None,None)

    def close(self):
        self.closed.set()
        self.admission.close()
        with self.state:
            self._discard(release=self.request is None)
        self.reaper.join(timeout=1)

    def cancel(self, request_id):
        with self.state:
            if self.request != request_id or self.resident is None:return False
            self._discard();return True

    @staticmethod
    def _receive(r):
        try:
            while not r['stop'].is_set():
                line=r['process'].stdout.readline(MAX_FRAME+1)
                if not line or len(line)>MAX_FRAME:raise ValueError('frame')
                frame=json.loads(line)
                if not isinstance(frame,dict):raise ValueError('frame')
                while not r['stop'].is_set():
                    try:r['queue'].put(frame,timeout=.1);break
                    except Full:pass
        except (OSError,ValueError):
            while not r['stop'].is_set():
                try:r['queue'].put(None,timeout=.1);break
                except Full:pass

    def _next(self,r,deadline,head=None):
        while True:
            current_control().check()
            if r['stop'].is_set() or self.closed.is_set():raise ModelRuntimeError('worker_cancelled')
            remaining=deadline-monotonic()
            if remaining<=0:raise ModelRuntimeError('worker_timeout')
            try:frame=r['queue'].get(timeout=min(.1,remaining))
            except Empty:continue
            if frame is None:raise ModelRuntimeError('worker_protocol')
            if head is not None and any(frame.get(k)!=head[k] for k in FIELDS):raise ModelRuntimeError('worker_identity')
            if frame.get('event')=='metric':
                accept_worker(frame.get('value'));continue
            return frame

    def _send(self,r,frame,deadline):
        data=(json.dumps(frame,ensure_ascii=False)+'\n').encode('utf-8')
        if len(data)>MAX_FRAME:raise ModelRuntimeError('worker_input_budget')
        done=Event();errors=[]
        def write():
            try:r['process'].stdin.write(data);r['process'].stdin.flush()
            except (OSError,ValueError):errors.append(True)
            finally:done.set()
        writer=Thread(target=write,daemon=True,name='model-protocol-write');writer.start()
        while not done.is_set():
            current_control().check()
            if r['stop'].is_set() or self.closed.is_set():raise ModelRuntimeError('worker_cancelled')
            if monotonic()>=deadline:raise ModelRuntimeError('worker_timeout')
            done.wait(min(.05,deadline-monotonic()))
        if errors:raise ModelRuntimeError('worker_pipe')

    def _load(self,spec):
        from .retrieval import model_slot, RetrievalError
        lease=model_slot(self.directory)
        try:lease.__enter__()
        except RetrievalError:raise ModelRuntimeError('model_busy') from None
        log=None;process=None
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            log=(self.directory/'model-runtime.log').open('ab')
            env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','COMSPEC'}}
            env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True',PADDLE_PDX_CACHE_HOME=str(self.directory/'cache'),DDA_WORKER_METRICS='0',
                PYTORCH_CUDA_ALLOC_CONF=os.environ.get('PYTORCH_CUDA_ALLOC_CONF','expandable_segments:True'))
            nonce=uuid4().hex
            process=subprocess.Popen([str(spec.python),str(spec.script),'--serve',nonce],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,env=env)
            r=dict(process=process,owner=None,queue=Queue(8),stop=Event(),lease=lease,log=log,
                spec=spec,generation=uuid4().hex,pid=None)
            r['reader']=Thread(target=self._receive,args=(r,),daemon=True,name='model-protocol-read')
            self.resident=r;r['reader'].start()
            r['owner']=ProcessOwner(process)
        except BaseException:
            if self.resident:self._discard()
            else:
                if process and process.poll() is None:process.kill();process.wait()
                if log:log.close()
                lease.__exit__(None,None,None)
            raise
        return r,nonce

    def infer(self,spec,operation,payload,*,request_id=None,timeout=1200.):
        if not math.isfinite(timeout) or timeout<=0:raise ValueError('Invalid model timeout')
        # frozen dataclass does not freeze nested dictionaries. Retain a detached
        # load descriptor so later caller mutations cannot disguise a new model.
        spec=replace(spec,config=json.loads(json.dumps(spec.config,allow_nan=False)))
        acquired=None;finished=False;r=None
        try:
            control=current_control();control.check()
            # 后台短等待后归还任务，避免协调器长期卡在重模型上。
            # 交互查询已有覆盖全部推理的总截止时间，应沿用剩余预算排队；
            # 不能在 OCR 尚未释放时被后台默认 3 秒提前截断。仍只领一个
            # FIFO ticket，容量、取消和每三次交互后让行的规则保持不变。
            wait=self.queue_timeout
            if control.deadline is not None:
                remaining=max(.0001,control.deadline-monotonic())
                wait=remaining if control.priority=='interactive' else min(wait,remaining)
            acquired=self.admission.acquire(control.priority,timeout=wait,cancel=control.cancel)
            control.check()
            with self.state:
                if self.closed.is_set():raise ModelRuntimeError('runtime_closed')
                self.request=request_id or uuid4().hex
                if self.resident and self.resident['spec']!=spec:self._discard(release=True)
                if self.resident is None:r,nonce=self._load(spec)
                else:r=self.resident;nonce=None;count('runtime.reused')
            with worker_process(r['process'].pid) as register:
                if nonce:
                    deadline=min(monotonic()+self.load_timeout,control.deadline or float('inf'))
                    hello=self._next(r,deadline)
                    if hello.get('event')!='hello' or hello.get('nonce')!=nonce:raise ModelRuntimeError('worker_hello')
                    with self.state:
                        if self.resident is not r:raise ModelRuntimeError('worker_cancelled')
                        r['owner'].attach_actual(hello.get('pid'),hello.get('ppid'));r['pid']=hello['pid']
                    register(r['pid'])
                    head=dict(request_id=self.request,runtime_generation=r['generation'],model_profile=spec.profile,operation='load')
                    self._send(r,dict(head,payload=spec.config),deadline)
                    if self._next(r,deadline,head).get('event')!='ready':raise ModelRuntimeError('worker_load')
                    self.loads+=1;count('runtime.loads')
                else:register(r['pid'])
                head=dict(request_id=self.request,runtime_generation=r['generation'],model_profile=spec.profile,operation=operation)
                deadline=min(monotonic()+timeout,control.deadline or float('inf'))
                self._send(r,dict(head,payload=payload),deadline)
                while True:
                    frame=self._next(r,deadline,head)
                    if frame.get('event')=='done':finished=True;count('runtime.requests');break
                    if frame.get('event')!='record' or not isinstance(frame.get('value'),dict):raise ModelRuntimeError('worker_protocol')
                    yield frame['value']
        except AdmissionError as exc:
            raise ModelRuntimeError('model_busy' if exc.code=='busy' else 'request_'+exc.code) from None
        except (OSError,ValueError) as exc:
            raise ModelRuntimeError('worker_unavailable') from None
        finally:
            if acquired:
                with self.state:
                    if not finished and self.resident is r:self._discard()
                    self.request=None;self.last_used=monotonic()
                self.admission.release(acquired)


# One broker per application process, shared by all four lightweight clients.
# Workers keep their original isolated venvs; switching kind replaces the resident.
# 8GB 卡上视觉约 3.6GB、向量约 2.5GB，再加上桌面占用，不能同时常驻。换模型必须先释放前一个进程。
_registry_lock=RLock()
_broker=None
_broker_key=None


def close_runtime(settings=None):
    global _broker,_broker_key
    with _registry_lock:
        if _broker is not None and (settings is None or _broker_key[0]==str(settings.data_dir.resolve())):
            _broker.close();_broker=None;_broker_key=None


atexit.register(close_runtime)


def worker_spec(settings,kind):
    from .cache import file_entries, venv_runtime_descriptor
    if kind not in {'ocr','vision','embedding','reranker'}:raise ValueError('Unknown model kind')
    python=settings.ocr_python if kind=='ocr' else settings.vision_python if kind=='vision' else settings.retrieval_python
    models=settings.ocr_models_dir if kind=='ocr' else settings.vision_models_dir if kind=='vision' else settings.embedding_dir if kind=='embedding' else settings.reranker_dir
    script=Path(__file__).parent/'workers/resident_worker.py'
    sources=[script,Path(__file__),Path(__file__).with_name('worker_protocol.py')]
    sources+=list(script.parent.glob('*_worker.py'))
    files=sorted(p for p in models.rglob('*') if p.is_file() and '.cache' not in p.parts and p.suffix in {'.json','.txt','.safetensors','.pdiparams','.yml'})
    threads=(settings.ocr_cpu_threads or settings.model_cpu_threads) if kind=='ocr' else settings.model_cpu_threads
    device={'ocr':settings.ocr_device,'vision':settings.vision_device,'embedding':settings.embedding_device,'reranker':settings.reranker_device}[kind]
    config=dict(kind=kind,threads=threads,device=device)
    if kind=='ocr':config['enable_mkldnn']=settings.ocr_enable_mkldnn
    else:config['dtype']={'vision':settings.vision_dtype,'embedding':settings.embedding_dtype,'reranker':settings.reranker_dtype}[kind]
    config['embedding' if kind=='embedding' else 'reranker' if kind=='reranker' else 'models']=str(models.resolve())
    if kind=='vision':config['max_new_tokens']=settings.vision_max_new_tokens
    # paddlepaddle 同时匹配 CPU 包和 paddlepaddle-gpu 的 dist-info。
    packages=('paddleocr','paddlex','paddlepaddle','pillow') if kind=='ocr' else ('torch','transformers','tokenizers','pillow')
    environment=venv_runtime_descriptor(python,packages=packages)
    descriptor=dict(config=config,runtime=environment,files=file_entries(files+sources))
    # Unknown environments cannot prove equivalence across calls. Execute safely
    # but do not retain an unverifiable model as a reusable cache hit.
    if environment is None:descriptor['unverified']=uuid4().hex
    profile=hashlib.sha256(json.dumps(descriptor,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return WorkerSpec(python,script,profile,config)


def runtime_records(settings,kind,operation,payload,*,timeout=1200.):
    global _broker,_broker_key
    spec=worker_spec(settings,kind)
    key=(str(settings.data_dir.resolve()),settings.model_idle_seconds,settings.model_load_timeout,settings.model_queue_timeout,settings.model_max_waiting)
    with _registry_lock:
        if _broker_key!=key:
            if _broker is not None:
                if _broker.snapshot()['request_id'] is not None:raise ModelRuntimeError('model_busy')
                _broker.close()
            _broker=ModelRuntime(settings.data_dir,idle_seconds=settings.model_idle_seconds,
                load_timeout=settings.model_load_timeout,queue_timeout=settings.model_queue_timeout,max_waiting=settings.model_max_waiting)
            _broker_key=key
        broker=_broker
    yield from broker.infer(spec,operation,payload,timeout=timeout)
