# 原件存储层：只处理文件流、内容摘要和本地路径；登记信息由 db.py 负责。
# 原始文件名只作为元数据；实际存储键由项目 UUID＋内容 SHA-256 组成。
import hashlib
import os
from pathlib import Path
import re
import tempfile


class UploadTooLarge(Exception):
    pass


def _resolved_path(path):
    resolved = path.resolve()
    if os.name == 'nt':
        # Windows non-strict realpath may retain \\?\ when another upload
        # creates a previously missing path between its two filesystem probes.
        # Normalize only the namespace spelling AFTER resolving links; the
        # containment check below must still reject real junction/symlink escapes.
        value = str(resolved)
        if value.startswith('\\\\?\\UNC\\'):
            resolved = Path('\\\\' + value[8:])
        elif re.match(r'^\\\\\?\\[a-zA-Z]:\\', value):
            resolved = Path(value[4:])
    return resolved


def clean_filename(name):
    # 去掉上传名称中的路径和控制字符；名称不用于决定业务原件的磁盘位置。
    basename = (name or "未命名文件").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "", basename)[:240] or "未命名文件"


class Storage:
    def __init__(self, root, limit, soffice_resolver=None):
        self.root = _resolved_path(Path(root))
        self.limit = limit
        # 只在登记旧版 Office 时询问 soffice 是否存在，不在这里启动转换。
        self.soffice_resolver = soffice_resolver

    def save(self, project_id, filename, stream):
        # 流式写临时文件并计算摘要，避免一次把数百 MiB 文件读进 Python 内存。
        staging = self.root / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size, head = 0, b""
        descriptor, temporary = tempfile.mkstemp(dir=staging, suffix=".part")
        try:
            with os.fdopen(descriptor, "wb") as output:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    if size > self.limit:
                        raise UploadTooLarge()
                    if len(head) < 5:
                        head = (head + block)[:5]
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            filename = clean_filename(filename)
            from .office import classify_saved_file
            office = classify_saved_file(filename, temporary, self.soffice_resolver)
            if office is not None:
                status, reason = office
            elif size == 0:
                # 登记阶段只做空文件、扩展名和 PDF 头部筛查。
                # 文件能否打开、是否加密、是否扫描件，由后续 PDF 预检确定。
                status, reason = "invalid", "文件为空，请检查原件。"
            elif not filename.lower().endswith(".pdf"):
                status, reason = "unsupported", "当前阶段仅登记 PDF 供后续解析；此格式暂不支持处理。"
            elif head != b"%PDF-":
                status, reason = "invalid", "未识别到 PDF 文件头，请检查文件格式。"
            else:
                status, reason = "registered", "已登记，待解析。尚未检查正文、加密或扫描状态。"
            sha = digest.hexdigest()
            key = f"documents/{project_id}/{sha}.bin"
            target = self.path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Concurrent identical uploads have identical bytes. Existing blobs stay intact.
            try:
                # 硬链接把已写好的同一份字节发布为原件；目标已存在时不覆盖。
                # 这属于本机文件系统操作，不会上传云端或写入向量字段。
                os.link(temporary, target)
            except FileExistsError:
                pass
            return {"filename": filename, "sha256": sha, "byte_size": size, "storage_key": key, "status": status, "reason": reason}
        finally:
            Path(temporary).unlink(missing_ok=True)

    def path(self, key):
        # 解析后的路径必须仍在数据目录内，防止 ../ 或链接路径逃出工作区。
        # 即使 key 来自数据库，也不能省去此边界。
        path = _resolved_path(self.root / key)
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid storage path")
        return path
