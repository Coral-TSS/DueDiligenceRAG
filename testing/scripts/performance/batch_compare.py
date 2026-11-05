"""对照几组 OCR 批大小和向量 token 预算。不改 .env，只在这一次进程里计时。

每次计时都包含一次模型加载，所以用来比设置之间的差别，不要和 process-pages 的整段耗时直接相减。
没有对应虚拟环境或权重时会说明原因并退出，不假装跑过。

在工程根目录：

    .\\.venv-dev\\Scripts\\python.exe testing/scripts/performance/batch_compare.py --pdf D:\\scan.pdf --pages 4
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))

from dd_agent.config import Settings
from dd_agent.reading import worker_records


def _positive_ints(text):
    values = tuple(int(item) for item in text.split(',') if item.strip())
    if not values or any(item < 1 for item in values):
        raise argparse.ArgumentTypeError('需要逗号分隔的正整数')
    return values


def _render(settings, pdf, pages, directory):
    manifest = dict(mode='read', source=str(pdf), pages=list(pages), output_dir=str(directory), render_scale=settings.pdf_render_scale)
    rows = list(worker_records(settings.pdf_python, 'pdf_worker', manifest, directory))
    images = []
    for row in rows:
        if row.get('error_code') or not row.get('image_path'):
            raise SystemExit('渲染失败：' + json.dumps(row, ensure_ascii=False))
        images.append(row)
    return images


def _ocr(settings, images, directory, rec_batch, page_batch):
    manifest = dict(models=str(settings.ocr_models_dir.resolve()), pages=images, device=settings.ocr_device,
                    rec_batch=rec_batch, page_batch=page_batch, threads=settings.ocr_cpu_threads or settings.model_cpu_threads,
                    enable_mkldnn=False)
    started = perf_counter()
    rows = list(worker_records(settings.ocr_python, 'ocr_worker', manifest, directory))
    elapsed = perf_counter() - started
    lines = 0
    chars = 0
    texts = []
    for row in rows:
        payload = row.get('payload') or {}
        page_texts = [text for text in (payload.get('rec_texts') or []) if str(text).strip()]
        lines += len(page_texts)
        chars += sum(len(str(text)) for text in page_texts)
        texts.extend(str(text) for text in page_texts)
    return elapsed, lines, chars, texts, any(row.get('error_code') for row in rows)


def _embed(settings, texts, directory, budget):
    manifest = dict(mode='embed', embedding=str(settings.embedding_dir.resolve()), texts=texts,
                    device=settings.embedding_device, dtype=settings.embedding_dtype,
                    threads=settings.model_cpu_threads, token_budget=budget, batch_limit=settings.embed_batch_limit)
    started = perf_counter()
    rows = list(worker_records(settings.retrieval_python, 'retrieval_worker', manifest, directory))
    return perf_counter() - started, rows


def main():
    parser = argparse.ArgumentParser(description='对照 OCR 批大小和向量 token 预算')
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--pages', type=int, default=4, help='从第 1 页起连续取这么多页')
    parser.add_argument('--rec-batch', type=_positive_ints, default=(4, 8, 16))
    parser.add_argument('--page-batch', type=_positive_ints, default=(1, 2, 4))
    parser.add_argument('--embed-budget', type=_positive_ints, default=(1024, 2048, 4096))
    args = parser.parse_args()
    if not args.pdf.is_file() or args.pages < 1:
        raise SystemExit('需要一个存在的 PDF，以及至少 1 页')
    settings = Settings.from_env()
    if not settings.pdf_python.is_file() or not settings.ocr_python.is_file():
        raise SystemExit('找不到 PDF 或 OCR 的虚拟环境，请先按 GPU 安装说明配好 .venv-docling 和 .venv-ocr')
    pages = list(range(1, args.pages + 1))
    with tempfile.TemporaryDirectory(prefix='dda-batch-') as directory:
        folder = Path(directory)
        images = _render(settings, args.pdf.resolve(), pages, folder)
        print(f'渲染 {len(images)} 页，接下来每格都含一次模型加载。')
        print('rec_batch page_batch seconds lines chars error')
        collected = []
        for rec_batch in args.rec_batch:
            for page_batch in args.page_batch:
                elapsed, lines, chars, texts, failed = _ocr(settings, images, folder, rec_batch, page_batch)
                print(f'{rec_batch:9} {page_batch:10} {elapsed:7.2f} {lines:5} {chars:5} {failed}')
                if texts and not collected:
                    collected = texts
        if not settings.retrieval_python.is_file():
            print('找不到检索虚拟环境，跳过向量预算对照。')
            return 0
        if not collected:
            print('OCR 没有文字，跳过向量预算对照。')
            return 0
        print('embed_budget seconds passages')
        for budget in args.embed_budget:
            elapsed, rows = _embed(settings, collected, folder, budget)
            vectors = rows[0].get('vectors') if rows and isinstance(rows[0], dict) else None
            print(f'{budget:12} {elapsed:7.2f} {len(collected) if vectors else 0}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
