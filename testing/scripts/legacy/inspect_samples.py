from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET
import json

root = Path(__file__).resolve().parents[3]
out = root / 'testing' / 'records' / 'sample-review'
out.mkdir(exist_ok=True)
ns = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
source = next(root.glob('*.docx'))
with ZipFile(source) as z:
    body = ET.fromstring(z.read('word/document.xml'))
    paragraphs = []
    for p in body.findall('.//w:p', ns):
        text = ''.join(p.itertext()) if False else ''.join(t.text or '' for t in p.findall('.//w:t', ns))
        if text.strip():
            style = p.find('./w:pPr/w:pStyle', ns)
            paragraphs.append({'style':style.get('{'+ns['w']+'}val') if style is not None else '', 'text':text})
    tables = len(body.findall('.//w:tbl', ns))
    props = z.read('docProps/app.xml').decode('utf-8') if 'docProps/app.xml' in z.namelist() else ''
report = {'filename':source.name,'paragraph_count':len(paragraphs),'tables':tables,'paragraphs':paragraphs}
(out/'report-structure.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':source.name,'paragraphs':len(paragraphs),'tables':tables,'headings': [p for p in paragraphs if p['style'] or ('工商' in p['text'] or '历史沿革' in p['text'] or '基本情况' in p['text'])][:75]}, ensure_ascii=False))

import pypdfium2 as pdfium
pdf = pdfium.PdfDocument(next(root.glob('*.pdf')))
count = len(pdf)
indices = sorted(set([0,1,2,3,4,count-1] + [int((count-1)*i/8) for i in range(9)]))
samples=[]
for i in indices:
    page=pdf[i]
    tp=page.get_textpage()
    text=tp.get_text_range()
    samples.append({'page':i+1,'chars':len(text.strip()),'preview':text[:180]})
    tp.close()
    if i in (0,1,2):
        page.render(scale=1.3).to_pil().save(out/f'pdf-page-{i+1}.png')
    page.close()
pdf.close()
profile={'pages':count,'sampled_pages':samples}
(out/'pdf-profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(profile,ensure_ascii=False))
