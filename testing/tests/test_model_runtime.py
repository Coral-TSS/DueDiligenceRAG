"""协议夹具执行真实子进程；不 import 或下载任何模型。"""
from pathlib import Path
import sys
from threading import Thread, Event
from time import monotonic, sleep

import pytest


def runtime(tmp_path, **options):
    try:
        from dd_agent.model_runtime import ModelRuntime, WorkerSpec
    except ModuleNotFoundError:
        pytest.fail('RG-05e resident runtime missing')
    script = Path(__file__).resolve().parents[1] / 'scripts/performance/runtime_fixture.py'
    spec = WorkerSpec(sys.executable, script, 'fixture-v1', {})
    return ModelRuntime(tmp_path, idle_seconds=.2, load_timeout=10, **options), spec


def test_interactive_wait_uses_query_deadline_while_background_keeps_short_budget(tmp_path):
    from dd_agent.model_admission import request_scope
    from dd_agent.model_runtime import ModelRuntimeError
    broker,spec=runtime(tmp_path,queue_timeout=.05,max_waiting=1)
    broker.idle_seconds=30
    values=[];errors=[];thread=None;held=None
    def query():
        try:
            with request_scope('interactive',deadline=monotonic()+2):
                values.extend(broker.infer(spec,'echo',{'value':9},timeout=2))
        except ModelRuntimeError as exc:errors.append(str(exc))
    try:
        list(broker.infer(spec,'echo',{},timeout=2));pid=broker.snapshot()['pid']
        held=broker.admission.acquire('background')
        thread=Thread(target=query);thread.start()
        end=monotonic()+1
        while broker.admission.snapshot()['interactive']!=1 and monotonic()<end:sleep(.001)
        # Full queues still reject immediately, even for a long-deadline query.
        with request_scope('interactive',deadline=monotonic()+2):
            with pytest.raises(ModelRuntimeError,match='busy'):list(broker.infer(spec,'echo',{},timeout=2))
        thread.join(.15)
        assert thread.is_alive(),'Query returned busy before its own budget expired: '+repr(errors)
        assert broker.snapshot()['pid']==pid
        broker.admission.release(held);held=None
        thread.join(3)
        assert not thread.is_alive() and not errors and values[0]['value']==9
        held=broker.admission.acquire('background')
        # Background requests continue to yield after the configured short wait.
        with request_scope('background',deadline=monotonic()+2):
            with pytest.raises(ModelRuntimeError,match='busy'):list(broker.infer(spec,'echo',{},timeout=2))
    finally:
        if held:broker.admission.release(held)
        broker.close()
        if thread:thread.join(3)


def test_interactive_queue_wait_still_expires_at_original_query_deadline(tmp_path):
    from dd_agent.model_admission import request_scope,AdmissionError
    from dd_agent.model_runtime import ModelRuntimeError
    broker,spec=runtime(tmp_path,queue_timeout=.01);held=broker.admission.acquire('background')
    try:
        with request_scope('interactive',deadline=monotonic()+.1) as control:
            with pytest.raises(ModelRuntimeError,match='busy'):list(broker.infer(spec,'echo',{},timeout=2))
            with pytest.raises(AdmissionError,match='deadline'):control.check()
        assert broker.admission.snapshot()['waiting']==0
    finally:broker.admission.release(held);broker.close()


def test_reuses_actual_interpreter_and_releases_idle_lease(tmp_path):
    from dd_agent.retrieval import model_slot, RetrievalError
    broker, spec = runtime(tmp_path)
    try:
        first = list(broker.infer(spec, 'echo', {'value': 1}, timeout=2))
        second = list(broker.infer(spec, 'echo', {'value': 2}, timeout=2))
        assert first[0]['pid'] == second[0]['pid']
        assert broker.snapshot()['loads'] == 1
        with pytest.raises(RetrievalError):
            with model_slot(tmp_path): pass
        end = monotonic() + 3
        while broker.snapshot()['pid'] and monotonic() < end: sleep(.02)
        assert broker.snapshot()['pid'] is None
        with model_slot(tmp_path): pass
    finally: broker.close()


@pytest.mark.parametrize('action', ['bad_id', 'bad_generation', 'bad_profile', 'bad_operation', 'bad_json', 'incomplete', 'crash', 'hang'])
def test_protocol_fault_discards_process_and_next_request_recovers(tmp_path, action):
    broker, spec = runtime(tmp_path)
    from dd_agent.model_runtime import ModelRuntimeError
    try:
        list(broker.infer(spec, 'echo', {}, timeout=2))
        old = broker.snapshot()['generation']
        with pytest.raises(ModelRuntimeError):
            list(broker.infer(spec, 'echo', {'action': action}, timeout=.3))
        assert broker.snapshot()['pid'] is None
        assert list(broker.infer(spec, 'echo', {'value': 7}, timeout=2))[0]['value'] == 7
        assert broker.snapshot()['generation'] != old
    finally: broker.close()


def test_cancellation_is_bound_to_request_and_queue_is_bounded(tmp_path):
    broker, spec = runtime(tmp_path, max_waiting=1, queue_timeout=.15)
    from dd_agent.model_runtime import ModelRuntimeError
    errors = []
    def run():
        try: list(broker.infer(spec, 'echo', {'action':'hang'}, request_id='active', timeout=30))
        except ModelRuntimeError: errors.append(True)
    thread = Thread(target=run)
    try:
        list(broker.infer(spec, 'echo', {}, timeout=2))
        thread.start()
        end = monotonic() + 3
        while broker.snapshot()['request_id'] != 'active' and monotonic() < end: sleep(.01)
        assert not broker.cancel('old-request')
        with pytest.raises(ModelRuntimeError, match='busy'):
            list(broker.infer(spec, 'echo', {}, timeout=2))
        assert broker.cancel('active')
        thread.join(5)
        assert not thread.is_alive() and errors
        assert list(broker.infer(spec, 'echo', {}, timeout=2))
    finally:
        broker.close(); thread.join(5)


def test_changing_profile_replaces_resident_process(tmp_path):
    from dataclasses import replace
    broker, spec = runtime(tmp_path)
    try:
        old = list(broker.infer(spec, 'echo', {}, timeout=2))[0]['pid']
        new = list(broker.infer(replace(spec, profile='fixture-v2'), 'echo', {}, timeout=2))[0]['pid']
        assert old != new and broker.snapshot()['loads'] == 2
    finally: broker.close()


def test_mutating_load_config_cannot_silently_reuse_old_instance(tmp_path):
    broker,spec=runtime(tmp_path)
    try:
        first=list(broker.infer(spec,'echo',{},timeout=2))[0]['pid']
        spec.config['new_setting']=True
        second=list(broker.infer(spec,'echo',{},timeout=2))[0]['pid']
        assert first!=second
    finally:broker.close()


def test_queued_interactive_cancel_does_not_kill_active_background(tmp_path):
    from dd_agent.model_admission import request_scope
    from dd_agent.model_runtime import ModelRuntimeError
    broker,spec=runtime(tmp_path,queue_timeout=2)
    broker.idle_seconds=30
    stopped=Event();errors=[]
    def active():
        try:list(broker.infer(spec,'echo',{'action':'hang'},request_id='background',timeout=30))
        except ModelRuntimeError:pass
    def queued():
        try:
            with request_scope('interactive',cancel=stopped):list(broker.infer(spec,'echo',{},timeout=2))
        except ModelRuntimeError as exc:errors.append(str(exc))
    a=Thread(target=active);b=Thread(target=queued)
    try:
        list(broker.infer(spec,'echo',{},timeout=2));pid=broker.snapshot()['pid']
        a.start()
        end=monotonic()+3
        while broker.snapshot()['request_id']!='background' and monotonic()<end:sleep(.01)
        b.start();sleep(.05);stopped.set();b.join(2)
        assert errors==['request_cancelled'] and broker.snapshot()['request_id']=='background' and broker.snapshot()['pid']==pid
        broker.cancel('background');a.join(3)
    finally:
        stopped.set();broker.close()
        if a.ident:a.join(3)
        if b.ident:b.join(3)


def test_active_request_control_cancels_its_worker(tmp_path):
    from dd_agent.model_admission import request_scope
    from dd_agent.model_runtime import ModelRuntimeError
    from threading import Timer
    broker,spec=runtime(tmp_path);stop=Event()
    try:
        list(broker.infer(spec,'echo',{},timeout=2))
        timer=Timer(.05,stop.set);timer.start()
        with request_scope('interactive',cancel=stop):
            with pytest.raises(ModelRuntimeError,match='cancelled'):
                list(broker.infer(spec,'echo',{'action':'hang'},timeout=5))
        timer.join(1)
        assert broker.snapshot()['pid'] is None
    finally:broker.close()


def test_load_timeout_releases_process_and_lease(tmp_path):
    from dataclasses import replace
    from dd_agent.model_runtime import ModelRuntimeError
    broker,spec=runtime(tmp_path)
    broker.load_timeout=.3
    try:
        with pytest.raises(ModelRuntimeError,match='timeout'):
            list(broker.infer(replace(spec,config={'load_hang':True}),'echo',{},timeout=2))
        assert broker.snapshot()['pid'] is None
        assert list(broker.infer(spec,'echo',{},timeout=2))
    finally:broker.close()


def test_shared_server_protocol_keeps_metrics_out_of_business_results(tmp_path):
    from dataclasses import replace
    from dd_agent.performance import capture
    import json
    broker, spec = runtime(tmp_path)
    spec=replace(spec,script=spec.script.with_name('runtime_server_fixture.py'))
    try:
        with capture(tmp_path/'metrics','test'):
            first=list(broker.infer(spec,'echo',{'value':11},timeout=3))
            second=list(broker.infer(spec,'echo',{'value':12},timeout=3))
        assert first[0]['pid']==second[0]['pid'] and second[0]['value']==12
        report=json.loads(next((tmp_path/'metrics').glob('*.json')).read_text(encoding='utf-8'))
        assert report['counters']['fixture.loads']==1
        assert report['counters']['fixture.requests']==2
        assert set(second[0])=={'pid','value'}
    finally:broker.close()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows job object')
def test_parent_exit_kills_actual_interpreter(tmp_path):
    import json
    import subprocess
    import ctypes
    from ctypes import wintypes
    script = Path(__file__).resolve().parents[1] / 'scripts/performance/runtime_fixture.py'
    # os._exit bypasses Python finally/atexit: only the OS owner can reclaim the child.
    code = '''import json, os, sys
from dd_agent.model_runtime import ModelRuntime, WorkerSpec
b=ModelRuntime(sys.argv[1], idle_seconds=30)
s=WorkerSpec(sys.executable,sys.argv[2],'test',{})
print(json.dumps(list(b.infer(s,'echo',{},timeout=3))[0]),flush=True)
os._exit(0)
'''
    process = subprocess.Popen([sys.executable,'-c',code,str(tmp_path),str(script)],stdout=subprocess.PIPE,text=True)
    try:
        pid = json.loads(process.stdout.readline())['pid']
        assert process.wait(timeout=10) == 0
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
        api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x100000,False,pid)
        if handle:
            try:assert api.WaitForSingleObject(handle,5000)==0
            finally:api.CloseHandle(handle)
    finally:
        if process.poll() is None:process.kill();process.wait()
        process.stdout.close()


_SECRET_KEYS = (
    'DDA_DATABASE_URL', 'DATABASE_URL', 'HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN',
    'OPENAI_API_KEY', 'AWS_SECRET_ACCESS_KEY', 'AWS_ACCESS_KEY_ID',
)
_IDENTITY_KEYS = ('LOGNAME', 'USER', 'LNAME', 'USERNAME')
# torch 2.11 cache_dir() 仅在 TORCHINDUCTOR_CACHE_DIR 未设置时调用 getpass.getuser()。
_PWDLESS = r'''
import builtins, os, sys
real = builtins.__import__
def blocked(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pwd" or name.startswith("pwd."):
        raise ModuleNotFoundError("No module named 'pwd'")
    return real(name, globals, locals, fromlist, level)
builtins.__import__ = blocked
sys.modules.pop("pwd", None)

def cache_dir():
    path = os.environ.get("TORCHINDUCTOR_CACHE_DIR")
    if path is None:
        import getpass, re, tempfile
        sanitized = re.sub(r'[\\/:*?"<>|]', "_", getpass.getuser())
        path = os.path.join(tempfile.gettempdir(), "torchinductor_" + sanitized)
        os.environ["TORCHINDUCTOR_CACHE_DIR"] = path
    os.makedirs(path, exist_ok=True)
    return path

mode = sys.argv[1]
if mode == "cache":
    for key in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        os.environ.pop(key, None)
    print(cache_dir())
elif mode == "user":
    import getpass
    print(getpass.getuser())
else:
    raise SystemExit(2)
'''


def _hide_identity(monkeypatch):
    for key in _IDENTITY_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv('TORCHINDUCTOR_CACHE_DIR', raising=False)


def _plant_secrets(monkeypatch):
    for key in _SECRET_KEYS:
        monkeypatch.setenv(key, 'secret-value')


def _pwdless(env, mode):
    import subprocess
    completed = subprocess.run([sys.executable, '-c', _PWDLESS, mode], env=env, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


class _StoppedWorker:
    def __init__(self):
        import io
        self.stdout = io.StringIO('{"ok":true}\n')
        self.stderr = io.StringIO('')
        self.pid = 1
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return 0

    def kill(self):
        self.returncode = -9


def _oneshot_env(directory):
    import dd_agent.reading as reading
    captured = {}

    def spy(args, **kwargs):
        captured['env'] = kwargs['env']
        return _StoppedWorker()

    original = reading.subprocess.Popen
    reading.subprocess.Popen = spy
    try:
        list(reading.worker_records(sys.executable, 'tokenizer_worker', {'texts': ['a']}, directory, timeout_seconds=5))
    finally:
        reading.subprocess.Popen = original
    return captured['env']


def _assert_no_secrets(env):
    leaked = [key for key in _SECRET_KEYS if key in env]
    assert leaked == []


class _Owner:
    """ProcessOwner 只在 Windows 上创建作业对象。环境回归不依赖那条监督路径。"""

    def __init__(self, process):
        self.process = process

    def attach_actual(self, pid, ppid):
        return None

    def close(self):
        return None


def _resident_env(tmp_path, monkeypatch):
    monkeypatch.setattr('dd_agent.model_runtime.ProcessOwner', _Owner)
    broker, spec = runtime(tmp_path)
    try:
        return list(broker.infer(spec, 'echo', {'action': 'env'}, timeout=3))[0]['env']
    finally:
        broker.close()


def test_worker_env_survives_windows_without_username_or_pwd(tmp_path, monkeypatch):
    from dd_agent.model_runtime import worker_process_env
    _hide_identity(monkeypatch)
    _plant_secrets(monkeypatch)
    oneshot = _oneshot_env(tmp_path / 'tokenizer')
    assert oneshot == worker_process_env(tmp_path / 'tokenizer')
    _assert_no_secrets(oneshot)
    assert not any(key.upper() in _IDENTITY_KEYS for key in oneshot)
    assert oneshot['HF_HUB_OFFLINE'] == '1' and oneshot['PYTHONUTF8'] == '1'
    assert 'TRANSFORMERS_OFFLINE' not in oneshot
    assert _pwdless(oneshot, 'cache') == oneshot['TORCHINDUCTOR_CACHE_DIR']
    resident = _resident_env(tmp_path / 'runtime', monkeypatch)
    # CPython 在缺少区域设置时会自己写入 LC_CTYPE，这不是父进程放进来的。
    assert {key: value for key, value in resident.items() if key != 'LC_CTYPE'} == worker_process_env(
        tmp_path / 'runtime', TRANSFORMERS_OFFLINE='1', DDA_WORKER_METRICS='0')
    _assert_no_secrets(resident)
    assert resident['TRANSFORMERS_OFFLINE'] == '1' and resident['HF_HUB_OFFLINE'] == '1'
    assert _pwdless(resident, 'cache') == resident['TORCHINDUCTOR_CACHE_DIR']


def test_worker_spawn_forwards_windows_username_without_secrets(tmp_path, monkeypatch):
    from dd_agent.model_runtime import worker_process_env
    _hide_identity(monkeypatch)
    _plant_secrets(monkeypatch)
    monkeypatch.setenv('USERNAME', 'alice')
    oneshot = _oneshot_env(tmp_path / 'tokenizer')
    assert oneshot['USERNAME'] == 'alice'
    assert 'USER' not in oneshot and 'LOGNAME' not in oneshot and 'LNAME' not in oneshot
    _assert_no_secrets(oneshot)
    assert _pwdless(oneshot, 'user') == 'alice'
    assert _pwdless(oneshot, 'cache') == oneshot['TORCHINDUCTOR_CACHE_DIR']
    resident = _resident_env(tmp_path / 'runtime', monkeypatch)
    assert {key: value for key, value in resident.items() if key != 'LC_CTYPE'} == worker_process_env(
        tmp_path / 'runtime', TRANSFORMERS_OFFLINE='1', DDA_WORKER_METRICS='0')
    assert resident['USERNAME'] == 'alice'
    _assert_no_secrets(resident)
    assert _pwdless(resident, 'user') == 'alice'


def test_process_owner_rejects_forged_parent_claim(tmp_path):
    import os
    import subprocess
    from dd_agent.process_owner import ProcessOwner
    process=subprocess.Popen([sys.executable,'-S','-c','import time; time.sleep(30)'])
    owner=ProcessOwner(process)
    outsider=subprocess.Popen([sys.executable,'-S','-c','import time; time.sleep(30)'])
    try:
        with pytest.raises(ValueError):owner.attach_actual(os.getpid(),process.pid)
        with pytest.raises(ValueError):owner.attach_actual(outsider.pid,process.pid)
    finally:
        owner.close();process.wait(timeout=5)
        if outsider.poll() is None:outsider.kill()
        outsider.wait(timeout=5)
