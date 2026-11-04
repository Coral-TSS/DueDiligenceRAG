"""无权重 worker，覆盖真实进程与协议边界，绝不读取业务资料。"""
import json
import os
import sys
import time

nonce = sys.argv[-1]
def emit(value):
    print(json.dumps(value), flush=True)
emit(dict(event='hello', nonce=nonce, pid=os.getpid(), ppid=os.getppid()))
for line in sys.stdin:
    frame = json.loads(line)
    head = {k: frame[k] for k in ('request_id', 'runtime_generation', 'model_profile', 'operation')}
    if frame['operation'] == 'load':
        if frame['payload'].get('load_hang'):time.sleep(30)
        emit(dict(head, event='ready'))
        continue
    action = frame['payload'].get('action', 'echo')
    if action == 'hang': time.sleep(30)
    if action == 'crash': os._exit(9)
    if action == 'bad_id': head['request_id'] = 'old-id'
    if action == 'bad_generation':head['runtime_generation']='old-generation'
    if action == 'bad_profile':head['model_profile']='old-profile'
    if action == 'bad_operation':head['operation']='wrong-operation'
    if action == 'bad_json':
        print('broken', flush=True); continue
    emit(dict(head, event='record', value=dict(pid=os.getpid(), value=frame['payload'].get('value'))))
    if action == 'incomplete': os._exit(0)
    emit(dict(head, event='done'))
