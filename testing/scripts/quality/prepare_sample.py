from pathlib import Path
import json
import pypdfium2 as pdfium
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[3]
out = root / 'testing' / 'records' / 'quality'
pages = [1, 2, 3, 4, 5, 40, 80, 120, 160, 200, 240, 280, 320, 360, 400, 440, 480, 520, 600, 649]
pdf = pdfium.PdfDocument(root / '01一-1，一-2工商档案.pdf')
sheet = Image.new('RGB', (1400, 2450), 'white')
draw = ImageDraw.Draw(sheet)
manifest = {'status':'candidate_pages_not_yet_stratified', 'page_count':len(pdf), 'pages':[],
            'note':'Source images only. Split/groups and field references require visual review; no report answers used.'}
for index, number in enumerate(pages):
    page = pdf[number - 1]
    bitmap = page.render(scale=3)
    image = bitmap.to_pil().copy()
    path = out / f'page-{number}.png'
    image.save(path)
    thumb = image.copy(); thumb.thumbnail((330, 455))
    x, y = (index % 4)*350, (index // 4)*490
    sheet.paste(thumb, (x, y+25)); draw.text((x+5, y+5), f'PDF page {number}', fill='black')
    manifest['pages'].append({'page_number':number, 'image':path.name, 'size':list(image.size), 'categories':[], 'group':None, 'split':None, 'fields':[]})
    page.close(); bitmap.close()
    print(number, flush=True)
pdf.close()
sheet.save(out / 'contact-sheet.jpg')
(out / 'sample-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
