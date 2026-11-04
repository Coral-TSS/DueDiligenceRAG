"""Local-only PaddleOCR worker. Models must exist before starting inference."""
# 基础 OCR 层：PP-OCRv5 文字检测／识别＋方向判断，与困难区域使用的视觉大模型分开。
# 一次性入口按批加载；常驻入口复用 load_ocr 的同一个 Paddle 实例。
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dd_agent.performance import span, count


def emit(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)


def load_ocr(config):
    models=Path(config['models'])
    names=['PP-OCRv5_mobile_det','PP-OCRv5_server_rec','PP-LCNet_x1_0_doc_ori','PP-LCNet_x1_0_textline_ori']
    # 使用已下载的明确目录，缺权重时逐页返回错误，不在运行期间自动下载。
    if any(not (models/name/'inference.pdiparams').is_file() for name in names):
        raise FileNotFoundError('Local OCR weights missing')
    with redirect_stdout(sys.stderr), span('model.ocr.load'):
        # Paddle 自身日志转到 stderr，避免破坏主进程逐行读取的 JSON stdout 协议。
        from paddleocr import PaddleOCR
        ocr=PaddleOCR(
            text_detection_model_name=names[0],text_detection_model_dir=str(models/names[0]),
            text_recognition_model_name=names[1],text_recognition_model_dir=str(models/names[1]),
            doc_orientation_classify_model_name=names[2],doc_orientation_classify_model_dir=str(models/names[2]),
            textline_orientation_model_name=names[3],textline_orientation_model_dir=str(models/names[3]),
            use_doc_orientation_classify=True,use_doc_unwarping=False,use_textline_orientation=True,
            device='cpu',enable_mkldnn=False,cpu_threads=config.get('threads',4),text_recognition_batch_size=4)
        count('model.ocr.loads')
        # cpu_threads 是单实例内部线程数，不表示 4 个页面同时启动 4 份模型。
    return ocr


def recognize_pages(ocr,config):
    for page in config['pages']:
        try:
            count('ocr.page.attempts')
            with redirect_stdout(sys.stderr), span('ocr.inference'):
                results=list(ocr.predict(page['image_path']))
            result=results[0]
            payload=result.json
            if isinstance(payload,str):payload=json.loads(payload)
            output=payload.get('res',payload)
            # Recognition boxes refer to the preprocessed (orientation-corrected) image.
            processed=result.get('doc_preprocessor_res',{}).get('output_img')
            # 行框坐标对应方向校正后的图片，保存匹配图片供后续裁剪；PDF 原件不修改。
            if processed is not None:
                from PIL import Image
                Image.fromarray(processed[:,:,::-1]).save(page['image_path'])
            yield {'page_number':page['page_number'],'payload':output,'image_path':page['image_path']}
        except Exception:
            yield {'page_number':page['page_number'],'error_code':'ocr_page_failed'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('manifest')
    args=parser.parse_args()
    config=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    try:ocr=load_ocr(config)
    except Exception as exc:
        code='ocr_models_missing' if isinstance(exc,FileNotFoundError) else 'ocr_initialization_failed'
        for page in config['pages']:emit({'page_number':page['page_number'],'error_code':code})
        return 0
    for value in recognize_pages(ocr,config):emit(value)
    return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
