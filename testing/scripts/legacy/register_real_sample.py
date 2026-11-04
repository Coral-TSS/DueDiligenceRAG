from pathlib import Path
import json
from dd_agent.config import Settings
from dd_agent.db import Database
from dd_agent.storage import Storage
root=Path(__file__).resolve().parents[3]
settings=Settings.from_env();db=Database(settings.database_url)
name='吉林津升 · 工商档案验收'
project=next((p for p in db.projects() if p['name']==name),None) or db.create_project(name)
source=root/'01一-1，一-2工商档案.pdf'
with source.open('rb') as stream:
    stored=Storage(settings.data_dir,settings.max_upload_bytes).save(project['id'],source.name,stream)
document,duplicate=db.register(project['id'],stored)
context={'project_id':str(project['id']),'document_id':str(document['id']),'reference_report':'【吉林津升】法律尽调报告-202060702.docx'}
(root/'testing'/'records'/'sample-review'/'context.json').write_text(json.dumps(context,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'project_created_or_reused':True,'pdf_registered':True,'duplicate':duplicate,'byte_size':document['byte_size']},ensure_ascii=False))
