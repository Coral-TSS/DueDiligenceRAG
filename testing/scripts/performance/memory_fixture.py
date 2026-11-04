"""Windows venv may start a redirector; report the interpreter's actual PID."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]/'src'))
from dd_agent.performance import _memory

print(json.dumps({'pid': os.getpid()}), flush=True)
allocation = bytearray(128*1024*1024)
print(json.dumps({'rss_mb': _memory(os.getpid())[0]}), flush=True)
time.sleep(.5)
