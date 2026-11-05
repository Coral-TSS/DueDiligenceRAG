"""Executed by the dedicated PDF runtime. The protocol on stdout is JSONL."""
# 实际解析引擎是 PDFium；本进程负责取文字／生成预览，不做 PaddleOCR 或向量化。
import argparse
import json
import os
from pathlib import Path
import re
import sys

# worker 直接按脚本启动；仅引入无第三方依赖的性能模块。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dd_agent.performance import span, timed, count


@timed('pdf.preflight')
def inspect_pages(pdf):
    pages = []
    for i in range(len(pdf)):
        page = pdf[i]
        try:
            tp = page.get_textpage()
            try: text = tp.get_text_range()
            finally: tp.close()
            pages.append({'page_number':i+1,'characters':len(text.strip()),'route':text_route(text)})
            count('pdf.preflight.pages')
        finally: page.close()
    return {'page_count':len(pdf),'pages':pages}


def text_route(text):
    # 简单文字层可读性规则：长度、替代字符、异常字符占比及中英文组成。
    # 它不能保证没有漏字或混合图片；这些是后续路由和覆盖评测需要补强的部分。
    compact=re.sub(r'\s','',text)
    if len(compact)<16 or '\ufffd' in compact:
        return 'ocr'
    unusual=re.sub(r'[\u4e00-\u9fffA-Za-z0-9，。；：、“”‘’（）《》【】—–·％￥\x21-\x7e]','',compact)
    if len(unusual)/len(compact)>0.02:
        return 'ocr'
    chinese=len(re.findall(r'[\u4e00-\u9fff]',compact))
    latin=len(re.findall(r'[a-zA-Z0-9]',compact))
    return 'text' if chinese>=10 or latin/len(compact)>0.85 else 'ocr'


def emit(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)


def preview_paths(directory, pattern, number):
    """最终图和临时图都用 .png 结尾，Pillow 才能按 PNG 保存。"""
    target = Path(directory) / pattern.format(number=number)
    return target, target.with_name(target.stem + '.partial.png')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('manifest')
    args=parser.parse_args()
    config=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    import pypdfium2 as pdfium
    try:
        pdf=pdfium.PdfDocument(config['source'])
    except Exception:
        emit({'error_code':'invalid_or_encrypted_pdf'})
        return 1
    try:
        if config['mode']=='inspect':
            # 预检遍历所有物理页，只返回页数／文字数量／路由，不输出所有正文。
            emit(inspect_pages(pdf))
        else:
            for number in config['pages']:
                # 外部物理页码从 1 开始，PDFium 数组下标从 0 开始。
                page=None
                try:
                    page=pdf[number-1]
                    with span('pdf.text', page=number):
                        tp=page.get_textpage()
                        try:text=tp.get_text_range()
                        finally:tp.close()
                    pattern=config.get('image_pattern') or 'page-{number}.png'
                    target, partial = preview_paths(config['output_dir'], pattern, number)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 临时名必须仍以 .png 结尾。Pillow 靠扩展名选格式，.png.part 会让保存失败，
                    # 整页被记成 pdf_page_failed，后面的 OCR 和 image_key 都不会发生。
                    # 可读文字页也保留预览图，供回看；渲染图片本身不表示调用视觉识别。
                    # 先写临时文件再替换，避免 OCR 重试读到半张图。
                    with span('pdf.render', page=number):
                        page.render(scale=config.get('render_scale',2)).to_pil().save(partial, format='PNG')
                        os.replace(partial, target)
                    count('pdf.render.pages')
                    emit({'page_number':number,'route':text_route(text),'text':text,'image_path':str(target)})
                except Exception:
                    emit({'page_number':number,'error_code':'pdf_page_failed'})
                finally:
                    if page is not None:page.close()
    finally:pdf.close()
    return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
