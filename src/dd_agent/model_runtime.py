"""四类模型共用的有界 JSONL 运行时。

一个实例内同一时刻只执行一个请求。重模型（OCR、视觉）独占。
两个 0.6B 检索模型（向量和重排序）可以同时留在显存里，共用一把文件租约，避免每次查询来回换入。
收到 done 才确认完整响应。协议错误、取消或生成器提前关闭会销毁正在执行的那个进程。
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
from .maintenance import rotate_log

LIGHT_KINDS = {'embedding', 'reranker'}


def residence_action(kind, *, resident_kind, partner_kind, resident_same, partner_same):
    """决定复用、替换哪一个槽，以及是否需要新进程。两个轻模型共用已有租约。"""
    if resident_same:
        return {'reuse': 'resident', 'discard': [], 'load': None}
    if partner_same:
        return {'reuse': 'partner', 'discard': [], 'load': None}
    if kind not in LIGHT_KINDS:
        discard = [name for name, present in (('partner', partner_kind), ('resident', resident_kind)) if present]
        return {'reuse': None, 'discard': discard, 'load': 'resident'}
    discard = []
    if resident_kind and resident_kind not in LIGHT_KINDS:
        discard.append('resident')
        resident_kind = None
    if partner_kind and partner_kind not in LIGHT_KINDS:
        discard.append('partner')
        partner_kind = None
    if resident_kind == kind:
        discard.append('resident')
        resident_kind = None
    if partner_kind == kind:
        discard.append('partner')
        partner_kind = None
    if resident_kind is None:
        return {'reuse': None, 'discard': discard, 'load': 'resident'}
    if partner_kind is None:
        return {'reuse': None, 'discard': discard, 'load': 'partner'}
    discard.append('partner')
    return {'reuse': None, 'discard': discard, 'load': 'partner'}

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
        self.closed=Event();self.resident=None;self.partner=None;self.lease=None;self.active=None
        self.request=None;self.loads=0;self.last_used=monotonic()
        self.reaper=Thread(target=self._reap,daemon=True,name='model-idle-reaper');self.reaper.start()

    def snapshot(self):
        with self.state:
            r=self.resident
            partner=self.partner
            return dict(pid=r['pid'] if r else None,partner_pid=partner['pid'] if partner else None,
                generation=r['generation'] if r else None,
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

    def _slot_of(self, r):
        if r is None:return None
        if self.resident is r:return 'resident'
        if self.partner is r:return 'partner'
        return None

    def _close_process(self, r, release):
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

    def _release_lease_if_idle(self):
        if self.resident is None and self.partner is None and self.lease is not None:
            self.lease.__exit__(None,None,None)
            self.lease=None

    def _discard(self, *, release=False, slot=None):
        # Caller holds state, including cancel/idle paths. Never wait for admission:
        # the active infer may itself be blocked on a dead worker's stdout.
        # slot 为空时两个槽都结束。文件租约留到两个槽都空才释放。
        names=('partner','resident') if slot is None else (slot,)
        for name in names:
            current=getattr(self, name)
            if not current:continue
            setattr(self, name, None)
            if self.active is current:self.active=None
            self._close_process(current, release)
        self._release_lease_if_idle()

    def close(self):
        self.closed.set()
        self.admission.close()
        with self.state:
            self._discard(release=self.request is None)
        self.reaper.join(timeout=1)

    def cancel(self, request_id):
        with self.state:
            if self.request != request_id:return False
            target=self.active or self.resident
            slot=self._slot_of(target)
            if slot is None:return False
            self._discard(slot=slot);return True

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

    def _load(self,spec,slot):
        from .retrieval import model_slot, RetrievalError
        acquired_now=False
        if self.lease is None:
            lease=model_slot(self.directory)
            try:lease.__enter__()
            except RetrievalError:raise ModelRuntimeError('model_busy') from None
            self.lease=lease
            acquired_now=True
        log=None;process=None
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            log_path=self.directory/'model-runtime.log'
            if self.resident is None and self.partner is None:
                rotate_log(log_path)
            log=log_path.open('ab')
            env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','COMSPEC'}}
            env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',
                PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True',PADDLE_PDX_CACHE_HOME=str(self.directory/'cache'),DDA_WORKER_METRICS='0',
                PYTORCH_CUDA_ALLOC_CONF=os.environ.get('PYTORCH_CUDA_ALLOC_CONF','expandable_segments:True'))
            nonce=uuid4().hex
            process=subprocess.Popen([str(spec.python),str(spec.script),'--serve',nonce],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,env=env)
            r=dict(process=process,owner=None,queue=Queue(8),stop=Event(),log=log,
                spec=spec,generation=uuid4().hex,pid=None)
            r['reader']=Thread(target=self._receive,args=(r,),daemon=True,name='model-protocol-read')
            setattr(self, slot, r);r['reader'].start()
            r['owner']=ProcessOwner(process)
            self.active=r
        except BaseException:
            if getattr(self, slot):self._discard(slot=slot)
            else:
                if process and process.poll() is None:process.kill();process.wait()
                if log:log.close()
                if acquired_now and self.resident is None and self.partner is None:
                    self.lease.__exit__(None,None,None);self.lease=None
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
                def kind_of(slot):
                    current=getattr(self, slot)
                    return current['spec'].config.get('kind') if current else None
                action=residence_action(spec.config.get('kind'),
                    resident_kind=kind_of('resident'), partner_kind=kind_of('partner'),
                    resident_same=bool(self.resident and self.resident['spec']==spec),
                    partner_same=bool(self.partner and self.partner['spec']==spec))
                for name in action['discard']:
                    self._discard(release=True, slot=name)
                if action['reuse']:
                    r=getattr(self, action['reuse']);nonce=None;self.active=r;count('runtime.reused')
                else:
                    r,nonce=self._load(spec, action['load'])
            with worker_process(r['process'].pid) as register:
                if nonce:
                    deadline=min(monotonic()+self.load_timeout,control.deadline or float('inf'))
                    hello=self._next(r,deadline)
                    if hello.get('event')!='hello' or hello.get('nonce')!=nonce:raise ModelRuntimeError('worker_hello')
                    with self.state:
                        if self._slot_of(r) is None:raise ModelRuntimeError('worker_cancelled')
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
                    slot=self._slot_of(r)
                    if not finished and slot is not None:self._discard(slot=slot)
                    self.request=None;self.active=None;self.last_used=monotonic()
                self.admission.release(acquired)


# One broker per application process. 向量和重排序可以同时常驻；视觉或 OCR 会先卸下这两个轻模型。
# 8GB 卡上视觉约 3.6GB，不能和向量叠在一起。两个 0.6B 检索模型大约各 2.4GB，加上桌面约 1.5GB，仍放得下。
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
    from .cache import cache_directory, file_entries, venv_runtime_descriptor
    if kind not in {'ocr','vision','embedding','reranker'}:raise ValueError('Unknown model kind')
    python=settings.ocr_python if kind=='ocr' else settings.vision_python if kind=='vision' else settings.retrieval_python
    models=settings.ocr_models_dir if kind=='ocr' else settings.vision_models_dir if kind=='vision' else settings.embedding_dir if kind=='embedding' else settings.reranker_dir
    script=Path(__file__).parent/'workers/resident_worker.py'
    sources=[script,Path(__file__),Path(__file__).with_name('worker_protocol.py')]
    sources+=list(script.parent.glob('*_worker.py'))
    files=sorted(p for p in models.rglob('*') if p.is_file() and '.cache' not in p.parts and p.suffix in {'.json','.txt','.safetensors','.pdiparams','.yml'})
    threads=(settings.ocr_cpu_threads or settings.model_cpu_threads) if kind=='ocr' else settings.model_cpu_threads
    device={'ocr':settings.ocr_device,'vision':settings.vision_device,'embedding':settings.embedding_device,'reranker':settings.reranker_device}[kind]
    config=dict(kind=kind,threads=threads,device=device,manifest_dir=str(cache_directory(settings)))
    if kind=='ocr':
        config['enable_mkldnn']=settings.ocr_enable_mkldnn
        config['rec_batch']=settings.ocr_rec_batch
        config['page_batch']=settings.ocr_page_batch
    else:config['dtype']={'vision':settings.vision_dtype,'embedding':settings.embedding_dtype,'reranker':settings.reranker_dtype}[kind]
    config['embedding' if kind=='embedding' else 'reranker' if kind=='reranker' else 'models']=str(models.resolve())
    if kind=='vision':
        from .config import vision_token_ceiling
        config['max_new_tokens']=vision_token_ceiling(settings)
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
