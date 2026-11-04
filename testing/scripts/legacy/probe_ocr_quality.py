"""Read-only OCR quality experiment; does not write application page records."""
import os,json,time
from pathlib import Path
root=Path(__file__).resolve().parents[3]
out=root/'testing'/'records'/'ocr-quality-review';out.mkdir(exist_ok=True)
os.environ['PADDLE_PDX_CACHE_HOME']=str(root/'models'/'ocr')
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
os.environ['HF_HUB_OFFLINE']='1'
import pypdfium2 as pdfium
pdf=pdfium.PdfDocument(root/'01一-1，一-2工商档案.pdf')
page=pdf[2]
image=page.render(scale=2331/842).to_pil()
width,height=image.size
regions={'name':(.30,.14,.94,.195),'address':(.30,.53,.94,.585),'dates':(.34,.75,.94,.805)}
for name,bounds in regions.items():
    image.crop(tuple(int(value*(width if i%2==0 else height)) for i,value in enumerate(bounds))).save(out/f'{name}.png')
page.close();pdf.close()
from paddleocr import PaddleOCR
models=root/'models'/'ocr'/'official_models'
ocr=PaddleOCR(
    text_detection_model_name='PP-OCRv5_mobile_det',text_detection_model_dir=str(models/'PP-OCRv5_mobile_det'),
    text_recognition_model_name='PP-OCRv5_server_rec',text_recognition_model_dir=str(models/'PP-OCRv5_server_rec'),
    use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False,
    device='cpu',enable_mkldnn=False,cpu_threads=4,text_recognition_batch_size=4)
results={}
for name in regions:
    start=time.monotonic()
    result=list(ocr.predict(str(out/f'{name}.png')))[0].json
    if isinstance(result,str):result=json.loads(result)
    value=result.get('res',result)
    results[name]={'texts':value.get('rec_texts',[]),'scores':value.get('rec_scores',[]),'seconds':round(time.monotonic()-start,2)}
    print(json.dumps({name:results[name]},ensure_ascii=False),flush=True)
(out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
