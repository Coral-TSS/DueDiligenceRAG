"""独立 JPG／JPEG／PNG 材料：登记分类，以及把原件收成 OCR 使用的单页 PNG。"""
import os
from pathlib import Path

from .image_artifacts import ArtifactError, PNG_SIGNATURE, atomic_save, png_size


JPEG_MAGIC = b'\xff\xd8\xff'
IMAGE_EXTENSIONS = {'.jpg': 'jpeg', '.jpeg': 'jpeg', '.png': 'png'}
UNSUPPORTED_FORMAT_REASON = '此格式暂不支持处理。当前可登记 PDF、Word、Excel、PowerPoint 以及 JPG、JPEG、PNG。'
REGISTERED_IMAGE_REASON = '已登记，待解析。将作为单页图片进行文字识别。'
EMPTY_FILE_REASON = '文件为空，请检查原件。'
INVALID_PDF_HEADER_REASON = '未识别到 PDF 文件头，请检查文件格式。'
REGISTERED_PDF_REASON = '已登记，待解析。尚未检查正文、加密或扫描状态。'


class StandaloneImageError(Exception):
    def __init__(self, error_code, message):
        self.error_code = error_code
        super().__init__(message)


def image_extension(filename):
    lower = (filename or '').lower()
    dot = lower.rfind('.')
    if dot < 0:
        return ''
    return IMAGE_EXTENSIONS.get(lower[dot:], '')


def sniff_image(head):
    data = head or b''
    if data.startswith(PNG_SIGNATURE):
        return 'png'
    if data.startswith(JPEG_MAGIC):
        return 'jpeg'
    return None


def image_kind_from_path(path):
    path = Path(path)
    with path.open('rb') as stream:
        head = stream.read(8)
    return sniff_image(head)


def is_image_filename(filename):
    return bool(image_extension(filename))


def classify_non_office_file(filename, size, head):
    """空文件、独立图片、PDF 和其他扩展名的登记结论。不打开完整图像解码。"""
    head = head or b''
    if size == 0:
        return 'invalid', EMPTY_FILE_REASON
    kind = image_extension(filename)
    if kind:
        found = sniff_image(head)
        if found == kind:
            return 'registered', REGISTERED_IMAGE_REASON
        label = 'JPEG' if kind == 'jpeg' else 'PNG'
        return 'invalid', f'扩展名为 {label}，但未识别到 {label} 文件头，请检查文件格式。'
    if (filename or '').lower().endswith('.pdf'):
        if not head.startswith(b'%PDF-'):
            return 'invalid', INVALID_PDF_HEADER_REASON
        return 'registered', REGISTERED_PDF_REASON
    return 'unsupported', UNSUPPORTED_FORMAT_REASON


def can_upgrade_unsupported(existing_status, existing_reason, new_status):
    """先前因缺少 soffice，或因当时只接受 PDF 而拒绝的材料，再次导入且现已可登记时，才升为 registered。"""
    if new_status != 'registered' or existing_status != 'unsupported':
        return False
    reason = existing_reason or ''
    if '未找到 LibreOffice 的 soffice' in reason:
        return True
    if '当前阶段仅登记 PDF' in reason or '此格式暂不支持处理' in reason:
        return True
    return False


def is_legacy_unsupported_reason(reason):
    """旧版「仅登记 PDF」或「此格式暂不支持处理」的拒绝原因。不含密码、宏、损坏等结论。"""
    text = reason or ''
    return '当前阶段仅登记 PDF' in text or '此格式暂不支持处理' in text


def decide_legacy_image_upgrade(status, reason, filenames, size, head, *, file_available):
    """磁盘原件若按现行规则已是可登记的 JPG／JPEG／PNG，返回新的 (status, reason)，否则返回 None。

    不改目录、主体或日期。文件缺失、空文件，或分类结果仍是 unsupported／invalid 时不升级。
    密码、宏、损坏和文件头不符的结论也不会被清掉。
    """
    if not file_available:
        return None
    if status != 'unsupported' or not is_legacy_unsupported_reason(reason):
        return None
    for filename in filenames or ():
        new_status, new_reason = classify_non_office_file(filename, size, head)
        if can_upgrade_unsupported(status, reason, new_status):
            return new_status, new_reason
    return None


def inspect_stored_file(storage, storage_key):
    """读取登记时使用的大小和文件头（16 字节）。路径非法、文件缺失或无法读取时返回 None。"""
    try:
        path = storage.path(storage_key)
    except (TypeError, ValueError, OSError):
        return None
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        with path.open('rb') as stream:
            head = stream.read(16)
        return size, head
    except OSError:
        return None


def upgrade_legacy_unsupported_images(database, storage):
    """服务启动时把旧版拒绝的独立图片就地改为已登记。不入队，也不启动识别。

    可重复执行：已升级的行不再作为候选；仍不可升级的行保持原状。
    """
    upgraded = 0
    for row in database.list_legacy_unsupported_documents():
        stored = inspect_stored_file(storage, row.get('storage_key'))
        if stored is None:
            continue
        size, head = stored
        decision = decide_legacy_image_upgrade(
            row.get('status'), row.get('reason'), row.get('filenames') or (),
            size, head, file_available=True)
        if decision is None:
            continue
        if database.apply_legacy_image_upgrade(row['id'], decision[0], decision[1]) is not None:
            upgraded += 1
    return upgraded

def inspect_standalone_image(path):
    """登记后的预检：固定为 1 页并走 OCR，不读取 PDF 文字层。"""
    kind = image_kind_from_path(path)
    if kind is None:
        raise StandaloneImageError('image_corrupt', '无法识别图片格式，文件可能已损坏。')
    if kind == 'png':
        try:
            png_size(Path(path).read_bytes())
        except (ArtifactError, OSError) as exc:
            raise StandaloneImageError('image_corrupt', 'PNG 图片无法打开，可能已损坏或截断。') from exc
    return {'page_count': 1, 'pages': [{'page_number': 1, 'characters': 0, 'route': 'ocr'}], 'source_kind': 'image'}


def convert_jpeg_to_png(source, target, *, max_pixels, max_file_bytes):
    """用 Pillow 把 JPEG 收成 PNG。超限不缩小；截断或损坏直接失败。"""
    try:
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = False
    except ImportError as exc:
        raise StandaloneImageError('image_converter_missing', '当前环境无法转换 JPEG，请检查 PDF 工作进程。') from exc
    source, target = Path(source), Path(target)
    try:
        with Image.open(source) as image:
            image.load()
            width, height = image.size
            if max_pixels and width * height > max_pixels:
                raise StandaloneImageError('image_budget_exceeded', '单图像素超过上限，未降低画质。')
            if image.mode not in {'RGB', 'L', 'RGBA'}:
                image = image.convert('RGB')
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.stem + '.partial.png')
            try:
                image.save(partial, format='PNG')
                if max_file_bytes and partial.stat().st_size > max_file_bytes:
                    partial.unlink(missing_ok=True)
                    raise StandaloneImageError('disk_budget_exceeded', '磁盘字节上限已超过，未降低画质。')
                os.replace(partial, target)
            except StandaloneImageError:
                raise
            except Exception:
                Path(partial).unlink(missing_ok=True)
                raise
    except StandaloneImageError:
        raise
    except Exception as exc:
        raise StandaloneImageError('image_corrupt', 'JPEG 图片无法打开，可能已损坏或截断。') from exc
    return target


def materialize_standalone_png(source, target, *, max_pixels, max_file_bytes):
    """把独立图片写成 OCR 使用的 PNG。PNG 原件直接落盘，JPEG 先转换。"""
    source, target = Path(source), Path(target)
    try:
        kind = image_kind_from_path(source)
    except OSError as exc:
        raise StandaloneImageError('image_corrupt', '图片无法读取，文件可能已损坏。') from exc
    if kind == 'png':
        try:
            data = source.read_bytes()
            width, height = png_size(data)
        except (ArtifactError, OSError) as exc:
            raise StandaloneImageError('image_corrupt', 'PNG 图片无法打开，可能已损坏或截断。') from exc
        if max_pixels and width * height > max_pixels:
            raise StandaloneImageError('image_budget_exceeded', '单图像素超过上限，未降低画质。')
        if max_file_bytes and len(data) > max_file_bytes:
            raise StandaloneImageError('disk_budget_exceeded', '磁盘字节上限已超过，未降低画质。')
        atomic_save(target, data)
        return target
    if kind == 'jpeg':
        return convert_jpeg_to_png(source, target, max_pixels=max_pixels, max_file_bytes=max_file_bytes)
    raise StandaloneImageError('image_corrupt', '无法识别图片格式，文件可能已损坏。')
