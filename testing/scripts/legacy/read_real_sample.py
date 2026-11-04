from pathlib import Path
import json
from dd_agent.config import Settings
from dd_agent.reading import Reader
root=Path(__file__).resolve().parents[3]
context=json.loads((root/'testing'/'records'/'sample-review'/'context.json').read_text(encoding='utf-8'))
result=Reader(Settings.from_env()).read(context['project_id'],context['document_id'],[1,2,3],force_ocr=True)
(root/'testing'/'records'/'sample-review'/'reading-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,default=str))
