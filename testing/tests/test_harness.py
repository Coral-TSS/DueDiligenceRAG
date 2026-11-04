"""Real Harness runtime; model protocol uses a localhost synthetic endpoint."""
import json
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from dd_agent.config import ROOT, Settings


def clean_env():
    env={k:v for k,v in os.environ.items() if k.upper() in {
        'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','COMSPEC'}}
    return {**env,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','PYTHONPATH':str(ROOT/'src')}


def test_harness_check_cli_does_not_need_database_or_model_credential(tmp_path):
    from dd_agent import agent_runtime
    settings=Settings(data_dir=tmp_path/'data',harness_home=tmp_path/'home',harness_workspace=tmp_path/'workspace')
    state=agent_runtime.check_harness(settings)
    assert state['status']=='ready' and state['model_requests']==0
    assert state['sdk_version']=='0.2.1rc2'
    assert state['runtime_version']=='0.2.1-rc.2'
    env=clean_env()
    env.update(DDA_DATABASE_URL='host=invalid.invalid',DDA_HARNESS_HOME=str(tmp_path/'cli-home'),
               DDA_HARNESS_WORKSPACE=str(tmp_path/'cli-workspace'),DDA_DATA_DIR=str(tmp_path/'cli-data'))
    result=subprocess.run([sys.executable,'-m','dd_agent','harness-check'],env=env,
                          capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['status']=='ready'


def test_missing_harness_environment_has_actionable_error(tmp_path):
    from dd_agent.agent_runtime import check_harness, HarnessError
    with pytest.raises(HarnessError,match='Harness'):
        check_harness(Settings(data_dir=tmp_path,harness_python=tmp_path/'missing.exe'))


def test_report_profile_discovers_only_configured_mcp_and_restores_session(tmp_path):
    # Using the wrong profile would expose shell tools or upload session logs.
    # The actual outgoing localhost request catches that, not YAML string checks.
    assert (ROOT/'src/dd_agent/agent_profiles/dd-report.patch.yml').is_file(), 'Report Harness profile missing'
    requests=[]
    class Model(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            if len(requests)==1:
                block={'type':'tool_use','id':'call-baseline',
                       'name':'mcp__baseline__baseline_status','input':{}}
                finish='tool_use'
            else:block={'type':'text','text':'HARNESS_OFFLINE_OK'};finish='end_turn'
            chunks=[{'type':'message_start','message':{'id':'offline-baseline','role':'assistant',
                        'content':[],'usage':{'input_tokens':1,'output_tokens':0}}},
                    {'type':'content_block_start','index':0,'content_block':block},
                    {'type':'content_block_stop','index':0},
                    {'type':'message_delta','delta':{'stop_reason':finish},'usage':{'output_tokens':1}},
                    {'type':'message_stop'}]
            payload=''.join('event: '+x['type']+'\ndata: '+json.dumps(x)+'\n\n' for x in chunks).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Model)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        result=subprocess.run([str(ROOT/'.venv-harness/Scripts/python.exe'),
            str(ROOT/'testing/scripts/harness/sdk_smoke.py'),'--root',str(tmp_path),
            '--endpoint',f'http://127.0.0.1:{server.server_port}/v1'],
            env=clean_env(),capture_output=True,text=True,encoding='utf-8',timeout=90)
        assert result.returncode==0,result.stdout+result.stderr
        state=json.loads(result.stdout)
        assert state['final_response']=='HARNESS_OFFLINE_OK' and state['restored']
        assert len(requests)==2, 'Restoration must not make additional model requests'
        names=[x['name'] for x in requests[0]['tools']]
        assert sorted(names)==sorted(['mcp__baseline__baseline_status','list_mcp_resources',
                                     'list_mcp_resource_templates','read_mcp_resource'])
        assert any(b.get('type')=='tool_result' and 'offline_fixture' in json.dumps(b)
                   for m in requests[1]['messages'] for b in m.get('content',[]) if isinstance(b,dict))
        assert all('dsh_session_log' not in r for r in requests)
        calls=[json.loads(s) for s in (tmp_path/'mcp-calls.jsonl').read_text().splitlines()]
        assert 'tools/call' in calls
    finally:
        server.shutdown();server.server_close();thread.join(5)
