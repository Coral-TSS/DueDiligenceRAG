"""在已有 PDF／Pillow 环境中，把独立 JPEG／PNG 收成 OCR 使用的 PNG。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dd_agent.standalone_images import StandaloneImageError, materialize_standalone_png


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest')
    args = parser.parse_args()
    config = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    target = Path(config['output_dir']) / (config.get('image_pattern') or 'page-{number}.png').format(number=1)
    try:
        path = materialize_standalone_png(
            config['source'], target,
            max_pixels=int(config.get('max_pixels') or 0),
            max_file_bytes=int(config.get('max_file_bytes') or 0),
        )
        emit({'page_number': 1, 'route': 'ocr', 'text': '', 'image_path': str(path), 'page_count': 1})
    except StandaloneImageError as exc:
        emit({'page_number': 1, 'error_code': exc.error_code})
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
