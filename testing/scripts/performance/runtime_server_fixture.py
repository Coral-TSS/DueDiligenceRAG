import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'src'))
from dd_agent.worker_protocol import serve
from dd_agent.performance import count

def load(config):
    count('fixture.loads')
    return config

def infer(engine, operation, payload):
    count('fixture.requests')
    yield {'pid':os.getpid(), 'value':payload.get('value')}

serve(load,infer,sys.argv[-1])
