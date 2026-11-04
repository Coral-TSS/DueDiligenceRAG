import os
from pathlib import Path
root=Path(__file__).resolve().parents[3]
os.environ['PADDLE_PDX_CACHE_HOME']=str(root/'models'/'ocr')
os.environ['PADDLE_PDX_MODEL_SOURCE']='bos'
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
from paddlex.inference.utils.official_models import official_models
for name in ['PP-OCRv5_mobile_det','PP-OCRv5_server_rec','PP-LCNet_x1_0_doc_ori','PP-LCNet_x1_0_textline_ori']:
    print(name,official_models[name],flush=True)
