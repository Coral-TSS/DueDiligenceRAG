"""统一图像产物：识别图、预览、缩略图、区域裁剪。

键包含原件摘要、物理页、渲染引擎、DPI、色彩、方向、裁剪和转换版本。
像素或磁盘超限时直接拒绝，不缩小、不换色、不降质。
缩略图不能充当识别输入。单键生成用带拥有者的租约；半写文件不出现在最终路径。
本模块不删除已登记产物。Windows 视觉与 OCR 工作进程的实机核对留在本机。
"""
import hashlib
import json
import math
import os
from pathlib import Path
import struct
from uuid import uuid4

from .performance import count


TRANSFORM_VERSION = 'image-transform-v1'
ENGINE = 'pdfium'
KINDS = ('recognition', 'preview', 'thumbnail', 'crop')
# 与视觉工人原有的单图 1200 万像素同一量级；超限报错，不把长边缩到 1280。
SINGLE_IMAGE_PIXELS = 12_000_000
INFLIGHT_PIXELS = 48_000_000
DISK_BYTES = 2 * 1024 * 1024 * 1024
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class ArtifactError(Exception):
    pass


class ArtifactKindError(ArtifactError):
    pass


class ArtifactBoundsError(ArtifactError):
    pass


class ArtifactLimitError(ArtifactError):
    """上限已超过。调用方不得改用更低清晰度重试同一请求。"""

    def __init__(self, kind, limit, used):
        self.kind = kind
        self.limit = limit
        self.used = used
        super().__init__(f'{kind}上限 {limit}，当前需求 {used}，已拒绝且未降低画质。')


class ArtifactMeter:
    def __init__(self):
        self.renders = 0
        self.decodes = 0
        self.peak_pixels = 0
        self.bytes_written = 0

    def note_render(self, pixels, nbytes):
        self.renders += 1
        self.bytes_written += nbytes
        self.peak_pixels = max(self.peak_pixels, pixels)
        count('image.render', 1)
        count('image.bytes', nbytes)

    def note_decode(self, resident_pixels):
        self.decodes += 1
        self.peak_pixels = max(self.peak_pixels, resident_pixels)
        count('image.decode', 1)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ArtifactError('图像参数不是有限数值。')
    if float(value).is_integer():
        return str(int(value))
    return format(float(value), '.6g')


def artifact_key(*, source_sha256, page, kind, engine, dpi, color, orientation, crop, transform_version):
    """稳定描述键。种类不同则键不同，因此缩略图不会命中识别图。"""
    if kind not in KINDS:
        raise ArtifactKindError('图像种类须为识别图、预览、缩略图或裁剪。')
    if not isinstance(source_sha256, str) or len(source_sha256) < 12:
        raise ArtifactError('原件摘要无效。')
    if type(page) is not int or page < 1:
        raise ArtifactError('物理页码无效。')
    if not engine or not color or not transform_version:
        raise ArtifactError('渲染引擎、色彩或转换版本无效。')
    crop_part = 'full' if crop is None else ','.join(_num(v) for v in _crop_tuple(crop))
    return '|'.join([
        source_sha256, str(page), kind, str(engine), _num(dpi), str(color),
        _num(orientation), crop_part, str(transform_version),
    ])


def _crop_tuple(crop):
    if len(crop) != 4:
        raise ArtifactBoundsError('裁剪须为四个坐标。')
    return tuple(float(v) for v in crop)


def _rotate_point(x, y, degrees):
    quarter = degrees % 360
    if quarter == 0:
        return float(x), float(y)
    if quarter == 90:
        return float(y), float(-x)
    if quarter == 180:
        return float(-x), float(-y)
    if quarter == 270:
        return float(-y), float(x)
    radians = math.radians(degrees)
    cosine, sine = math.cos(radians), math.sin(radians)
    # 图像坐标 y 向下，顺时针旋转。
    return (x * cosine + y * sine, -x * sine + y * cosine)


def _unrotate_point(x, y, degrees):
    quarter = degrees % 360
    if quarter == 0:
        return float(x), float(y)
    if quarter == 90:
        return float(-y), float(x)
    if quarter == 180:
        return float(-x), float(-y)
    if quarter == 270:
        return float(y), float(-x)
    radians = math.radians(degrees)
    cosine, sine = math.cos(radians), math.sin(radians)
    return (x * cosine - y * sine, x * sine + y * cosine)


class PageTransform:
    """原图像素到产物像素的仿射，以及反向映射。不裁掉越界区域。"""

    def __init__(self, source_width, source_height, rotation_degrees, scale, crop=None):
        if (not math.isfinite(source_width) or not math.isfinite(source_height)
                or source_width <= 0 or source_height <= 0):
            raise ArtifactError('原图尺寸无效。')
        if not math.isfinite(rotation_degrees):
            raise ArtifactError('旋转角度无效。')
        if not math.isfinite(scale) or scale <= 0:
            raise ArtifactError('缩放比例无效。')
        corners = [(0.0, 0.0), (float(source_width), 0.0), (0.0, float(source_height)),
                   (float(source_width), float(source_height))]
        if crop is not None:
            x0, y0, x1, y1 = _crop_tuple(crop)
            if not (0 <= x0 < x1 <= source_width and 0 <= y0 < y1 <= source_height):
                raise ArtifactBoundsError('裁剪超出页面。')
            corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
        rotated = [_rotate_point(x, y, rotation_degrees) for x, y in corners]
        min_x = min(point[0] for point in rotated)
        min_y = min(point[1] for point in rotated)
        max_x = max(point[0] for point in rotated)
        max_y = max(point[1] for point in rotated)
        self.source_size = (float(source_width), float(source_height))
        self.rotation_degrees = float(rotation_degrees)
        self.scale = float(scale)
        self.crop = None if crop is None else tuple(float(v) for v in _crop_tuple(crop))
        self._min = (min_x, min_y)
        self.artifact_size = ((max_x - min_x) * scale, (max_y - min_y) * scale)
        # x' = scale * (x_rot - min_x)，x_rot 由旋转得到；六个系数在整数角度上可复核。
        self.forward_origin = (min_x, min_y)

    def to_artifact(self, x, y):
        rotated_x, rotated_y = _rotate_point(x, y, self.rotation_degrees)
        return ((rotated_x - self._min[0]) * self.scale, (rotated_y - self._min[1]) * self.scale)

    def to_source(self, x, y):
        rotated_x = x / self.scale + self._min[0]
        rotated_y = y / self.scale + self._min[1]
        return _unrotate_point(rotated_x, rotated_y, self.rotation_degrees)

    def matrix(self):
        """只记录旋转、缩放和平移参数，供登记和反向映射。"""
        return {
            'rotation_degrees': self.rotation_degrees,
            'scale': self.scale,
            'translate': [-self._min[0] * self.scale, -self._min[1] * self.scale],
            'source_size': list(self.source_size),
            'artifact_size': list(self.artifact_size),
            'crop': None if self.crop is None else list(self.crop),
            'transform_version': TRANSFORM_VERSION,
        }


def dpi_for_scale(scale, page_dpi=72):
    if not math.isfinite(scale) or scale <= 0 or not math.isfinite(page_dpi) or page_dpi <= 0:
        raise ArtifactError('DPI 无效。')
    return page_dpi * scale


def admit_image(width, height, *, single_limit, inflight, inflight_limit, disk_used, incoming_bytes, disk_limit):
    """超限即失败。没有缩小、换压缩或降采样的分支。"""
    if any(type(v) is bool or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
           for v in (width, height, single_limit, inflight, inflight_limit, disk_used, incoming_bytes, disk_limit)):
        raise ArtifactError('像素或磁盘预算无效。')
    pixels = width * height
    if pixels > single_limit:
        raise ArtifactLimitError('单图像素', single_limit, pixels)
    if inflight + pixels > inflight_limit:
        raise ArtifactLimitError('在途像素', inflight_limit, inflight + pixels)
    if disk_used + incoming_bytes > disk_limit:
        raise ArtifactLimitError('磁盘字节', disk_limit, disk_used + incoming_bytes)
    return pixels


def atomic_save(path, data):
    """先写临时文件再替换。最终路径在替换完成前不存在，或仍是上一份完整文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.partial')
    try:
        with temporary.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    else:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return path


def png_size(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) < 24 or data[:8] != PNG_SIGNATURE:
        raise ArtifactError('不是完整的 PNG。')
    length = struct.unpack('>I', data[8:12])[0]
    if data[12:16] != b'IHDR' or length < 8:
        raise ArtifactError('PNG 缺少尺寸信息。')
    width, height = struct.unpack('>II', data[16:24])
    if width < 1 or height < 1 or b'IEND' not in data:
        raise ArtifactError('PNG 尺寸无效。')
    return width, height


def sidecar_path(path):
    path = Path(path)
    return path.with_name(path.name + '.dda.json')


def inspect_artifact(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return {'state': 'missing'}
    try:
        data = path.read_bytes()
        size = png_size(data)
    except (OSError, ArtifactError):
        return {'state': 'corrupt'}
    digest = hashlib.sha256(data).hexdigest()
    side = sidecar_path(path)
    if not side.is_file() or side.is_symlink():
        return {'state': 'unsealed', 'sha256': digest, 'artifact_size': size, 'bytes': len(data)}
    try:
        descriptor = json.loads(side.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'state': 'corrupt'}
    recorded = descriptor.get('artifact_size') if isinstance(descriptor, dict) else None
    if not isinstance(descriptor, dict) or descriptor.get('sha256') != digest or list(recorded or []) != [size[0], size[1]]:
        # 文件本身是 PNG，但和已登记校验不一致：不能复用，重新渲染后再登记。
        return {'state': 'stale', 'sha256': digest, 'artifact_size': size, 'bytes': len(data)}
    return {'state': 'ok', 'descriptor': descriptor, 'sha256': digest, 'artifact_size': size}


def describe(*, source_sha256, page, kind, engine, dpi, color, orientation, crop,
             source_size, scale, sha256, nbytes, relative_path, artifact_size, rebuildable=True):
    transform = PageTransform(source_size[0], source_size[1], orientation, scale, crop)
    key = artifact_key(source_sha256=source_sha256, page=page, kind=kind, engine=engine, dpi=dpi,
                       color=color, orientation=orientation, crop=crop, transform_version=TRANSFORM_VERSION)
    return {
        'key': key,
        'kind': kind,
        'source_sha256': source_sha256,
        'page': page,
        'engine': engine,
        'dpi': dpi,
        'color': color,
        'orientation': orientation,
        'crop': None if crop is None else list(_crop_tuple(crop)),
        'transform_version': TRANSFORM_VERSION,
        'source_size': [source_size[0], source_size[1]],
        'scale': scale,
        'matrix': transform.matrix(),
        'artifact_size': [artifact_size[0], artifact_size[1]],
        'sha256': sha256,
        'bytes': nbytes,
        'relative_path': relative_path,
        'rebuildable': bool(rebuildable),
    }


def png_bytes(root, suffix='.png'):
    """统计登记根下的 PNG 字节，不跟随符号链接。"""
    total = 0
    base = Path(root)
    if not base.is_dir():
        return 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink() or not name.endswith(suffix):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def seal_rendered_png(path, *, root, relative_path, source_sha256, page, kind, scale):
    """按未旋转渲染的实际像素登记。尺寸对不上就拒绝，不另存一张更小的图。"""
    report = inspect_artifact(path)
    if report['state'] in {'missing', 'corrupt'}:
        raise ArtifactError('图像缺失或损坏，不能登记。')
    if report['state'] == 'ok' and report['descriptor'].get('kind') == kind:
        return report['descriptor']
    width, height = report['artifact_size']
    if not math.isfinite(scale) or scale <= 0:
        raise ArtifactError('缩放比例无效。')
    return seal_png(
        path, source_sha256=source_sha256, page=page, kind=kind, engine=ENGINE,
        dpi=dpi_for_scale(scale), color='rgb', orientation=0, crop=None,
        source_size=(width / scale, height / scale), scale=scale,
        relative_path=relative_path, root=root)


def seal_png(path, *, source_sha256, page, kind, engine=ENGINE, dpi, color, orientation, crop,
             source_size, scale, relative_path, root):
    """给已经原子落盘的 PNG 写描述。文件损坏时不另存一份降质图。"""
    path = Path(path)
    report = inspect_artifact(path)
    if report['state'] in {'missing', 'corrupt'}:
        raise ArtifactError('图像缺失或损坏，不能登记。')
    if report['state'] == 'ok' and report['descriptor'].get('kind') == kind and report['descriptor'].get('key'):
        return report['descriptor']
    data = path.read_bytes()
    width, height = png_size(data)
    # 描述里的产物尺寸以文件为准。与转换预测不一致说明参数和文件不是同一张图，拒绝登记。
    predicted = PageTransform(source_size[0], source_size[1], orientation, scale, crop).artifact_size
    if abs(predicted[0] - width) > 1 or abs(predicted[1] - height) > 1:
        raise ArtifactError('图像尺寸与旋转缩放不一致，未改写文件。')
    relative = Path(relative_path).as_posix()
    if relative.startswith('/') or '..' in Path(relative).parts:
        raise ArtifactError('图像相对路径越出登记根。')
    resolved = Path(root) / relative
    if Path(path).resolve() != resolved.resolve():
        raise ArtifactError('图像路径与登记键不一致。')
    descriptor = describe(
        source_sha256=source_sha256, page=page, kind=kind, engine=engine, dpi=dpi, color=color,
        orientation=orientation, crop=crop, source_size=source_size, scale=scale,
        sha256=hashlib.sha256(data).hexdigest(), nbytes=len(data), relative_path=relative,
        artifact_size=(width, height))
    atomic_save(sidecar_path(path), json.dumps(descriptor, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    return descriptor


def assert_recognition_input(descriptor):
    if not isinstance(descriptor, dict) or descriptor.get('kind') != 'recognition':
        raise ArtifactKindError('缩略图、预览或裁剪不能作为识别输入。')
    relative = str(descriptor.get('relative_path') or '')
    if '/thumbnail/' in f'/{relative}' or descriptor.get('kind') == 'thumbnail':
        raise ArtifactKindError('缩略图不能作为识别输入。')
    return descriptor


def reference_record(descriptor, *, referrers=(), lease_active=False, last_used=''):
    """交给 RG-05l 清单的只读登记。不包含删除动作。"""
    return {
        'role': 'image',
        'cache_root': 'image-artifacts',
        'relative_path': descriptor['relative_path'],
        'bytes': descriptor.get('bytes') or 0,
        'version': descriptor.get('transform_version') or TRANSFORM_VERSION,
        'referrers': list(referrers),
        'lease_active': bool(lease_active),
        'last_used': last_used,
        'rebuildable': bool(descriptor.get('rebuildable', True)),
        'protected': False,
        'kind': descriptor.get('kind'),
    }


def plan_pages(numbers, routes, exists, force_ocr, *, preview=False, intact=None, kinds=None):
    """识别页有完整图则复用。文字页默认不渲染，只有明确要预览时才画。缩略图不能复用为识别图。"""
    reuse, render = [], []
    kinds = kinds or {}
    for number in numbers:
        route = routes.get(number)
        kind = kinds.get(number)
        needs_image = bool(force_ocr or route == 'ocr' or (preview and route == 'text'))
        if not needs_image:
            continue
        present = bool(exists(number))
        good = present and (intact is None or intact(number)) and kind != 'thumbnail'
        if good:
            reuse.append(number)
        else:
            render.append(number)
    return reuse, render


def restart_action(*, image_intact, ocr_valid, image_required, preview_missing=False,
                   params_changed=False, text_depends_on_image=False):
    """按页决定复用、只重建图，或因参数变化让依赖该图的文本重做。不返回整卷重跑。"""
    if params_changed:
        return {
            'images': 'invalidate',
            'redo_ocr': bool(text_depends_on_image),
            'redo_text': bool(text_depends_on_image),
            'scope': 'page',
        }
    if preview_missing and ocr_valid and not image_required:
        return {'images': 'rebuild_preview', 'redo_ocr': False, 'redo_text': False, 'scope': 'page'}
    if ocr_valid and not image_required:
        return {'images': 'skip', 'redo_ocr': False, 'redo_text': False, 'scope': 'page'}
    if image_required and not image_intact:
        return {'images': 'rebuild', 'redo_ocr': not ocr_valid, 'redo_text': False, 'scope': 'page'}
    if image_intact and not ocr_valid:
        return {'images': 'reuse', 'redo_ocr': True, 'redo_text': False, 'scope': 'page'}
    return {'images': 'reuse', 'redo_ocr': False, 'redo_text': False, 'scope': 'page'}


def _lock_file(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GenerationLease:
    """单键跨进程租约。等待方拿到锁后必须重新检查产物，不能假定自己仍要生成。"""

    def __init__(self, root, key, owner):
        if not owner or not key:
            raise ArtifactError('生成租约缺少键或拥有者。')
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
        self.path = Path(root) / 'image-leases' / f'{digest}.lease'
        self.owner = str(owner)
        self.key = key
        self._file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 不用追加模式：追加写入会忽略 seek，拥有者记录会落在旧换行后面，读出来是空的。
        if not self.path.exists():
            self.path.write_bytes(b'\n')
        self._file = self.path.open('r+b')
        _lock_file(self._file)
        record = {'owner': self.owner, 'pid': os.getpid(), 'key': self.key}
        payload = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
        # 先覆盖再截掉旧尾巴，避免把已锁住的首字节截没（Windows 文件锁按区间）。
        self._file.seek(0)
        self._file.write(payload)
        self._file.truncate()
        self._file.flush()
        return self

    def holder(self):
        if self._file is None:
            raise ArtifactError('租约尚未持有。')
        self._file.seek(0)
        line = self._file.readline()
        if not line.strip():
            return None
        return json.loads(line.decode('utf-8'))

    def release(self):
        if self._file is None:
            return
        try:
            self._file.seek(0)
            self._file.truncate()
            self._file.flush()
        finally:
            try:
                _unlock_file(self._file)
            finally:
                self._file.close()
                self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def generate_once(root, key, owner, produce, intact):
    """持有单键租约。锁内复查，已有完整产物则不再生成。"""
    with GenerationLease(root, key, owner) as lease:
        found = intact()
        if found is not None:
            return {'artifact': found, 'rendered': False, 'owner': lease.holder()}
        artifact = produce()
        return {'artifact': artifact, 'rendered': True, 'owner': lease.holder()}


class DecodeScope:
    """有界页作用域内同一路径只解码一次。退出时关闭并清空，不保留全卷。"""

    def __init__(self, *, limit=4, opener, meter=None):
        if type(limit) is not int or limit < 1:
            raise ArtifactError('解码页数上限无效。')
        self.limit = limit
        self.opener = opener
        self.meter = meter or ArtifactMeter()
        self._open = {}
        self.closed = False

    def get(self, path):
        if self.closed:
            raise ArtifactError('解码作用域已退出。')
        token = str(Path(path))
        if token in self._open:
            return self._open[token]
        if len(self._open) >= self.limit:
            raise ArtifactLimitError('在途页', self.limit, len(self._open) + 1)
        image = self.opener(token)
        self._open[token] = image
        resident = 0
        for item in self._open.values():
            resident += int(getattr(item, 'pixels', 0) or 0)
        self.meter.note_decode(resident)
        return image

    def close(self):
        for image in self._open.values():
            closer = getattr(image, 'close', None)
            if closer:
                closer()
        self._open.clear()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def publish_png(path, data, *, width, height, single_limit, inflight, inflight_limit,
                disk_used, disk_limit, meter=None):
    """先检查预算再原子保存。失败时最终路径保持原样，不写入缩小后的图。"""
    pixels = admit_image(width, height, single_limit=single_limit, inflight=inflight,
                         inflight_limit=inflight_limit, disk_used=disk_used,
                         incoming_bytes=len(data), disk_limit=disk_limit)
    atomic_save(path, data)
    if meter is not None:
        meter.note_render(pixels, len(data))
    return path
