"""Small real-material retrieval smoke; no automatic/manual fact confirmation."""
import json
from pathlib import Path
from dd_agent.config import Settings
from dd_agent.retrieval import EvidenceService
from dd_agent.review import ReviewService

root = Path(__file__).resolve().parents[3]
context = json.loads((root/'testing/records/sample-review/context.json').read_text(encoding='utf-8'))
settings = Settings.from_env()
service = EvidenceService(settings)
p, d = context['project_id'], context['document_id']
indexed = service.index(p, d, [1,2,3])
print(json.dumps({'indexed':indexed},ensure_ascii=False),flush=True)
again = service.index(p, d, [1,2,3])
found = service.search(p, '企业名称预先核准申请书中的注册资本是多少？')
assert indexed['failed_pages'] == [], indexed
assert again['skipped_pages'] == [1,2,3], again
assert any(hit['page_number']==3 for hit in found['results']), found
assert all(hit['verification']=='unverified' for hit in found['results'])
state = ReviewService(settings).state(p,d,3)
assert state['adoption'] is None
result = {'status':'three_page_engineering_pilot_only','first_index':indexed,'repeat_index':again,'query':found,
          'visual_candidates':state['candidates'], 'note':'Raw OCR contains known errors; visual candidates remain unadopted. Not full-volume or field-accuracy acceptance.'}
(root/'testing/records/quality/real-pilot.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps({'coverage':found['coverage'],'result_pages':[hit['page_number'] for hit in found['results']]},ensure_ascii=False),flush=True)
