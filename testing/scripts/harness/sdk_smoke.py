"""Exercise SDK, MCP and persistence against a localhost model fixture."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from deepseek_harness import DeepSeekHarness


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--endpoint',required=True);args=parser.parse_args()
    if urlsplit(args.endpoint).hostname!='127.0.0.1':raise ValueError('Only localhost fixture allowed')
    base=args.root.resolve();home=base/'home';workspace=base/'workspace';workspace.mkdir(parents=True)
    subdir=workspace/'saved';subdir.mkdir()
    project=Path(__file__).resolve().parents[3]
    node=project/'.local/harness/runtime/node-v22.19.0-win-x64'
    os.environ['PATH']=str(node)+os.pathsep+os.environ.get('PATH','')
    patch=base/'fixture.patch.yml'
    patch.write_text(json.dumps([{'insert':[{'id':'mcp-baseline','name':'@deepseek-ai/dsh-mcp-client',
        'config':{'serverName':'baseline','transport':'stdio','command':sys.executable,
                  'args':[str(Path(__file__).with_name('mcp_fixture.py')),str(base/'mcp-calls.jsonl')],
                  'cwd':str(base),'failOnStartupError':True,'toolCallTimeoutMs':15000}}]}]),encoding='utf-8')
    options=dict(profile='sdk-minimal',dsh_bin=str(project/'.local/harness/cli/node_modules/.bin/dsh.cmd'),
        dsh_home=str(home),cwd=str(workspace),
        patches=(str(project/'src/dd_agent/agent_profiles/dd-report.patch.yml'),str(patch)),
        provider='deepseek-official',model='deepseek-v4-flash',base_url=args.endpoint,
        api_key='offline-fixture',max_tokens=256,initialize_timeout_seconds=30,
        request_timeout_seconds=30,shutdown_timeout_seconds=5)
    session_id='dd-offline-baseline'
    with DeepSeekHarness(**options) as harness:
        session=harness.start_session(session_id)
        result=session.run('Call baseline_status once, then report the offline baseline result.')
        if result.final_response!='HARNESS_OFFLINE_OK':
            raise AssertionError({'response':result.final_response,'finish_reason':result.finish_reason,
                                  'terminal_events':[e for e in result.events if e.get('type')=='turn/end']})
        session.set_working_directory(str(subdir))
    with DeepSeekHarness(**options) as harness:
        session=harness.resume_session(session_id)
        restored=Path(session.get_working_directory()).resolve()==subdir
    print(json.dumps({'final_response':result.final_response,'finish_reason':result.finish_reason,
                      'restored':restored},ensure_ascii=False))


if __name__=='__main__':main()
