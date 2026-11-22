"""从 DOCX 压缩包取出嵌入图片，并用现有 OCR 运行时识别文字。

正文里的嵌入图、浮动图、表格里的图，以及页眉页脚里的图都算。
不把「此处有图、未提取」写成已成功的正文。Excel 不在这里处理。
"""
from io import BytesIO
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

IMAGE_PLACEHOLDER = '此处有图、未提取'
_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_EMBED = f'{{{_R_NS}}}embed'
_LINK = f'{{{_R_NS}}}link'
_RID = f'{{{_R_NS}}}id'


def _local(tag):
    if not isinstance(tag, str):
        return ''
    if tag.startswith('{'):
        return tag.rsplit('}', 1)[-1]
    return tag


def _part_key(name):
    base = name.rsplit('/', 1)[-1]
    if base == 'document.xml':
        return 'document'
    if base.endswith('.xml'):
        return base[:-4]
    return base


def _story_parts(names):
    selected = []
    for name in names:
        if not name.startswith('word/') or name.count('/') != 1 or not name.endswith('.xml'):
            continue
        base = name.rsplit('/', 1)[-1]
        if base == 'document.xml' or base.startswith(('header', 'footer')):
            selected.append(name)

    def sort_key(name):
        base = name.rsplit('/', 1)[-1]
        group = 0 if base == 'document.xml' else 1 if base.startswith('header') else 2
        return group, base

    return sorted(selected, key=sort_key)


def _relationships(package, part_name):
    folder, _, file_name = part_name.rpartition('/')
    rels_name = f'{folder}/_rels/{file_name}.rels' if folder else f'_rels/{file_name}.rels'
    try:
        root = ET.fromstring(package.read(rels_name))
    except KeyError:
        return {}
    except ET.ParseError:
        return {}
    found = {}
    for rel in root:
        if _local(rel.tag) != 'Relationship':
            continue
        rid = rel.get('Id')
        if not rid:
            continue
        found[rid] = {
            'target': (rel.get('Target') or '').replace('\\', '/'),
            'external': (rel.get('TargetMode') or '').lower() == 'external',
        }
    return found


def _resolve(part_name, target):
    target = (target or '').split('#', 1)[0].replace('\\', '/')
    if not target:
        return ''
    if target.startswith('/'):
        return target.lstrip('/')
    folder = part_name.rsplit('/', 1)[0] if '/' in part_name else ''
    parts = [piece for piece in folder.split('/') if piece] if folder else []
    for piece in target.split('/'):
        if piece in ('', '.'):
            continue
        if piece == '..':
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return '/'.join(parts)


def _suffix(name, data):
    lower = (name or '').lower()
    for ext in ('.png', '.jpeg', '.jpg', '.gif', '.tif', '.tiff', '.bmp', '.webp', '.emf', '.wmf'):
        if lower.endswith(ext):
            return '.jpg' if ext == '.jpeg' else ext
    data = data or b''
    if data.startswith(b'\x89PNG'):
        return '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith(b'GIF8'):
        return '.gif'
    if data.startswith(b'BM'):
        return '.bmp'
    return '.img'


def _walk(element, found):
    local = _local(element.tag)
    if local == 'AlternateContent':
        choices = [child for child in list(element) if _local(child.tag) == 'Choice']
        chosen = choices or [child for child in list(element) if _local(child.tag) == 'Fallback']
        for child in chosen:
            _walk(child, found)
        return
    if local == 'drawing':
        blips = [node for node in element.iter() if _local(node.tag) == 'blip']
        if blips:
            placement = 'anchor' if any(_local(node.tag) == 'anchor' for node in element.iter()) else 'inline'
            for blip in blips:
                found.append((placement, blip.get(_EMBED) or blip.get(_LINK) or '', bool(blip.get(_EMBED))))
        return
    if local == 'imagedata':
        found.append(('vml', element.get(_RID) or '', True))
        return
    for child in list(element):
        _walk(child, found)


def _read_target(package, part_name, rels, rid, embedded):
    if not rid or rid not in rels:
        return None, '', 'image_extract_failed'
    rel = rels[rid]
    if rel['external'] or not embedded:
        return None, '', 'image_extract_failed'
    resolved = _resolve(part_name, rel['target'])
    if not resolved:
        return None, '', 'image_extract_failed'
    try:
        data = package.read(resolved)
    except KeyError:
        return None, resolved, 'image_extract_failed'
    if not data:
        return None, resolved, 'image_extract_failed'
    return data, resolved, ''


def _part_from_rels(rels_name):
    folder, _, file_name = rels_name.rpartition('/')
    parent = folder.rsplit('/_rels', 1)[0] if folder.endswith('_rels') else ''
    part_file = file_name[:-5] if file_name.endswith('.rels') else file_name
    return f'{parent}/{part_file}' if parent else part_file


def blob_with_missing_targets(blob):
    """补上关系里点名、压缩包里却没有的部件，让正文解析不会被缺图整份打断。

    补进去的是空字节，只用于读取段落和表格。图片识别仍看原件，空字节不会被当成已提取。
    """
    try:
        package = zipfile.ZipFile(BytesIO(blob))
    except zipfile.BadZipFile:
        return blob
    with package:
        names = {item.replace('\\', '/') for item in package.namelist()}
        missing = []
        for name in sorted(names):
            if not (name.startswith('word/_rels/') and name.endswith('.rels')):
                continue
            rels = _relationships(package, _part_from_rels(name))
            for rel in rels.values():
                if rel['external']:
                    continue
                resolved = _resolve(_part_from_rels(name), rel['target'])
                if resolved and resolved not in names and resolved not in missing:
                    missing.append(resolved)
        if not missing or len(missing) > 32:
            return blob
        stored = {item: package.read(item) for item in package.namelist()}
    repaired = BytesIO()
    with zipfile.ZipFile(repaired, 'w') as target:
        for name, data in stored.items():
            target.writestr(name, data)
        for name in missing:
            target.writestr(name, b'')
    return repaired.getvalue()


def extract_docx_images(blob):
    """按文档顺序返回图片。提不出字节的图片也保留，并带上失败原因。"""
    try:
        package = zipfile.ZipFile(BytesIO(blob))
    except zipfile.BadZipFile:
        return []
    images = []
    with package:
        names = [item.replace('\\', '/') for item in package.namelist()]
        for part_name in _story_parts(names):
            try:
                root = ET.fromstring(package.read(part_name))
            except (KeyError, ET.ParseError):
                images.append({
                    'locator': {'kind': 'docx_image', 'part': _part_key(part_name), 'ordinal': 1},
                    'bytes': None, 'suffix': '.img', 'error_code': 'image_extract_failed',
                })
                continue
            rels = _relationships(package, part_name)
            found = []
            _walk(root, found)
            part = _part_key(part_name)
            for ordinal, (placement, rid, embedded) in enumerate(found, 1):
                data, resolved, error = _read_target(package, part_name, rels, rid, embedded)
                images.append({
                    'locator': {'kind': 'docx_image', 'part': part, 'ordinal': ordinal, 'placement': placement},
                    'bytes': data, 'suffix': _suffix(resolved, data), 'error_code': error,
                })
    return images


def _one_result(record):
    if not record or record.get('error_code'):
        return {'ok': False, 'error_code': (record or {}).get('error_code') or 'image_ocr_failed'}
    from .reading import ReadingError, normalize_ocr
    try:
        text = normalize_ocr(record.get('payload') or {}).get('text', '').strip()
    except (ReadingError, TypeError, ValueError):
        return {'ok': False, 'error_code': 'image_ocr_failed'}
    if text == IMAGE_PLACEHOLDER:
        return {'ok': False, 'error_code': 'image_not_recognized'}
    return {'ok': True, 'text': text}


def recognize_docx_images(settings, images):
    """把图片字节交给现有 PaddleOCR 路径。调用方负责把结果写回来源单元。"""
    results = [None] * len(images)
    pending = []
    for index, image in enumerate(images):
        if image.get('error_code') or not image.get('bytes'):
            results[index] = {'ok': False, 'error_code': image.get('error_code') or 'image_extract_failed'}
            continue
        pending.append(index)
    if not pending:
        return results
    from .reading import ocr_records
    with tempfile.TemporaryDirectory(prefix='dda-docx-img-') as directory:
        pages = []
        for number, index in enumerate(pending, 1):
            image = images[index]
            path = Path(directory) / f'image-{number}{image.get("suffix") or ".img"}'
            path.write_bytes(image['bytes'])
            pages.append({'page_number': number, 'image_path': str(path)})
        try:
            records = list(ocr_records(settings, {
                'models': str(settings.ocr_models_dir),
                'pages': pages,
                'rec_batch': getattr(settings, 'ocr_rec_batch', 16),
                'page_batch': getattr(settings, 'ocr_page_batch', 4),
            }, directory))
        except Exception:
            for index in pending:
                results[index] = {'ok': False, 'error_code': 'image_ocr_failed'}
            return results
    by_page = {}
    for record in records:
        if isinstance(record, dict) and type(record.get('page_number')) is int:
            by_page[record['page_number']] = record
    for number, index in enumerate(pending, 1):
        results[index] = _one_result(by_page.get(number))
    return results
