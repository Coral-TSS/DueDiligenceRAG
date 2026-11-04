"""Serve real reader assets with synthetic progress, no DB/models/customer files."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path


ROOT=Path(__file__).resolve().parents[3]/'src/dd_agent/static'
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=self.path.split('?')[0]
        if path=='/reader':data=(ROOT/'reader.html').read_bytes();kind='text/html'
        elif path.startswith('/static/'):
            file=ROOT/Path(path).name
            if file.suffix not in {'.js','.css'}:self.send_error(404);return
            data=file.read_bytes();kind='text/javascript' if file.suffix=='.js' else 'text/css'
        else:
            if path.endswith('/reading'):
                result=dict(filenames=['合成恢复验收.pdf'],successful_pages=20,page_count=649,failed_pages=0)
            elif path.endswith('/pages'):result=[]
            elif path.endswith('/processing'):
                result=dict(status='running',execution_state='interrupted',worker_present=False,
                    progress=dict(processed_pages=15,total_pages=649,current_page=1,stage='reading'))
            else:self.send_error(404);return
            data=json.dumps(result).encode();kind='application/json'
        self.send_response(200);self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    print(f'http://127.0.0.1:{server.server_port}/reader?project=fixture&document=fixture',flush=True)
    server.serve_forever()
