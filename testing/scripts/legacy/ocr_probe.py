from pathlib import Path
import os,json,time
root=Path(__file__).resolve().parents[3]
os.environ['PADDLE_PDX_CACHE_HOME']=str(root/'models'/'ocr')
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
os.environ['HF_HUB_OFFLINE']='1'
from paddleocr import PaddleOCR
models=root/'models'/'ocr'/'official_models'
start=time.monotonic()
ocr=PaddleOCR(
    text_detection_model_name='PP-OCRv5_mobile_det',text_detection_model_dir=str(models/'PP-OCRv5_mobile_det'),
    text_recognition_model_name='PP-OCRv5_server_rec',text_recognition_model_dir=str(models/'PP-OCRv5_server_rec'),
    doc_orientation_classify_model_name='PP-LCNet_x1_0_doc_ori',doc_orientation_classify_model_dir=str(models/'PP-LCNet_x1_0_doc_ori'),
    textline_orientation_model_name='PP-LCNet_x1_0_textline_ori',textline_orientation_model_dir=str(models/'PP-LCNet_x1_0_textline_ori'),
    use_doc_orientation_classify=True,use_doc_unwarping=False,use_textline_orientation=True,
    device='cpu',enable_mkldnn=False,cpu_threads=4,text_recognition_batch_size=4,
)
for page in [1,2,3]:
    t=time.monotonic()
    result=list(ocr.predict(str(root/'testing'/'records'/'sample-review'/f'pdf-page-{page}.png')))[0]
    payload=result.json
    if isinstance(payload,str):payload=json.loads(payload)
    (root/'testing'/'records'/'sample-review'/f'ocr-page-{page}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    value=payload.get('res',payload)
    print(json.dumps({'page':page,'seconds':round(time.monotonic()-t,2),'lines':value.get('rec_texts',[]),'scores':value.get('rec_scores',[])},ensure_ascii=False),flush=True)
print('total seconds',time.monotonic()-start,flush=True)
