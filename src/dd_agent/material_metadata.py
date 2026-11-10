"""材料类别、项目主体和日期期间的校验，以及检索过滤的 SQL 片段。

类别用规格里的固定表。第一版不推断类别。过滤片段只描述文档列和物理页码，
不读取 Office 定位里的幻灯片号或行号。
"""
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID


class MetadataError(ValueError):
    def __init__(self, message, fields=None, status=422):
        super().__init__(message)
        self.fields = dict(fields or {})
        self.status = status


CATEGORIES = {
    'uncategorized': '未分类',
    'business_license': '营业执照',
    'articles': '公司章程',
    'shareholder_resolution': '股东会决议',
    'board_resolution': '董事会决议',
    'equity_structure': '股权结构／名册',
    'audit_report': '审计报告',
    'financial_statement': '财务报表',
    'major_contract': '重大合同',
    'litigation': '诉讼／仲裁文书',
    'ip_certificate': '知识产权证书',
    'regulatory_filing': '监管报送／批复',
    'hr_labor': '劳动／人事材料',
    'interview_record': '访谈记录',
    'other': '其他',
}

ROLES = {
    'target': '目标公司',
    'subsidiary': '子公司',
    'shareholder': '股东',
    'counterparty': '交易对手',
    'other': '其他',
}


def validate_category(value):
    if not isinstance(value, str) or value not in CATEGORIES:
        raise MetadataError('类别不在允许范围内。', {'category': '类别不在允许范围内。'})
    return value


def validate_role(value):
    if value is None or value == '':
        return 'other'
    if not isinstance(value, str) or value not in ROLES:
        raise MetadataError('主体角色不在允许范围内。', {'role': '主体角色不在允许范围内。'})
    return value


def validate_name(value):
    if not isinstance(value, str):
        raise MetadataError('主体名称须为 1–200 个字符。', {'name': '主体名称须为 1–200 个字符。'})
    value = value.strip()
    if not 1 <= len(value) <= 200:
        raise MetadataError('主体名称须为 1–200 个字符。', {'name': '主体名称须为 1–200 个字符。'})
    return value


def _as_date(value, field):
    if value is None or value == '':
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        if not isinstance(value, str) or len(value) != 10 or 'T' in value:
            raise MetadataError(f'{field}不是合法的日历日。', {field: '须为 YYYY-MM-DD。'})
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise MetadataError(f'{field}不是合法的日历日。', {field: '须为 YYYY-MM-DD。'}) from None
    return value


def validate_date_fields(kind, point, start, end):
    point = _as_date(point, 'point_date')
    start = _as_date(start, 'period_start')
    end = _as_date(end, 'period_end')
    if kind not in ('none', 'point', 'period'):
        raise MetadataError('日期种类无效。', {'date_kind': '须为 none、point 或 period。'})
    if kind == 'none':
        if point or start or end:
            raise MetadataError('未填写日期时不能同时给出时点或期间。', {'date_kind': '未填写时不能带日期。'})
        return dict(date_kind='none', point_date=None, period_start=None, period_end=None)
    if kind == 'point':
        if start or end:
            raise MetadataError('时点与期间不能同时填写。', {'date_kind': '时点与期间不能同时填写。'})
        if point is None:
            raise MetadataError('时点日期无效。', {'point_date': '须填写单一日期。'})
        return dict(date_kind='point', point_date=point, period_start=None, period_end=None)
    if point is not None:
        raise MetadataError('时点与期间不能同时填写。', {'date_kind': '时点与期间不能同时填写。'})
    if start is None or end is None:
        raise MetadataError('期间须同时填写起日和止日。', {'period_start': '起日与止日都要填写。'})
    if start > end:
        raise MetadataError('起日不能晚于止日。', {'period_start': '起日不能晚于止日。'})
    return dict(date_kind='period', point_date=None, period_start=start, period_end=end)


def dates_overlap(kind, point, start, end, *, date_point=None, period_start=None, period_end=None):
    """第 7.5 节重叠规则。未填写不匹配。检索仍以 SQL 为准，这里只给纯函数断言。"""
    if date_point is not None and (period_start is not None or period_end is not None):
        raise MetadataError('日期过滤不能同时给出时点和期间。')
    if date_point is not None:
        if kind == 'point':
            return point == date_point
        if kind == 'period':
            return start <= date_point <= end
        return False
    if period_start is None and period_end is None:
        return True
    if kind == 'point':
        return period_start <= point <= period_end
    if kind == 'period':
        return start <= period_end and end >= period_start
    return False


@dataclass(frozen=True)
class SearchScope:
    document_ids: tuple | None = None
    page_from: int | None = None
    page_to: int | None = None
    categories: tuple | None = None
    subject_ids: tuple | None = None
    date_point: date | None = None
    period_start: date | None = None
    period_end: date | None = None

    @property
    def active(self):
        return any(value is not None for value in (
            self.document_ids, self.page_from, self.categories, self.subject_ids,
            self.date_point, self.period_start))

    @property
    def dates_enabled(self):
        return self.date_point is not None or self.period_start is not None


def _uuid_list(values, label):
    if values is None:
        return None
    if isinstance(values, (str, UUID)) or not isinstance(values, (list, tuple)):
        raise MetadataError(f'{label}须为列表。')
    if len(values) == 0:
        raise MetadataError(f'{label}不能为空列表。')
    parsed, seen = [], set()
    for item in values:
        try:
            value = item if isinstance(item, UUID) else UUID(str(item).strip())
        except (ValueError, AttributeError, TypeError):
            raise MetadataError(f'{label}无效。') from None
        if value not in seen:
            seen.add(value)
            parsed.append(value)
    return tuple(parsed)


def _category_list(values):
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise MetadataError('类别须为列表。', {'categories': '类别须为列表。'})
    if len(values) == 0:
        raise MetadataError('类别不能为空列表。', {'categories': '类别不能为空列表。'})
    parsed, seen = [], set()
    for item in values:
        code = validate_category(item)
        if code not in seen:
            seen.add(code)
            parsed.append(code)
    return tuple(parsed)


def parse_filters(*, document_id=None, document_ids=None, page_from=None, page_to=None,
                  categories=None, subject_ids=None, date_point=None, period_start=None,
                  period_end=None, purpose='search'):
    if purpose == 'list' and any(value is not None for value in (document_id, document_ids, page_from, page_to)):
        raise MetadataError('列表筛选不含材料标识或页范围。')
    if document_id is not None and document_ids is not None:
        raise MetadataError('不能同时传入 document_id 与 document_ids。')
    if document_id is not None:
        document_ids = [document_id]
    documents = _uuid_list(document_ids, '材料标识')
    subjects = _uuid_list(subject_ids, '主体标识')
    codes = _category_list(categories)
    if (page_from is None) != (page_to is None):
        raise MetadataError('页范围须同时给出起页和止页。', {'page_from': '起页和止页都要填写。'})
    if page_from is not None:
        if type(page_from) is not int or type(page_to) is not int or page_from < 1 or page_to < page_from:
            raise MetadataError('页范围不合法。', {'page_from': '起页须为正整数且不大于止页。'})
    point = _as_date(date_point, 'date_point')
    start = _as_date(period_start, 'period_start')
    end = _as_date(period_end, 'period_end')
    if point is not None and (start is not None or end is not None):
        raise MetadataError('日期过滤不能同时给出时点和期间。', {'date_point': '时点与期间不能同时填写。'})
    if (start is None) != (end is None):
        raise MetadataError('日期期间过滤须同时给出起日和止日。', {'period_start': '起日与止日都要填写。'})
    if start is not None and start > end:
        raise MetadataError('过滤起日不能晚于止日。', {'period_start': '起日不能晚于止日。'})
    scope = SearchScope(documents, page_from, page_to, codes, subjects, point, start, end)
    return scope if scope.active else None


def metadata_sql(scope, alias='d'):
    """材料维度的 AND 条件。页码不在这里，避免把幻灯片号写进类别条件。"""
    if scope is None:
        return '', ()
    parts, args = [], []
    if scope.categories is not None:
        parts.append(f'{alias}.category = ANY(%s::text[])')
        args.append(list(scope.categories))
    if scope.subject_ids is not None:
        parts.append(f'{alias}.subject_id = ANY(%s::uuid[])')
        args.append(list(scope.subject_ids))
    if scope.date_point is not None:
        parts.append(
            f"(({alias}.date_kind = 'point' AND {alias}.point_date = %s) OR "
            f"({alias}.date_kind = 'period' AND {alias}.period_start <= %s AND {alias}.period_end >= %s))")
        args.extend((scope.date_point, scope.date_point, scope.date_point))
    elif scope.period_start is not None:
        parts.append(
            f"(({alias}.date_kind = 'point' AND {alias}.point_date BETWEEN %s AND %s) OR "
            f"({alias}.date_kind = 'period' AND {alias}.period_start <= %s AND {alias}.period_end >= %s))")
        args.extend((scope.period_start, scope.period_end, scope.period_end, scope.period_start))
    if not parts:
        return '', ()
    return ' AND ' + ' AND '.join(parts), tuple(args)


def page_sql(scope, column):
    if scope is None or scope.page_from is None:
        return '', ()
    return f' AND {column} BETWEEN %s AND %s', (scope.page_from, scope.page_to)


def span_page_sql(scope, column='c.source_spans'):
    """父块只认 source_spans 里的物理页码。没有页码的片段在设置页范围时不命中。"""
    if scope is None or scope.page_from is None:
        return '', ()
    return (
        f" AND EXISTS (SELECT 1 FROM jsonb_array_elements({column}) elem "
        "WHERE jsonb_typeof(elem) = 'object' AND elem ? 'page_number' "
        "AND (elem->>'page_number') ~ '^[0-9]+$' "
        "AND (elem->>'page_number')::int BETWEEN %s AND %s)",
        (scope.page_from, scope.page_to))
