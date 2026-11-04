"""有界页读取服务（RG-05g1／K02）。

职责：项目边界、页目录 keyset 续读、单页正文切片与来源绑定、页历史分页。
本文件只返回领域错误（PageQueryError），由 app.py 适配 HTTP 状态码；
不导入 web 框架，后续 MCP 适配器可直接复用同一服务。
"""
from datetime import datetime
from uuid import UUID

from .performance import count, timed


TEXT_SLICE = 100_000          # 单次返回的正文上限；超出用同版本偏移续读，不静默截断。
CATALOG_DEFAULT, CATALOG_MAX = 50, 100
HISTORY_DEFAULT, HISTORY_MAX = 10, 50
HISTORY_PREVIEW = 500         # 历史条目只给预览，避免一次取回全历史正文。
VIEWS = ('raw', 'effective')

# K02 错误映射：404 不存在／跨项目／缺产物，409 未处理／过期／需改用有界入口，503 忙，504 超时。
ERROR_STATUS = dict(not_found=404, wrong_project=404, missing_artifact=404,
                    not_processed=409, stale=409, bounded_read_required=409,
                    busy=503, timeout=504)

# 跨项目与真正不存在共用同一句提示，避免用 UUID 探测别的项目。
MISSING_DOCUMENT = '材料不存在或不属于当前项目。'


class PageQueryError(Exception):
    """带稳定错误码的领域错误；消息可直接展示给用户。"""

    def __init__(self, code, message):
        if code not in ERROR_STATUS:
            raise ValueError(code)
        super().__init__(message)
        self.code = code


def bounded(value, default, maximum, label):
    # 绕过 HTTP 直接调用（例如未来 MCP）时仍受同一上限约束。
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'{label}须为 1–{maximum}。')
    return value


def page_number(value):
    if type(value) is not int or value < 1:
        raise ValueError('页号须为正整数。')
    return value


def _cursor(value):
    """历史游标＝'创建时间|记录 ID'；非法游标按参数错误处理，不做猜测。"""
    if value is None:
        return None
    try:
        stamp, identity = value.split('|', 1)
        return datetime.fromisoformat(stamp), UUID(identity)
    except (ValueError, AttributeError):
        raise ValueError('历史游标格式无效。') from None


class PageReader:
    """一个项目内、一份材料的页读取视图；所有查询都带 document_id 边界。"""

    def __init__(self, database, project_id, document_id):
        self.database = database
        self.project_id = UUID(str(project_id))
        self.document_id = UUID(str(document_id))

    def _document(self):
        document = self.database.document(self.project_id, self.document_id)
        if document is None:
            # wrong_project 目前不对外区分：只保留统一提示，避免跨项目探测。
            raise PageQueryError('not_found', MISSING_DOCUMENT)
        return document

    def _reading(self):
        reading = self.database.reading_version(self.document_id)
        if reading is None:
            raise PageQueryError('not_processed', '材料尚未完成预检，暂无页可读。')
        return reading

    def _page(self, number, view='effective'):
        reading = self._reading()
        if number > reading['page_count']:
            raise PageQueryError('not_found', '该页超出材料页数。')
        row = self.database.page_row(self.document_id, number, effective=view == 'effective')
        if row is None:
            raise PageQueryError('not_processed', '该页尚未读取，请先完成该页处理。')
        return row

    @timed('page_reads.catalog')
    def catalog(self, limit=None, after=None):
        """按物理页号 keyset 续读页目录：不含正文、行框或内部存储路径。"""
        limit = bounded(limit, CATALOG_DEFAULT, CATALOG_MAX, '每批页数')
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError('页游标须为非负整数。')
        self._document()
        result = self.database.page_catalog(self.document_id, limit, after=after or 0)
        if result is None:
            raise PageQueryError('not_processed', '材料尚未完成预检，暂无页目录。')
        summary, items = result['summary'], result['items']
        count('page_reads.catalog.items', len(items))
        return dict(
            document_id=str(self.document_id),
            reading_version=summary['reading_version'],
            page_count=summary['page_count'],
            total_pages=summary['total_pages'],
            succeeded_pages=summary['succeeded_pages'],
            failed_pages=summary['failed_pages'],
            # status_revision 随页状态／进度变化；content_revision 只在文字或有效来源变化时变化。
            status_revision=summary['status_revision'],
            content_revision=summary['content_revision'],
            as_of=summary['as_of'].isoformat(),
            # 非冻结快照：同一阅读版本下新完成页会出现在后续批次，不必让已有游标失效。
            snapshot='not_frozen',
            items=items,
            next_cursor=items[-1]['page_number'] if result['has_more'] and items else None,
        )

    @timed('page_reads.page')
    def page(self, number, view='effective', offset=0, source_revision=None, include_lines=False):
        """单页读取：一条 SQL 定位该页，正文超过 TEXT_SLICE 时用偏移续读。"""
        number = page_number(number)
        if view not in VIEWS:
            raise ValueError('view 须为 raw 或 effective。')
        if type(offset) is not int or offset < 0:
            raise ValueError('正文偏移须为非负整数。')
        self._document()
        row = self._page(number, view)
        revision = self._revision(row, view)
        if source_revision is not None and source_revision != revision:
            # 续读必须绑定来源版本：内容变了就明确拒绝，而不是把两段不同文字拼起来。
            raise PageQueryError('stale', '该页来源已变化，请按新版本重新读取。')
        text = row['text'] or ''
        total = len(text)
        if offset > total:
            raise PageQueryError('stale', '续读偏移超出当前正文长度，请按新版本重新读取。')
        chunk = text[offset:offset + TEXT_SLICE]
        end = offset + len(chunk)
        count('page_reads.text.chars', len(chunk))
        payload = dict(
            page_number=number, view=view,
            status=row['status'], method=row['method'], error_code=row['error_code'],
            # revision 是本视图用于续读绑定的版本；raw_revision／source_revision 同时返回便于比对。
            revision=revision,
            raw_revision=row['raw_revision'], source_revision=row['source_revision'],
            verification=row['verification'],
            text=chunk, total_length=total, offset=offset,
            next_offset=None if end >= total else end, slice_limit=TEXT_SLICE,
            has_image=bool(row['image_key']),
            image_url=f'/api/projects/{self.project_id}/documents/{self.document_id}/pages/{number}/image',
        )
        if include_lines:
            # 行框按需返回：只有显式要求时才带上该页的区域坐标。
            payload['lines'] = row['lines'] or []
        return payload

    @timed('page_reads.history')
    def history(self, number, limit=None, after=None):
        """页的机器增强历史：最新状态由 page() 提供，这里只列有界历史。"""
        number = page_number(number)
        limit = bounded(limit, HISTORY_DEFAULT, HISTORY_MAX, '历史条数')
        cursor = _cursor(after)
        self._document()
        current = self._page(number, 'effective')
        rows = self.database.page_history(self.document_id, number, limit, cursor, HISTORY_PREVIEW)
        more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            items.append(dict(
                id=str(row['id']),
                created_at=row['created_at'].isoformat(), profile_version=row['profile_version'],
                base_revision=row['base_revision'], verification=row['verification'],
                # 同一基础页可以增强多次；只有有效视图实际选择的那条是当前结果。
                is_current=row['id'] == current['machine_id'],
                base_matches_current=row['base_revision'] == current['raw_revision'],
                text_length=row['text_length'],
                text_preview=row['text_preview'],
                preview_truncated=bool(row['text_length'] and row['text_length'] > HISTORY_PREVIEW),
            ))
        count('page_reads.history.items', len(items))
        return dict(page_number=number, raw_revision=current['raw_revision'],
                    source_revision=current['source_revision'], items=items,
                    next_cursor=f"{rows[-1]['created_at'].isoformat()}|{rows[-1]['id']}" if more and rows else None)

    def summary(self, number, source_revision=None):
        """当前增强摘要：正文由 page() 读取，历史由 history() 按需分页读取。"""
        number = page_number(number)
        self._document()
        current = self._page(number, 'effective')
        if source_revision is not None and source_revision != current['source_revision']:
            raise PageQueryError('stale', '该页来源已变化，请刷新当前页。')
        metadata = self.database.page_machine_summary(self.document_id, number, current['machine_id'])
        return dict(page_number=number, source_revision=current['source_revision'],
                    raw_revision=current['raw_revision'], **metadata)

    @staticmethod
    def _revision(row, view):
        # raw 视图的有效来源就是基础阅读行，因此用 raw_revision 作续读绑定。
        return row['raw_revision'] if view == 'raw' else row['source_revision']
