"""按证据标识或来源范围补读。过期引用不返回新正文，跨项目与不存在使用同一句提示。"""
from uuid import UUID

from .material_metadata import material_view
from .office import locator_label
from .page_queries import PageQueryError
from .storage import Storage

# 与材料列表同一组列。主体角色在 project_subjects.role，对外叫 subject_role，避免和主体自身的 role 混用。
_MATERIAL_COLUMNS = '''d.category, d.subject_id, sub.name AS subject_name, sub.role AS subject_role,
        d.date_kind, d.point_date, d.period_start, d.period_end'''
_SUBJECT_JOIN = '''LEFT JOIN dd_agent.project_subjects sub
          ON sub.id = d.subject_id AND sub.project_id = d.project_id'''


MISSING = '证据不存在或不属于当前项目。'
MISSING_DOCUMENT = '材料不存在或不属于当前项目。'
STALE = '引用已过期，请重新检索。'
NOT_READY = '该来源尚未成功识别，不能补读正文。'
DEFAULT_LIMIT = 4000
MAX_LIMIT = 8000
MAX_PAGES = 20
MAX_UNITS = 20


def _limit(value):
    if value is None:
        return DEFAULT_LIMIT
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise ValueError(f'补读长度须为 1–{MAX_LIMIT}。')
    return value


def _offset(value):
    if type(value) is not int or value < 0:
        raise ValueError('正文偏移须为非负整数。')
    return value


def _slice(text, offset, limit):
    if offset > len(text):
        raise PageQueryError('stale', '续读偏移超出当前正文长度，请按新版本重新读取。')
    piece = text[offset:offset + limit]
    end = offset + len(piece)
    return piece, None if end >= len(text) else end


class EvidenceReader:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    def by_id(self, project_id, evidence_id, *, offset=0, limit=None, index_version=None):
        project_id = UUID(str(project_id))
        try:
            evidence_id = UUID(str(evidence_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('证据标识无效。') from None
        offset = _offset(offset)
        limit = _limit(limit)
        if index_version is not None and (not isinstance(index_version, str) or len(index_version) != 64):
            raise ValueError('索引版本无效。')
        with self.database.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            page = _page_chunk(conn, project_id, evidence_id)
            row = page or _unit_chunk(conn, project_id, evidence_id)
            if row is None:
                raise PageQueryError('not_found', MISSING)
            filenames = _filenames(conn, project_id, row['document_id'])
        self._file(row['storage_key'])
        if index_version is not None and index_version != row['index_version']:
            raise PageQueryError('stale', STALE)
        if page:
            if row['page_status'] != 'succeeded':
                raise PageQueryError('not_processed', NOT_READY)
            if not row['current']:
                raise PageQueryError('stale', STALE)
            text, next_offset = _slice(row['text'] or '', offset, limit)
            return {
                'project_id': str(project_id),
                'document_id': str(row['document_id']),
                'evidence_id': str(evidence_id),
                'filenames': filenames,
                'index_kind': 'page',
                'index_version': row['index_version'],
                'source_revision': row['source_revision'],
                'verification': row['verification'],
                'recognition': _recognition(row['verification']),
                'status': row['page_status'],
                'method': row['method'],
                'error_code': row['error_code'] or '',
                'locator': {
                    'kind': 'pdf', 'page': row['page_number'],
                    'start_offset': row['start_offset'], 'end_offset': row['end_offset'],
                },
                'locator_label': f"PDF 第 {row['page_number']} 页",
                'text': text,
                'offset': offset,
                'total_length': len(row['text'] or ''),
                'limit': limit,
                'next_offset': next_offset,
                'truncated': next_offset is not None,
                **material_view(row),
            }
        if row['status'] != 'succeeded' or row['formula_state'] == 'unknown':
            raise PageQueryError('not_processed', NOT_READY)
        if not row['current']:
            raise PageQueryError('stale', STALE)
        text, next_offset = _slice(row['text'] or '', offset, limit)
        locator = row['locator'] or {}
        return {
            'project_id': str(project_id),
            'document_id': str(row['document_id']),
            'evidence_id': str(evidence_id),
            'filenames': filenames,
            'index_kind': 'source_unit',
            'index_version': row['index_version'],
            'verification': None,
            'recognition': 'base',
            'status': row['status'],
            'formula_state': row['formula_state'] or '',
            'error_code': row['error_code'] or '',
            'locator': locator,
            'locator_label': locator_label(locator, row['formula_state'] or ''),
            'text': text,
            'offset': offset,
            'total_length': len(row['text'] or ''),
            'limit': limit,
            'next_offset': next_offset,
            'truncated': next_offset is not None,
            **material_view(row),
        }

    def by_range(self, project_id, document_id, *, page_from=None, page_to=None, resume_page=None,
                 offset=0, limit=None, source_revision=None, resume_unit=None):
        project_id, document_id = UUID(str(project_id)), UUID(str(document_id))
        offset = _offset(offset)
        limit = _limit(limit)
        if (page_from is None) ^ (page_to is None):
            raise ValueError('页范围须同时给出起页和止页。')
        with self.database.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            document = conn.execute(
                '''SELECT d.id, d.storage_key, ''' + _MATERIAL_COLUMNS + '''
                   FROM dd_agent.documents d ''' + _SUBJECT_JOIN + '''
                   WHERE d.project_id = %s AND d.id = %s''', (project_id, document_id)).fetchone()
            if document is None:
                raise PageQueryError('not_found', MISSING_DOCUMENT)
            filenames = _filenames(conn, project_id, document_id)
            material = material_view(document)
            self._file(document['storage_key'])
            if page_from is not None:
                return self._pages(conn, project_id, document_id, filenames, page_from, page_to,
                                   resume_page, offset, limit, source_revision, material=material)
            return self._units(conn, project_id, document_id, filenames, offset, limit, resume_unit, material=material)

    def _pages(self, conn, project_id, document_id, filenames, page_from, page_to, resume_page, offset, limit, source_revision, *, material):
        if type(page_from) is not int or type(page_to) is not int or page_from < 1 or page_to < page_from:
            raise ValueError('页范围须为正整数，且止页不小于起页。')
        if page_to - page_from + 1 > MAX_PAGES:
            raise ValueError(f'单次页范围最多 {MAX_PAGES} 页。')
        reading = conn.execute(
            '''SELECT page_count, pipeline_version FROM dd_agent.document_readings WHERE document_id = %s''',
            (document_id,)).fetchone()
        if reading is None:
            raise PageQueryError('not_processed', '材料尚未完成预检，不能按页范围补读。')
        if page_to > reading['page_count']:
            raise ValueError('页范围超出材料页数。')
        start = resume_page if resume_page is not None else page_from
        if type(start) is not int or start < page_from or start > page_to:
            raise ValueError('续读页不在范围内。')
        if source_revision is not None:
            pinned = _effective(conn, document_id, start)
            live = pinned['source_revision'] if pinned else None
            if live != source_revision:
                raise PageQueryError('stale', '该页来源已变化，请按新版本重新读取。')
        items = []
        remaining = limit
        next_pos = None
        for number in range(start, page_to + 1):
            row = _effective(conn, document_id, number)
            local = offset if number == start else 0
            if row is None or row['status'] != 'succeeded':
                items.append(_page_item(number, row, '', local, 0))
                continue
            text = row['text'] or ''
            if local > len(text):
                raise PageQueryError('stale', '续读偏移超出当前正文长度，请按新版本重新读取。')
            piece = text[local:]
            take = piece[:remaining]
            items.append(_page_item(number, row, take, local, len(text)))
            remaining -= len(take)
            if len(take) < len(piece):
                next_pos = {'page_number': number, 'offset': local + len(take)}
                break
            if remaining == 0 and number < page_to:
                next_pos = {'page_number': number + 1, 'offset': 0}
                break
        return _range_payload(project_id, document_id, filenames, page_from, page_to, limit, items, next_pos, material=material)

    def _units(self, conn, project_id, document_id, filenames, offset, limit, resume_unit, *, material):
        reading = conn.execute(
            'SELECT 1 FROM dd_agent.document_readings WHERE document_id = %s', (document_id,)).fetchone()
        if reading is not None:
            raise ValueError('这份材料有物理页码，请给出页范围，不能一次读取整卷。')
        exists = conn.execute(
            'SELECT 1 FROM dd_agent.source_units WHERE document_id = %s LIMIT 1', (document_id,)).fetchone()
        if exists is None:
            raise PageQueryError('not_processed', '材料尚未解析出来源单元，不能补读。')
        after_key, after_id = '', ''
        if resume_unit is not None:
            try:
                after = UUID(str(resume_unit))
            except (ValueError, TypeError, AttributeError):
                raise ValueError('续读位置无效。') from None
            marker = conn.execute(
                '''SELECT locator_key, id::text AS id_text FROM dd_agent.source_units
                   WHERE document_id = %s AND id = %s''', (document_id, after)).fetchone()
            if marker is None:
                raise PageQueryError('not_found', MISSING)
            after_key, after_id = marker['locator_key'], marker['id_text']
        rows = conn.execute('''SELECT id, locator, text, status, formula_state, error_code, locator_key
            FROM dd_agent.source_units
            WHERE document_id = %s AND (%s = '' OR (locator_key, id::text) >= (%s, %s))
            ORDER BY locator_key, id LIMIT %s''',
            (document_id, after_key, after_key, after_id, MAX_UNITS + 1)).fetchall()
        items = []
        remaining = limit
        next_pos = None
        started = resume_unit is not None
        for index, row in enumerate(rows[:MAX_UNITS]):
            local = offset if started and index == 0 else 0
            readable = row['status'] == 'succeeded' and row['formula_state'] != 'unknown'
            full = row['text'] or '' if readable else ''
            if local > len(full):
                raise PageQueryError('stale', '续读偏移超出当前正文长度，请按新版本重新读取。')
            piece = full[local:]
            take = piece[:remaining] if readable else ''
            items.append(_unit_item(row, take, local, len(full)))
            if readable:
                remaining -= len(take)
                if len(take) < len(piece):
                    next_pos = {'unit_id': str(row['id']), 'offset': local + len(take)}
                    break
            if remaining == 0 and index + 1 < len(rows):
                nxt = rows[index + 1]
                next_pos = {'unit_id': str(nxt['id']), 'offset': 0}
                break
        else:
            if len(rows) > MAX_UNITS:
                next_pos = {'unit_id': str(rows[MAX_UNITS]['id']), 'offset': 0}
        return _range_payload(project_id, document_id, filenames, None, None, limit, items, next_pos, material=material)

    def _file(self, storage_key):
        try:
            path = Storage(self.settings.data_dir, self.settings.max_upload_bytes).path(storage_key)
        except (ValueError, OSError):
            raise PageQueryError('missing_artifact', '原件文件缺失，请检查数据目录。') from None
        if not path.is_file():
            raise PageQueryError('missing_artifact', '原件文件缺失，请检查数据目录。')


def _page_chunk(conn, project_id, evidence_id):
    return conn.execute('''SELECT c.id, c.document_id, c.page_number, c.text, c.start_offset, c.end_offset,
        c.index_version, d.storage_key, p.status AS page_status, p.verification, p.method, p.error_code,
        p.source_revision, ''' + _MATERIAL_COLUMNS + ''',
        (p.status = 'succeeded' AND p.pipeline_version = i.pipeline_version
         AND p.updated_at = i.source_updated_at AND p.source_revision = i.source_revision) AS current
        FROM dd_agent.evidence_chunks c
        JOIN dd_agent.indexed_pages i
          ON i.document_id = c.document_id AND i.page_number = c.page_number AND i.index_version = c.index_version
        JOIN dd_agent.documents d ON d.id = c.document_id
        ''' + _SUBJECT_JOIN + '''
        LEFT JOIN dd_agent.effective_document_pages p
          ON p.document_id = c.document_id AND p.page_number = c.page_number
        WHERE c.id = %s AND d.project_id = %s''', (evidence_id, project_id)).fetchone()


def _unit_chunk(conn, project_id, evidence_id):
    return conn.execute('''SELECT c.id, c.document_id, c.text, c.index_version, c.locator,
        u.status, u.formula_state, u.error_code, d.storage_key, ''' + _MATERIAL_COLUMNS + ''',
        (u.status = 'succeeded' AND u.formula_state <> 'unknown' AND u.text = c.text) AS current
        FROM dd_agent.source_unit_chunks c
        JOIN dd_agent.source_units u ON u.id = c.unit_id
        JOIN dd_agent.documents d ON d.id = c.document_id
        ''' + _SUBJECT_JOIN + '''
        WHERE c.id = %s AND d.project_id = %s''', (evidence_id, project_id)).fetchone()


def _effective(conn, document_id, number):
    return conn.execute('''SELECT page_number, status, method, text, error_code, verification, source_revision
        FROM dd_agent.effective_document_pages
        WHERE document_id = %s AND page_number = %s''', (document_id, number)).fetchone()


def _filenames(conn, project_id, document_id):
    rows = conn.execute('''SELECT s.filename FROM dd_agent.document_sources s
        JOIN dd_agent.documents d ON d.id = s.document_id
        WHERE d.project_id = %s AND d.id = %s
        ORDER BY s.first_seen_at, s.filename LIMIT 50''', (project_id, document_id)).fetchall()
    return [row['filename'] for row in rows]


def _recognition(verification):
    if verification == 'machine_uncertain':
        return 'unknown'
    if verification in ('machine_enhanced', 'machine_checked'):
        return 'enhanced'
    return 'unverified'


def _page_item(number, row, text, offset, total):
    status = 'not_processed' if row is None else row['status']
    return {
        'page_number': number,
        'status': status,
        'method': None if row is None else row['method'],
        'error_code': '' if row is None else (row['error_code'] or ''),
        'verification': None if row is None else row['verification'],
        'recognition': 'unprocessed' if row is None or row['status'] != 'succeeded' else _recognition(row['verification']),
        'source_revision': None if row is None else row['source_revision'],
        'locator': {'kind': 'pdf', 'page': number},
        'locator_label': f'PDF 第 {number} 页',
        'text': text,
        'offset': offset,
        'total_length': total,
    }


def _unit_item(row, text, offset, total):
    locator = row['locator'] or {}
    unknown = row['formula_state'] == 'unknown'
    return {
        'unit_id': str(row['id']),
        'status': row['status'],
        'formula_state': row['formula_state'] or '',
        'error_code': row['error_code'] or '',
        'recognition': 'unknown' if unknown or row['status'] != 'succeeded' else 'base',
        'locator': locator,
        'locator_label': locator_label(locator, row['formula_state'] or ''),
        'text': text,
        'offset': offset,
        'total_length': total,
    }


def _range_payload(project_id, document_id, filenames, page_from, page_to, limit, items, next_pos, *, material):
    returned = sum(len(item['text']) for item in items)
    # 一份材料一份元数据，放在响应顶层。页或来源单元条目仍只带各自的定位和来源修订。
    return {
        'project_id': str(project_id),
        'document_id': str(document_id),
        'filenames': filenames,
        'page_from': page_from,
        'page_to': page_to,
        'limit': limit,
        'returned_characters': returned,
        'items': items,
        'next': next_pos,
        'truncated': next_pos is not None,
        **material,
    }
