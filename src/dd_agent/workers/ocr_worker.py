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
        from dd_agent.devices import paddle_runtime_kwargs, resolve_paddle
        from paddleocr import PaddleOCR
        # oneDNN 默认关闭：性能基线如此，且 Windows 上 Paddle 3 的 oneDNN 曾出现原语执行失败。
        # 只有 DDA_OCR_ENABLE_MKLDNN=1 且实际走 CPU 时才打开。GPU 不传 cpu_threads／enable_mkldnn。
        runtime=resolve_paddle(config)
        ocr=PaddleOCR(
            text_detection_model_name=names[0],text_detection_model_dir=str(models/names[0]),
            text_recognition_model_name=names[1],text_recognition_model_dir=str(models/names[1]),
            doc_orientation_classify_model_name=names[2],doc_orientation_classify_model_dir=str(models/names[2]),
            textline_orientation_model_name=names[3],textline_orientation_model_dir=str(models/names[3]),
            use_doc_orientation_classify=True,use_doc_unwarping=False,use_textline_orientation=True,
            text_recognition_batch_size=int(config.get('rec_batch') or 16), **paddle_runtime_kwargs(runtime))
        count('model.ocr.loads')
        # cpu_threads 是单实例内部线程数，不表示 4 个页面同时启动 4 份模型。
    return ocr


def _page_number(page):
    return page.get('page_number') if type(page.get('page_number')) is int else None


def _decode(page, result):
    payload=result.json
    if isinstance(payload,str):payload=json.loads(payload)
    output=payload.get('res',payload)
    # 行框坐标对应方向校正后的图片，保存匹配图片供后续裁剪；PDF 原件不修改。
    processed=result.get('doc_preprocessor_res',{}).get('output_img')
    if processed is not None:
        from PIL import Image
        Image.fromarray(processed[:,:,::-1]).save(page['image_path'])
    return {'page_number':page['page_number'],'payload':output,'image_path':page['image_path']}


def _one(ocr, page):
    try:
        count('ocr.page.attempts')
        with redirect_stdout(sys.stderr), span('ocr.inference', page=_page_number(page)):
            results=list(ocr.predict(page['image_path']))
        return _decode(page, results[0])
    except Exception:
        return {'page_number':page['page_number'],'error_code':'ocr_page_failed'}


def recognize_pages(ocr,config):
    pages=list(config['pages'])
    batch=int(config.get('page_batch') or 1)
    if batch<=1 or len(pages)<=1:
        for page in pages:
            yield _one(ocr, page)
        return
    # PaddleOCR 的 predict 可以接收路径列表。不支持或条数对不上时退回逐页，不是可配置的旧模式。
    for start in range(0, len(pages), batch):
        group=pages[start:start+batch]
        try:
            count('ocr.page.attempts', len(group))
            with redirect_stdout(sys.stderr), span('ocr.inference', page=_page_number(group[0])):
                results=list(ocr.predict([page['image_path'] for page in group]))
            if len(results)!=len(group):
                raise ValueError('batch')
            for page, result in zip(group, results):
                yield _decode(page, result)
            count('ocr.page.batched', len(group))
        except Exception:
            count('ocr.page.batch_fallback')
            for page in group:
                yield _one(ocr, page)


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
