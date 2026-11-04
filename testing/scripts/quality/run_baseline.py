"""Read-only baseline; copies previews because OCR corrects orientation in place."""
import json
from pathlib import Path
import shutil
import time
import argparse

from dd_agent.config import Settings
from dd_agent.reading import worker_records, normalize_ocr

root = Path(__file__).resolve().parents[3]
out = root/'testing/records/quality'
working = out/'baseline-images'; working.mkdir(exist_ok=True)
plan = json.loads((out/'sample-manifest.json').read_text(encoding='utf-8'))
parser=argparse.ArgumentParser();parser.add_argument('--limit',type=int,default=5);args=parser.parse_args()
if not 1<=args.limit<=5:raise ValueError('This quality experiment uses batches of 1-5 pages')
result_path=out/'baseline-20.jsonl'
done={row['page_number'] for row in (json.loads(line) for line in result_path.read_text(encoding='utf-8').splitlines()) if not row.get('error_code')} if result_path.exists() else set()
pages = []
for page in plan['pages']:
    if page['page_number'] in done or len(pages)>=args.limit:continue
    target = working/page['image']
    shutil.copyfile(out/page['image'], target)
    pages.append({'page_number':page['page_number'], 'image_path':str(target)})
start = previous = time.monotonic()
with result_path.open('a',encoding='utf-8') as stream:
    for record in worker_records(Settings().ocr_python, 'ocr_worker', {'models':str(Settings().ocr_models_dir), 'pages':pages}, out/'baseline-worker'):
        now=time.monotonic()
        if 'payload' in record:
            record.update(normalize_ocr(record.pop('payload')))
        record['since_previous_output_seconds']=round(now-previous,3)
        stream.write(json.dumps(record,ensure_ascii=False)+'\n'); stream.flush()
        print(json.dumps({'page':record['page_number'],'characters':len(record.get('text','')),'error':record.get('error_code'),'since_previous_output_seconds':record['since_previous_output_seconds']}),flush=True)
        previous=now
(out/f'baseline-batch-{int(time.time())}.json').write_text(json.dumps({'pages':len(pages),'total_seconds':round(time.monotonic()-start,3),'render_scale':3,'cpu_threads':4,'note':'Includes startup and orchestration; not isolated performance benchmark.'},indent=2),encoding='utf-8')
