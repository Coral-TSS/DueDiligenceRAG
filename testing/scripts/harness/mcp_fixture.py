"""Synthetic stdio MCP endpoint for real Harness integration tests only."""
import json
from pathlib import Path
import sys


for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    with Path(sys.argv[1]).open('a',encoding='utf-8') as log:
        log.write(json.dumps(method)+'\n')
    if 'id' not in message:continue
    if method=='initialize':
        result={'protocolVersion':message['params']['protocolVersion'],
                'serverInfo':{'name':'dd-baseline-fixture','version':'1'},'capabilities':{'tools':{}}}
    elif method=='tools/list':
        result={'tools':[{'name':'baseline_status','description':'Read a synthetic offline baseline status.',
            'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]}
    elif method=='tools/call':
        result={'content':[{'type':'text','text':'offline_fixture'}]}
    elif method=='ping':result={}
    else:
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32601,'message':'Unknown method'}}),flush=True)
        continue
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)
