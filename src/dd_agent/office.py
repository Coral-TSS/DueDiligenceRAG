"""现代 DOCX／XLSX／PPTX 的登记、来源单元和向量。旧版 DOC／XLS／PPT 先转换再走同一条解析。不把它们写成 PDF 页。

检索单元按完整事实切，而不是按单元格或自然段各切一条。Excel 每个数据行配上工作表名和列名，
只有一列的说明文字再按行范围合并；Word 表格一行配上表头，正文在同一标题下合并；
幻灯片在长度内合并形状文字。
超长单元再按 chunks.MAX_CHARS 切开，原文不截断。Word 嵌入图走现有 OCR，识别出的文字才入库。
材料只有在应入库的向量都写进当前索引版本后才算处理完成。

INDEX_UNIT_VERSION 变了之后，旧 Office 向量不再命中当前检索，覆盖缺口会标成待重建。
这里不自动重跑处理，要等用户对那份材料再次处理。
"""
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .chunks import MAX_CHARS, split_text
from .chunks import _HEADING
from .lexical import document_tsvector


PARSE_VERSION = 'office-v1'
# 单元形状版本。只影响 office_version() 的哈希，不改表结构，也不触发自动重建。
INDEX_UNIT_VERSION = 'office-unit-v2'
_PACKAGE = {
    '.docx': 'word/document.xml',
    '.xlsx': 'xl/workbook.xml',
    '.pptx': 'ppt/presentation.xml',
}
_LEGACY = {'.doc': '旧版 Word', '.xls': '旧版 Excel', '.ppt': '旧版 PowerPoint'}
_PHASES = ('converting', 'parsing', 'recognizing', 'indexing')
_PHASE_REASONS = {
    'converting': '正在用本机 LibreOffice 转换旧版 Office，尚未解析正文，也未建立向量。',
    'parsing': '正在解析段落、表格、单元格或幻灯片文字，尚未建立向量。',
    'recognizing': '正在识别文档中的图片文字，尚未建立向量。',
    'indexing': '正在建立向量索引，尚未全部入库。',
}
_OUTCOME_REASONS = {
    'complete': '正文已解析。当前版本向量已全部入库。',
    'partial': '已入库部分文字，仍有单元失败或图片未能识别，不能视为已处理。',
    'empty': '解析已结束，但没有提取到可入库的文字，不能视为已处理。',
    'image_failed': '图片未能识别，向量未入库，不能视为已处理。',
    'indexing_incomplete': '仍有文字尚未写入当前版本的向量索引，不能视为已处理。',
    'failed': '处理失败，不能视为已处理。',
}


def office_version(profile):
    if not profile:
        return None
    import hashlib
    return hashlib.sha256((INDEX_UNIT_VERSION + profile).encode()).hexdigest()


def _has_index_text(text):
    return bool((text or '').strip(' \t\n\r\f\v\u3000'))


def terminal_office_outcome(*, unit_failed, image_failed, indexable, indexed, version_known):
    """完成只表示：没有失败单元，且每段应入库的文字都在当前索引版本里。"""
    if not version_known:
        indexed = 0
    if unit_failed and indexed:
        return 'partial'
    if unit_failed:
        return 'image_failed' if image_failed else 'failed'
    if indexable and (not version_known or indexed < indexable):
        return 'indexing_incomplete'
    if indexable == 0:
        return 'empty'
    return 'complete'


def display_office_outcome(*, document_status, reason, job_status, job_phase,
                           unit_total, unit_failed, image_failed, indexable, indexed, version_known):
    if document_status in ('unsupported', 'invalid'):
        return document_status
    if job_status in ('running', 'pause_requested'):
        return job_phase if job_phase in _PHASES else 'parsing'
    if job_status == 'queued':
        return 'queued'
    if job_status == 'paused':
        return 'paused'
    if job_status == 'failed':
        return 'failed'
    if job_status is None and unit_total == 0 and '不能视为已处理' not in (reason or '') and '已全部入库' not in (reason or ''):
        return 'registered'
    return terminal_office_outcome(
        unit_failed=unit_failed, image_failed=image_failed, indexable=indexable,
        indexed=indexed, version_known=version_known)


def describe_office_outcome(outcome, *, conversion_reason='', had_images=False, unit_total=0,
                            recognized_images=False, image_failed=0):
    if outcome == 'complete':
        prefix = conversion_reason if conversion_reason else '正文已解析。'
        if prefix and not prefix.endswith('。'):
            prefix += '。'
        extra = '图片文字已识别。' if had_images else ''
        return f'{prefix}{extra}当前版本向量已全部入库。'
    if outcome == 'empty':
        prefix = conversion_reason if conversion_reason else ''
        if unit_total == 0:
            detail = '解析已结束，但没有提取到段落、表格、单元格、幻灯片或图片，不能视为已处理。'
        elif recognized_images:
            detail = '图片已识别，但没有可入库的文字，不能视为已处理。'
        else:
            detail = '解析已结束，但没有可入库的文字，不能视为已处理。'
        return prefix + detail
    if outcome == 'partial':
        if image_failed:
            return '已入库部分文字，仍有图片未能识别，不能视为已处理。'
        return '已入库部分文字，仍有单元未能入库，不能视为已处理。'
    return _OUTCOME_REASONS.get(outcome, _OUTCOME_REASONS['failed'])


def display_office_reason(outcome, stored_reason):
    stored = stored_reason or ''
    if outcome in ('registered', 'queued', 'paused', 'failed', 'unsupported', 'invalid', *_PHASES):
        return stored
    if outcome == 'complete' and '已全部入库' in stored:
        return stored
    if outcome == 'empty' and '不能视为已处理' in stored and '没有' in stored:
        return stored
    if outcome == 'partial' and '不能视为已处理' in stored and '已全部入库' not in stored:
        return stored
    if outcome == 'image_failed' and '图片未能识别' in stored:
        return stored
    if outcome == 'indexing_incomplete' and '不能视为已处理' in stored and '向量' in stored and '已全部入库' not in stored:
        return stored
    return _OUTCOME_REASONS.get(outcome, stored)


def _outcome_error(outcome):
    return {
        'empty': 'empty_extract',
        'image_failed': 'image_not_recognized',
        'indexing_incomplete': 'index_incomplete',
        'failed': 'embed_failed',
    }.get(outcome, '')


def _counts(units, indexed_keys, *, version_known):
    unit_failed = image_failed = indexable = indexed = 0
    for unit in units:
        if unit['status'] != 'succeeded':
            unit_failed += 1
            if unit['locator'].get('kind') == 'docx_image':
                image_failed += 1
            continue
        if unit.get('formula_state') == 'unknown' or not _has_index_text(unit['text']):
            continue
        indexable += 1
        if version_known and unit['locator_key'] in indexed_keys:
            indexed += 1
    return {
        'unit_total': len(units), 'unit_failed': unit_failed, 'image_failed': image_failed,
        'indexable': indexable, 'indexed': indexed,
    }


def _extension(filename):
    lower = (filename or '').lower()
    dot = lower.rfind('.')
    if dot < 0:
        return ''
    suffix = lower[dot:]
    if suffix in _PACKAGE or suffix in _LEGACY:
        return suffix
    return ''


def classify_saved_file(filename, path, soffice_resolver=None):
    """登记阶段只看扩展名和压缩包或复合文档结构，不抽取正文，也不启动转换器。"""
    ext = _extension(filename)
    if ext in _LEGACY:
        from .legacy_office import registration_decision
        decision = registration_decision(path, ext, soffice_resolver)
        if decision is not None:
            return decision
    if ext not in _PACKAGE:
        return None
    try:
        with zipfile.ZipFile(path) as package:
            names = [item.replace('\\', '/') for item in package.namelist()]
    except zipfile.BadZipFile:
        return 'invalid', '不是有效的 Office 压缩包，未解析正文。'
    if any(name == 'EncryptionInfo' or name == 'EncryptedPackage' or name.endswith('/EncryptionInfo') for name in names):
        return 'unsupported', '文件已加密或设有密码，未解析正文。'
    if any(name.endswith('vbaProject.bin') for name in names):
        return 'unsupported', '文件含宏。未执行宏，也未解析正文。'
    if _PACKAGE[ext] not in names:
        return 'invalid', '压缩包里没有该格式的正文，未解析。'
    return 'registered', '已登记，待解析。尚未提取段落、表格、单元格或幻灯片文字。'


def _bounds(start, end):
    if start is None:
        return ''
    if end is None or end == start:
        return str(start)
    return f'{start}–{end}'


def _column_number(letter):
    number = 0
    for char in str(letter):
        if not char.isalpha():
            return None
        number = number * 26 + (ord(char.upper()) - 64)
    return number or None


def _column_bounds(columns):
    if not columns:
        return ''
    if len(columns) == 1:
        return str(columns[0])
    if all(isinstance(item, int) for item in columns):
        if list(columns) == list(range(columns[0], columns[0] + len(columns))):
            return f'{columns[0]}–{columns[-1]}'
        return '、'.join(str(item) for item in columns)
    numbers = [_column_number(item) for item in columns]
    if all(numbers) and numbers == list(range(numbers[0], numbers[0] + len(numbers))):
        return f'{columns[0]}–{columns[-1]}'
    return '、'.join(str(item) for item in columns)


def locator_label(locator, formula_state=''):
    kind = locator.get('kind')
    if kind == 'docx_paragraph':
        label = f"段落 {locator['paragraph']}"
    elif kind == 'docx_paragraphs':
        label = '段落 ' + _bounds(locator.get('paragraph_from'), locator.get('paragraph_to'))
        heading = locator.get('heading_paragraph')
        if heading and heading != locator.get('paragraph_from'):
            label += f'，标题在段落 {heading}'
    elif kind == 'docx_table_cell':
        label = f"表{locator['table']} 行{locator['row']} 列{locator['column']}"
    elif kind == 'docx_table_row':
        row = _bounds(locator.get('row'), locator.get('row_to', locator.get('row')))
        label = f"表{locator['table']} 行{row}"
        columns = locator.get('columns') or []
        if columns:
            label += ' 列' + _column_bounds(columns)
    elif kind == 'xlsx_cell':
        label = f"工作表{locator['sheet']} 行{locator['row']} 列{locator['column']}"
    elif kind == 'xlsx_rows':
        label = f"工作表{locator.get('sheet')} 行{_bounds(locator.get('row_from'), locator.get('row_to'))}"
        columns = locator.get('columns') or []
        if columns:
            label += ' 列' + _column_bounds(columns)
    elif kind == 'pptx_paragraph':
        label = f"幻灯片{locator['slide']} 形状{locator['shape']} 段落{locator['paragraph']}"
    elif kind == 'pptx_slide':
        label = f"幻灯片{locator.get('slide')}"
        if locator.get('shape_from'):
            label += ' 形状' + _bounds(locator.get('shape_from'), locator.get('shape_to', locator.get('shape_from')))
    elif kind == 'pptx_shape':
        label = f"幻灯片{locator.get('slide')} 形状{locator.get('shape')}"
        if locator.get('paragraph_from'):
            label += ' 段落' + _bounds(locator.get('paragraph_from'), locator.get('paragraph_to'))
    elif kind == 'docx_image':
        part = str(locator.get('part') or 'document')
        ordinal = locator.get('ordinal', 1)
        if part.startswith('header'):
            label = f"页眉图片 {ordinal}"
        elif part.startswith('footer'):
            label = f"页脚图片 {ordinal}"
        else:
            label = f"图片 {ordinal}"
    else:
        label = '来源'
    parts = locator.get('parts')
    part = locator.get('part')
    if isinstance(parts, int) and parts > 1 and isinstance(part, int):
        label += f'（{part}/{parts}）'
    if formula_state == 'not_recalculated':
        label += '（含公式，未重算）'
    if locator.get('unknown_columns') and formula_state != 'unknown':
        label += '（部分公式未知）'
    return label


def _key(locator):
    return json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def parse_office(filename, blob):
    ext = _extension(filename)
    if ext == '.docx':
        return _bound_units(_parse_docx(blob))
    if ext == '.xlsx':
        return _bound_units(_parse_xlsx(blob))
    if ext == '.pptx':
        return _bound_units(_parse_pptx(blob))
    return []


def _pieces(text):
    """把超长原文切成不超过 MAX_CHARS 的片段。重叠可以有，缺字不行。"""
    parts = [item['text'] for item in split_text(text) if item.get('text')]
    if parts and all(len(part) <= MAX_CHARS for part in parts):
        return parts
    return _hard_slices(text, MAX_CHARS)


def _hard_slices(text, limit):
    slices = []
    cursor = 0
    while cursor < len(text):
        stop = min(cursor + limit, len(text))
        if stop < len(text):
            window = text[cursor:stop]
            cut = max(window.rfind('\n'), window.rfind('。'), window.rfind('；'), window.rfind('|'))
            if cut >= max(1, limit // 3):
                stop = cursor + cut + 1
        piece = text[cursor:stop].strip()
        if piece:
            slices.append(piece)
        if stop <= cursor:
            stop = min(cursor + limit, len(text))
        cursor = stop
    return slices


def _bound_units(units):
    bounded = []
    for unit in units:
        text = unit['text']
        if unit['status'] != 'succeeded' or len(text) <= MAX_CHARS:
            bounded.append(unit)
            continue
        pieces = _pieces(text) or [text]
        total = len(pieces)
        for index, piece in enumerate(pieces, 1):
            locator = dict(unit['locator'])
            if total > 1:
                locator['part'] = index
                locator['parts'] = total
            bounded.append(_unit(
                locator, piece, formula_state=unit['formula_state'],
                status=unit['status'], error_code=unit['error_code']))
    return bounded


def _is_heading(paragraph, text):
    style = paragraph.style
    name = (style.name or '') if style is not None else ''
    style_id = (getattr(style, 'style_id', '') or '') if style is not None else ''
    if name.startswith('Heading') or name.startswith('标题') or style_id.startswith('Heading'):
        return True
    if name in {'Title', '标题'} or style_id == 'Title':
        return True
    return _HEADING.match(text) is not None


def _parse_docx(blob):
    from docx import Document
    from .docx_images import blob_with_missing_targets
    # 缺图时 python-docx 会拒绝打开整包。先补空部件，只为读出文字；图片是否提出仍按原件判断。
    document = Document(BytesIO(blob_with_missing_targets(blob)))
    units = _docx_paragraph_units(document)
    for table_index, table in enumerate(document.tables, 1):
        units.extend(_docx_table_units(table_index, table))
    return units


def _docx_paragraph_units(document):
    units = []
    group = []
    heading_index = None
    heading_text = ''

    def flush():
        nonlocal group
        if group:
            units.extend(_pack_paragraphs(group, heading_index, heading_text))
        group = []

    for index, paragraph in enumerate(document.paragraphs, 1):
        text = paragraph.text.strip()
        if not text:
            continue
        if _is_heading(paragraph, text):
            flush()
            heading_index = index
            heading_text = text
            group = [(index, text)]
            continue
        group.append((index, text))
    flush()
    return units


def _pack_paragraphs(group, heading_index, heading_text):
    units = []
    current = []

    def emit(batch, prefix_heading=False):
        if not batch:
            return
        body = '\n'.join(text for _, text in batch)
        locator = {
            'kind': 'docx_paragraphs',
            'paragraph_from': batch[0][0],
            'paragraph_to': batch[-1][0],
        }
        if (prefix_heading and heading_text and heading_index
                and batch[0][0] != heading_index
                and len(heading_text) + 1 + len(body) <= MAX_CHARS):
            body = heading_text + '\n' + body
            locator['heading_paragraph'] = heading_index
        _append_text_units(units, locator, body)

    for index, text in group:
        if len(text) > MAX_CHARS:
            emit(current, prefix_heading=bool(current) and current[0][0] != heading_index)
            current = []
            pieces = _pieces(text)
            total = len(pieces)
            for part, piece in enumerate(pieces, 1):
                body = piece
                locator = {
                    'kind': 'docx_paragraphs',
                    'paragraph_from': index,
                    'paragraph_to': index,
                }
                if (heading_text and heading_index and index != heading_index
                        and len(heading_text) + 1 + len(piece) <= MAX_CHARS):
                    body = heading_text + '\n' + piece
                    locator['heading_paragraph'] = heading_index
                if total > 1:
                    locator['part'] = part
                    locator['parts'] = total
                units.append(_unit(locator, body))
            continue
        prospective = current + [(index, text)]
        joined = '\n'.join(item for _, item in prospective)
        if heading_text and prospective and prospective[0][0] != heading_index:
            joined = heading_text + '\n' + joined
        if current and len(joined) > MAX_CHARS:
            emit(current, prefix_heading=current[0][0] != heading_index)
            current = [(index, text)]
        else:
            current = prospective
    emit(current, prefix_heading=bool(current) and current[0][0] != heading_index)
    return units


def _append_text_units(units, locator, text):
    if len(text) <= MAX_CHARS:
        units.append(_unit(locator, text))
        return
    pieces = _pieces(text)
    total = len(pieces)
    for index, piece in enumerate(pieces, 1):
        piece_locator = dict(locator)
        if total > 1:
            piece_locator['part'] = index
            piece_locator['parts'] = total
        units.append(_unit(piece_locator, piece))


def _word_row_cells(row):
    entries = []
    seen = []
    column = 1 + (getattr(row, 'grid_cols_before', 0) or 0)
    for cell in row.cells:
        if any(cell is previous for previous in seen):
            column += 1
            continue
        seen.append(cell)
        texts = [paragraph.text.strip() for paragraph in cell.paragraphs if paragraph.text.strip()]
        text = ' '.join(texts)
        if text:
            entries.append((column, text))
        column += 1
    return entries


def _docx_table_units(table_index, table):
    rows = []
    for row_index, row in enumerate(table.rows, 1):
        cells = _word_row_cells(row)
        if cells:
            rows.append((row_index, cells))
    if not rows:
        return []
    titles, rest = _leading_titles(rows)
    if not rest:
        rest = rows
        titles = []
    labels = _label_map(rest) if _row_is_header(rest) else None
    data = rest[1:] if labels else rest
    if labels and not data:
        data = rest
        labels = None
    units = []
    prefix = [f'表{table_index}', *titles]
    for row_index, cells in data:
        fields, unknown = _fields_for_cells(cells, labels)
        locator = {
            'kind': 'docx_table_row',
            'table': table_index,
            'row': row_index,
        }
        units.extend(_units_from_fields(locator, prefix, fields, unknown, row_key='row', row_value=row_index))
    return units


_NUMERIC_TEXT = re.compile(r'^[+-]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?(?:%|％)?$')
_YEAR_TEXT = re.compile(r'^(?:19|20)\d{2}年?$')


def _is_numeric_text(text):
    raw = ''.join(str(text).split())
    raw = raw.replace('＋', '+').replace('－', '-').replace('−', '-')
    for suffix in ('万元', '亿元', '元', '万', '亿'):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[:-len(suffix)]
            break
    raw = raw.replace(',', '').replace('，', '')
    return _NUMERIC_TEXT.match(raw) is not None


def _header_like(raw, text):
    if isinstance(raw, bool):
        return False
    if isinstance(raw, (int, float)):
        number = float(raw)
        if not number.is_integer():
            return False
        year = int(number)
        return 1900 <= year <= 2100
    stripped = str(text).strip()
    if _YEAR_TEXT.match(stripped):
        return True
    return not _is_numeric_text(stripped)


def _leading_titles(rows):
    """rows 是 (row_number, cells)。横跨多列之前的单格行当成标题，不拿去当列名。"""
    titles = []
    rest = list(rows)
    while rest and len(rest[0][1]) == 1 and any(len(cells) >= 2 for _, cells in rest[1:]):
        cell = rest[0][1][0]
        if _cell_unknown(cell):
            break
        titles.append(_cell_text(cell))
        rest = rest[1:]
    return titles, rest


def _row_is_header(rows):
    if len(rows) < 2:
        return False
    known = [cell for cell in rows[0][1] if not _cell_unknown(cell)]
    if len(known) < 2 or not all(_header_like(_cell_raw(cell), _cell_text(cell)) for cell in known):
        return False
    later = [cell for _, cells in rows[1:] for cell in cells if not _cell_unknown(cell)]
    later_numeric = any(not _header_like(_cell_raw(cell), _cell_text(cell)) for cell in later)
    if later_numeric or len(known) >= 3:
        return True
    widths = [len([cell for cell in cells if not _cell_unknown(cell)]) for _, cells in rows]
    if all(width == 2 for width in widths) and not later_numeric:
        return False
    return False


def _label_map(rows):
    header = [cell for cell in rows[0][1] if not _cell_unknown(cell) and _cell_text(cell)]
    counts = {}
    for cell in header:
        counts[_cell_text(cell)] = counts.get(_cell_text(cell), 0) + 1
    labels = {}
    for cell in header:
        text = _cell_text(cell)
        column = _cell_column(cell)
        labels[_cell_key(cell)] = f'{text}（{column}）' if counts[text] > 1 else text
    return labels


def _fields_for_cells(cells, labels):
    unknown = [_cell_column(cell) for cell in cells if _cell_unknown(cell)]
    known = [cell for cell in cells if not _cell_unknown(cell) and _cell_text(cell)]
    if labels is None and len(known) == 2 and not unknown:
        left, right = known
        field = _Field(
            f'{_cell_text(left)}: {_cell_text(right)}',
            [_cell_column(left), _cell_column(right)],
            [_cell_state(left), _cell_state(right)])
        return [field], unknown
    fields = []
    for cell in known:
        label = labels.get(_cell_key(cell)) if labels else None
        if not label:
            label = _cell_column(cell)
        fields.append(_Field(f'{label}: {_cell_text(cell)}', [_cell_column(cell)], [_cell_state(cell)]))
    return fields, unknown


class _Field:
    def __init__(self, text, columns, states):
        self.text = text
        self.columns = list(columns)
        self.states = list(states)


def _units_from_fields(locator, prefix, fields, unknown, *, row_key, row_value):
    if not fields:
        if not unknown:
            return []
        blank = dict(locator)
        blank['columns'] = list(unknown)
        blank['unknown_columns'] = list(unknown)
        return [_unit(blank, '', formula_state='unknown')]
    packed = _pack_fields(prefix, fields)
    total = len(packed)
    units = []
    for index, (text, columns, states) in enumerate(packed, 1):
        piece = dict(locator)
        piece[row_key] = row_value
        piece['columns'] = columns
        if unknown:
            piece['unknown_columns'] = list(unknown)
        if total > 1:
            piece['part'] = index
            piece['parts'] = total
        formula_state = 'not_recalculated' if 'not_recalculated' in states else ''
        units.append(_unit(piece, text, formula_state=formula_state))
    return units


def _pack_fields(prefix, fields):
    if not fields:
        return []
    label = fields[0]
    chunks = []
    current = [label]
    for field in fields[1:]:
        if _joined_length(prefix, current + [field]) <= MAX_CHARS:
            current.append(field)
            continue
        chunks.append(current)
        if field is not label and _joined_length(prefix, [label, field]) <= MAX_CHARS:
            current = [label, field]
        else:
            current = [field]
    if current:
        chunks.append(current)
    packed = []
    for chunk in chunks:
        packed.extend(_expand_chunk(prefix, chunk))
    return packed


def _joined_length(prefix, fields):
    return len(_render(prefix, fields))


def _render(prefix, fields):
    parts = [part for part in prefix if part]
    parts.extend(field.text for field in fields)
    return ' | '.join(parts)


def _expand_chunk(prefix, fields):
    text = _render(prefix, fields)
    columns = []
    states = []
    for field in fields:
        for column in field.columns:
            if column not in columns:
                columns.append(column)
        states.extend(field.states)
    if len(text) <= MAX_CHARS:
        return [(text, columns, states)]
    head = _render(prefix, fields[:1]) if fields else ' | '.join(part for part in prefix if part)
    pieces = []
    for piece in _pieces(text):
        if head and head not in piece and len(head) + 3 + len(piece) <= MAX_CHARS:
            piece = head + ' | ' + piece
        if len(piece) <= MAX_CHARS:
            pieces.append((piece, columns, states))
            continue
        for smaller in _hard_slices(piece, MAX_CHARS):
            pieces.append((smaller, columns, states))
    return pieces


def _parse_xlsx(blob):
    from openpyxl import load_workbook
    formulas = load_workbook(BytesIO(blob), data_only=False, read_only=True)
    cached = load_workbook(BytesIO(blob), data_only=True, read_only=True)
    merges = _xlsx_merges(blob)
    units = []
    try:
        for name in formulas.sheetnames:
            sheet = formulas[name]
            if getattr(sheet, 'sheet_state', 'visible') != 'visible':
                continue
            values = cached[name]
            units.extend(_xlsx_sheet_units(name, sheet, values, merges.get(name) or []))
    finally:
        formulas.close()
        cached.close()
    return units


def _xlsx_sheet_units(name, sheet, values, merges):
    by_row = {}
    for formula_row, cached_row in zip(sheet.iter_rows(), values.iter_rows()):
        for formula_cell, cached_cell in zip(formula_row, cached_row):
            fact = _xlsx_cell(formula_cell, cached_cell)
            if fact is None:
                continue
            by_row.setdefault(fact.row, {})[fact.column_index] = fact
    _fill_vertical_merges(by_row, merges)
    rows = []
    for row_number in sorted(by_row):
        cells = [by_row[row_number][key] for key in sorted(by_row[row_number])]
        if cells:
            rows.append((row_number, cells))
    if not rows:
        return []
    vertical_starts = {min_row for min_row, _min_col, max_row, _max_col in merges if max_row > min_row}
    titles, rest = _xlsx_titles(rows, vertical_starts)
    if not rest:
        rest = rows
        titles = []
    if _single_column_prose(rest):
        return _xlsx_prose_units(name, titles, rest)
    labels = _label_map(rest) if _row_is_header(rest) else None
    data = rest[1:] if labels else rest
    if labels and not data:
        data = rest
        labels = None
    prefix = [f'工作表{name}', *titles]
    units = []
    for row_number, cells in data:
        fields, unknown = _fields_for_cells(cells, labels)
        locator = {
            'kind': 'xlsx_rows',
            'sheet': name,
            'row_from': row_number,
            'row_to': row_number,
        }
        units.extend(_units_from_fields(
            locator, prefix, fields, unknown, row_key='row_from', row_value=row_number))
    return units


def _xlsx_titles(rows, vertical_starts):
    titles = []
    rest = list(rows)
    while rest and rest[0][0] not in vertical_starts and len(rest[0][1]) == 1 and any(len(cells) >= 2 for _, cells in rest[1:]):
        cell = rest[0][1][0]
        if _cell_unknown(cell):
            break
        titles.append(_cell_text(cell))
        rest = rest[1:]
    return titles, rest


def _single_column_prose(rows):
    return len(rows) >= 2 and all(
        len(cells) == 1 and not _cell_unknown(cells[0]) for _, cells in rows)


def _xlsx_prose_units(name, titles, rows):
    prefix = ' | '.join(part for part in [f'工作表{name}', *titles] if part)
    batches = []
    current = []

    def render(batch):
        body = '\n'.join(_cell_text(cells[0]) for _, cells in batch)
        return f'{prefix}\n{body}' if prefix else body

    for row in rows:
        trial = current + [row]
        if current and len(render(trial)) > MAX_CHARS:
            batches.append(current)
            current = [row]
        else:
            current = trial
    if current:
        batches.append(current)
    units = []
    for batch in batches:
        text = render(batch)
        columns = []
        for _, cells in batch:
            column = _cell_column(cells[0])
            if column not in columns:
                columns.append(column)
        locator = {
            'kind': 'xlsx_rows',
            'sheet': name,
            'row_from': batch[0][0],
            'row_to': batch[-1][0],
            'columns': columns,
        }
        _append_text_units(units, locator, text)
    return units


def _fill_vertical_merges(by_row, merges):
    for min_row, min_col, max_row, _max_col in merges:
        if max_row <= min_row:
            continue
        source = by_row.get(min_row, {}).get(min_col)
        if source is None or source.formula_state == 'unknown':
            continue
        for row_number in range(min_row + 1, max_row + 1):
            existing = by_row.get(row_number)
            if not existing or min_col in existing:
                continue
            existing[min_col] = source._replace(row=row_number)


def _xlsx_cell(formula_cell, cached_cell):
    raw = formula_cell.value
    if raw is None:
        return None
    formula = isinstance(raw, str) and raw.startswith('=')
    cached = cached_cell.value
    column = formula_cell.column_letter
    column_index = formula_cell.column
    row = formula_cell.row
    if formula and _missing_cached(cached):
        return _XlsxCell(row, column_index, column, '', 'unknown', None)
    if formula:
        text = _plain(cached)
        if not text:
            return _XlsxCell(row, column_index, column, '', 'unknown', cached)
        return _XlsxCell(row, column_index, column, text, 'not_recalculated', cached)
    text = _plain(raw)
    if not text:
        return None
    return _XlsxCell(row, column_index, column, text, '', raw)


def _missing_cached(value):
    if value is None:
        return True
    return isinstance(value, str) and (not value.strip() or value.startswith('='))


def _plain(value):
    from datetime import date, datetime
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, datetime):
        return value.isoformat(sep=' ')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


class _XlsxCell:
    def __init__(self, row, column_index, column, text, formula_state, raw):
        self.row = row
        self.column_index = column_index
        self.column = column
        self.text = text
        self.formula_state = formula_state
        self.raw = raw

    def _replace(self, **changes):
        values = {
            'row': self.row, 'column_index': self.column_index, 'column': self.column,
            'text': self.text, 'formula_state': self.formula_state, 'raw': self.raw,
        }
        values.update(changes)
        return _XlsxCell(**values)


_XLSX_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_XLSX_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _xlsx_merges(blob):
    """只读模式不提供合并区域。从压缩包读 mergeCell，失败时退回不合并。"""
    try:
        from openpyxl.utils.cell import range_boundaries
        with zipfile.ZipFile(BytesIO(blob)) as package:
            workbook = ET.fromstring(package.read('xl/workbook.xml'))
            rels = ET.fromstring(package.read('xl/_rels/workbook.xml.rels'))
            targets = {}
            for rel in rels:
                identity = rel.attrib.get('Id')
                target = rel.attrib.get('Target')
                if identity and target:
                    targets[identity] = target
            found = {}
            for sheet in workbook.findall(f'{{{_XLSX_MAIN}}}sheets/{{{_XLSX_MAIN}}}sheet'):
                name = sheet.attrib.get('name')
                identity = sheet.attrib.get(f'{{{_XLSX_REL}}}id')
                target = targets.get(identity, '')
                path = _worksheet_path(target)
                spans = []
                try:
                    root = ET.fromstring(package.read(path))
                except KeyError:
                    found[name] = []
                    continue
                for cell in root.findall(f'{{{_XLSX_MAIN}}}mergeCells/{{{_XLSX_MAIN}}}mergeCell'):
                    ref = cell.attrib.get('ref')
                    if not ref:
                        continue
                    min_col, min_row, max_col, max_row = range_boundaries(ref)
                    spans.append((min_row, min_col, max_row, max_col))
                found[name] = spans
            return found
    except (ET.ParseError, KeyError, OSError, ValueError, zipfile.BadZipFile):
        return {}


def _worksheet_path(target):
    target = (target or '').replace('\\', '/').lstrip('/')
    if target.startswith('xl/'):
        return target
    return 'xl/' + target


def _cell_unknown(cell):
    if isinstance(cell, _XlsxCell):
        return cell.formula_state == 'unknown'
    return False


def _cell_text(cell):
    if isinstance(cell, _XlsxCell):
        return cell.text
    return cell[1]


def _cell_raw(cell):
    if isinstance(cell, _XlsxCell):
        return cell.raw
    return cell[1]


def _cell_column(cell):
    if isinstance(cell, _XlsxCell):
        return cell.column
    return cell[0]


def _cell_key(cell):
    if isinstance(cell, _XlsxCell):
        return cell.column_index
    return cell[0]


def _cell_state(cell):
    if isinstance(cell, _XlsxCell):
        return cell.formula_state
    return ''


def _parse_pptx(blob):
    from pptx import Presentation
    deck = Presentation(BytesIO(blob))
    units = []
    for slide_index, slide in enumerate(deck.slides, 1):
        shapes = []
        for shape_index, shape in enumerate(slide.shapes, 1):
            blocks = _shape_blocks(shape)
            if blocks:
                shapes.append((shape_index, blocks))
        if not shapes:
            continue
        slide_text = '\n'.join(text for _, blocks in shapes for _, text in blocks)
        if len(slide_text) <= MAX_CHARS:
            units.append(_unit({
                'kind': 'pptx_slide',
                'slide': slide_index,
                'shape_from': shapes[0][0],
                'shape_to': shapes[-1][0],
            }, slide_text))
            continue
        for shape_index, blocks in shapes:
            units.extend(_pack_shape(slide_index, shape_index, blocks))
    return units


def _shape_blocks(shape):
    blocks = []
    if getattr(shape, 'has_text_frame', False):
        for index, paragraph in enumerate(shape.text_frame.paragraphs, 1):
            text = paragraph.text.strip()
            if text:
                blocks.append((index, text))
    if getattr(shape, 'has_table', False):
        start = blocks[-1][0] + 1 if blocks else 1
        for offset, row in enumerate(shape.table.rows):
            seen = []
            values = []
            for cell in row.cells:
                if any(cell is previous for previous in seen):
                    continue
                seen.append(cell)
                text = cell.text.strip()
                if text:
                    values.append(text)
            if values:
                blocks.append((start + offset, ' | '.join(values)))
    return blocks


def _pack_shape(slide_index, shape_index, blocks):
    units = []
    current = []

    def emit(batch):
        if not batch:
            return
        text = '\n'.join(item for _, item in batch)
        locator = {
            'kind': 'pptx_shape',
            'slide': slide_index,
            'shape': shape_index,
            'paragraph_from': batch[0][0],
            'paragraph_to': batch[-1][0],
        }
        _append_text_units(units, locator, text)

    for index, text in blocks:
        if len(text) > MAX_CHARS:
            emit(current)
            current = []
            pieces = _pieces(text)
            total = len(pieces)
            for part, piece in enumerate(pieces, 1):
                locator = {
                    'kind': 'pptx_shape',
                    'slide': slide_index,
                    'shape': shape_index,
                    'paragraph_from': index,
                    'paragraph_to': index,
                }
                if total > 1:
                    locator['part'] = part
                    locator['parts'] = total
                units.append(_unit(locator, piece))
            continue
        prospective = current + [(index, text)]
        if current and len('\n'.join(item for _, item in prospective)) > MAX_CHARS:
            emit(current)
            current = [(index, text)]
        else:
            current = prospective
    emit(current)
    return units


def _unit(locator, text, *, formula_state='', status='succeeded', error_code=''):
    return {
        'locator': locator, 'locator_key': _key(locator), 'text': text,
        'formula_state': formula_state, 'status': status, 'error_code': error_code,
    }


class OfficeIndex:
    def __init__(self, settings, database, models):
        self.settings = settings
        self.database = database
        self.models = models

    def version(self):
        return office_version(self.models.profile())

    def index(self, project_id, document_id, *, image_recognizer=None):
        from .retrieval import RetrievalError, valid_vector
        from .docx_images import IMAGE_PLACEHOLDER, extract_docx_images
        project_id, document_id = UUID(str(project_id)), UUID(str(document_id))
        document = self.database.document(project_id, document_id)
        if document is None:
            raise RetrievalError('材料不存在或不属于当前项目。')
        legacy = next((name for name in document['filenames'] if _extension(name) in _LEGACY), '')
        modern = next((name for name in document['filenames'] if _extension(name) in _PACKAGE), '')
        from .storage import Storage
        stored = Storage(self.settings.data_dir, self.settings.max_upload_bytes).path(document['storage_key'])
        # 同一内容若同时有现代扩展名和旧扩展名，以文件头决定走哪条解析，避免把 docx 误判成旧版后拒绝。
        if legacy and modern:
            with stored.open('rb') as handle:
                head = handle.read(8)
            if head != bytes.fromhex('D0CF11E0A1B11AE1'):
                legacy = ''
        conversion = None
        conversion_reason = ''
        if legacy:
            self._phase(project_id, document_id, 'converting')
            from .legacy_office import convert_for_parse
            prepared = convert_for_parse(self.settings, stored, _extension(legacy))
            if not prepared['ok']:
                self._reject(project_id, document_id, prepared)
                return {
                    'indexed_units': [], 'failed_units': [], 'index_version': self.version(),
                    'unsupported': True, 'error_code': prepared['error_code'], 'reason': prepared['reason'],
                    'outcome': 'failed', 'conversion': prepared['conversion'],
                }
            filename = 'converted' + prepared['target_extension']
            blob = prepared['blob']
            conversion = prepared['conversion']
            conversion_reason = prepared['reason']
        elif modern:
            filename = modern
            blob = stored.read_bytes()
        else:
            raise RetrievalError('这份材料不是可解析的 Office 文件。')
        self._phase(project_id, document_id, 'parsing')
        try:
            units = parse_office(filename, blob)
            images = extract_docx_images(blob) if _extension(filename) == '.docx' else []
        except Exception:
            if not legacy:
                raise
            rejection = {
                'status': 'invalid',
                'reason': f'{_LEGACY[_extension(legacy)]} 的转换结果无法解析，未入库。',
                'error_code': 'legacy_output_rejected',
                'conversion': dict(conversion, outcome='legacy_output_rejected'),
            }
            self._reject(project_id, document_id, rejection)
            return {
                'indexed_units': [], 'failed_units': [], 'index_version': self.version(),
                'unsupported': True, 'error_code': rejection['error_code'], 'reason': rejection['reason'],
                'outcome': 'failed', 'conversion': rejection['conversion'],
            }
        if images:
            self._phase(project_id, document_id, 'recognizing')
            recognized = self._recognize_images(images, image_recognizer)
            for image, result in zip(images, recognized, strict=True):
                if not result.get('ok'):
                    units.append(_unit(image['locator'], '', status='failed', error_code=result.get('error_code') or 'image_ocr_failed'))
                    continue
                text = (result.get('text') or '').strip()
                if text == IMAGE_PLACEHOLDER:
                    units.append(_unit(image['locator'], '', status='failed', error_code='image_not_recognized'))
                    continue
                units.append(_unit(image['locator'], text, status='succeeded'))
        self._phase(project_id, document_id, 'indexing')
        units = _bound_units(units)
        version = self.version()
        failed = []
        indexed = []
        for unit in units:
            if unit['text'].strip() == IMAGE_PLACEHOLDER:
                unit['status'] = 'failed'
                unit['error_code'] = unit['error_code'] or 'image_not_recognized'
                unit['text'] = ''
            unit_id = self._store_unit(document_id, unit)
            if unit['status'] != 'succeeded' or not _has_index_text(unit['text']) or unit['formula_state'] == 'unknown':
                self._clear_chunks(unit_id, version)
                if unit['status'] != 'succeeded':
                    failed.append(unit['locator'])
                continue
            if self._reuse(unit_id, version, unit['text']):
                indexed.append(unit['locator'])
                continue
            try:
                vector = valid_vector(self.models.embed([unit['text']])[0])
            except Exception:
                failed.append(unit['locator'])
                self._mark_failed(unit_id)
                unit['status'] = 'failed'
                unit['error_code'] = 'embed_failed'
                self._clear_chunks(unit_id, version)
                continue
            self._publish_chunk(document_id, unit_id, version, unit, vector)
            indexed.append(unit['locator'])
        self._drop_stale(document_id, [unit['locator_key'] for unit in units])
        indexed_keys = {_key(locator) for locator in indexed}
        image_units = [unit for unit in units if unit['locator'].get('kind') == 'docx_image']
        counts = _counts(units, indexed_keys, version_known=True)
        outcome = terminal_office_outcome(
            unit_failed=counts['unit_failed'], image_failed=counts['image_failed'],
            indexable=counts['indexable'], indexed=counts['indexed'], version_known=True)
        reason = describe_office_outcome(
            outcome, conversion_reason=conversion_reason, had_images=bool(image_units),
            unit_total=counts['unit_total'],
            recognized_images=any(unit['status'] == 'succeeded' for unit in image_units),
            image_failed=counts['image_failed'])
        self._set_disposition(project_id, document_id, 'registered', reason)
        result = {
            'indexed_units': indexed, 'failed_units': failed, 'index_version': version,
            'outcome': outcome, 'reason': reason,
            'error_code': _outcome_error(outcome),
        }
        if conversion is not None:
            result['conversion'] = conversion
        return result

    def _recognize_images(self, images, recognizer):
        if recognizer is None:
            from .docx_images import recognize_docx_images
            return recognize_docx_images(self.settings, images)
        results = []
        for image in images:
            if image.get('error_code') or not image.get('bytes'):
                results.append({'ok': False, 'error_code': image.get('error_code') or 'image_extract_failed'})
                continue
            try:
                result = recognizer(image)
            except Exception:
                results.append({'ok': False, 'error_code': 'image_ocr_failed'})
                continue
            if not isinstance(result, dict) or not result.get('ok'):
                code = result.get('error_code') if isinstance(result, dict) else ''
                results.append({'ok': False, 'error_code': code or 'image_ocr_failed'})
                continue
            results.append({'ok': True, 'text': str(result.get('text') or '')})
        return results

    def _phase(self, project_id, document_id, phase):
        self._set_disposition(project_id, document_id, 'registered', _PHASE_REASONS[phase])
        with self.database.connect() as conn:
            conn.execute('''UPDATE dd_agent.processing_jobs
                SET progress = COALESCE(progress, '{}'::jsonb) || %s::jsonb, updated_at=clock_timestamp()
                WHERE project_id=%s AND document_id=%s AND status IN ('running', 'pause_requested')''',
                (Jsonb({'phase': phase}), project_id, document_id))

    def _drop_stale(self, document_id, keys):
        # 重新解析后，旧的占位图片单元不能继续留在当前索引里。
        with self.database.connect() as conn:
            if keys:
                conn.execute('''DELETE FROM dd_agent.source_units
                    WHERE document_id=%s AND parse_version=%s AND NOT (locator_key = ANY(%s::text[]))''',
                    (document_id, PARSE_VERSION, keys))
            else:
                conn.execute('''DELETE FROM dd_agent.source_units
                    WHERE document_id=%s AND parse_version=%s''', (document_id, PARSE_VERSION))

    def _set_disposition(self, project_id, document_id, status, reason):
        with self.database.connect() as conn:
            row = conn.execute('''UPDATE dd_agent.documents SET status=%s, reason=%s
                WHERE project_id=%s AND id=%s RETURNING id''', (status, reason, project_id, document_id)).fetchone()
        if row is None:
            from .retrieval import RetrievalError
            raise RetrievalError('材料不存在或不属于当前项目。')

    def _reject(self, project_id, document_id, rejection):
        # 转换失败时撤下已发布的来源单元，避免旧向量继续被当前项目检索到。只动这一份材料。
        with self.database.connect() as conn:
            conn.execute('''DELETE FROM dd_agent.source_units u USING dd_agent.documents d
                WHERE u.document_id=d.id AND d.project_id=%s AND d.id=%s''', (project_id, document_id))
            row = conn.execute('''UPDATE dd_agent.documents SET status=%s, reason=%s
                WHERE project_id=%s AND id=%s RETURNING id''',
                (rejection['status'], rejection['reason'], project_id, document_id)).fetchone()
        if row is None:
            from .retrieval import RetrievalError
            raise RetrievalError('材料不存在或不属于当前项目。')

    def _store_unit(self, document_id, unit):
        with self.database.connect() as conn:
            row = conn.execute('''INSERT INTO dd_agent.source_units
                (id,document_id,locator,locator_key,text,formula_state,parse_version,status,error_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (document_id, locator_key, parse_version) DO UPDATE SET
                  text=EXCLUDED.text, formula_state=EXCLUDED.formula_state,
                  status=EXCLUDED.status, error_code=EXCLUDED.error_code, locator=EXCLUDED.locator
                RETURNING id''', (
                    uuid4(), document_id, Jsonb(unit['locator']), unit['locator_key'], unit['text'],
                    unit['formula_state'], PARSE_VERSION, unit['status'], unit['error_code'])).fetchone()
        return row['id']

    def _reuse(self, unit_id, version, text):
        with self.database.connect() as conn:
            row = conn.execute('''SELECT text FROM dd_agent.source_unit_chunks
                WHERE unit_id=%s AND index_version=%s AND ordinal=0''', (unit_id, version)).fetchone()
        return row is not None and row['text'] == text

    def _clear_chunks(self, unit_id, version):
        with self.database.connect() as conn:
            conn.execute('DELETE FROM dd_agent.source_unit_chunks WHERE unit_id=%s AND index_version=%s', (unit_id, version))

    def _mark_failed(self, unit_id):
        with self.database.connect() as conn:
            conn.execute("UPDATE dd_agent.source_units SET status='failed', error_code='embed_failed' WHERE id=%s", (unit_id,))

    def _publish_chunk(self, document_id, unit_id, version, unit, vector):
        with self.database.connect() as conn:
            conn.execute('DELETE FROM dd_agent.source_unit_chunks WHERE unit_id=%s AND index_version=%s', (unit_id, version))
            conn.execute('''INSERT INTO dd_agent.source_unit_chunks
                (id,unit_id,document_id,index_version,ordinal,text,embedding,lexemes,locator)
                VALUES (%s,%s,%s,%s,0,%s,%s::public.vector,%s::tsvector,%s)''', (
                    uuid4(), unit_id, document_id, version, unit['text'], vector,
                    document_tsvector(unit['text']), Jsonb(unit['locator'])))

    def candidates(self, project_id, version, *, vector=None, tsquery=None, needles=(), included=None, limit=12, scope=None):
        from .material_metadata import metadata_sql
        clause = ''
        args = [project_id, version]
        if included is not None:
            clause = ' AND c.document_id=ANY(%s::uuid[])'
            args.append(list(included))
        meta, meta_args = metadata_sql(scope, 'd')
        clause += meta
        args.extend(meta_args)
        # 来源单元没有物理页码。设了页范围就整段排除，不用定位里的幻灯片号或行号冒充页码。
        if scope is not None and scope.page_from is not None:
            clause += ' AND FALSE'
        rows = []
        with self.database.connect() as conn:
            if vector is not None:
                rows.extend(conn.execute('''SELECT c.id, c.unit_id, c.document_id, c.text, c.locator,
                    u.formula_state, 1-(c.embedding <=> %s::public.vector) AS similarity
                    FROM dd_agent.source_unit_chunks c
                    JOIN dd_agent.source_units u ON u.id=c.unit_id AND u.status='succeeded'
                    JOIN dd_agent.documents d ON d.id=c.document_id
                    WHERE d.project_id=%s AND c.index_version=%s''' + clause + '''
                    ORDER BY c.embedding <=> %s::public.vector, c.id LIMIT %s''',
                    [vector, *args, vector, limit]).fetchall())
            if tsquery:
                exact = ('(' + ' OR '.join(['strpos(lower(c.text),%s)>0'] * len(needles)) + ') DESC,') if needles else ''
                rows.extend(conn.execute('''WITH q AS (SELECT %s::tsquery AS tsq)
                    SELECT c.id, c.unit_id, c.document_id, c.text, c.locator, u.formula_state,
                      ts_rank_cd(c.lexemes,q.tsq) AS keyword_score
                    FROM q JOIN dd_agent.source_unit_chunks c ON c.lexemes @@ q.tsq
                    JOIN dd_agent.source_units u ON u.id=c.unit_id AND u.status='succeeded'
                    JOIN dd_agent.documents d ON d.id=c.document_id
                    WHERE d.project_id=%s AND c.index_version=%s''' + clause + '''
                    ORDER BY ''' + exact + ''' ts_rank_cd(c.lexemes,q.tsq) DESC, c.id LIMIT %s''',
                    [tsquery, *args, *needles, limit]).fetchall())
        hits = []
        for row in rows:
            locator = row['locator']
            hits.append(dict(row, index_kind='source_unit', locator_label=locator_label(locator, row['formula_state']),
                             similarity=row.get('similarity') or 0))
        return hits

    def count_chunks(self, project_id, version):
        with self.database.connect() as conn:
            row = conn.execute('''SELECT count(*) AS n FROM dd_agent.source_unit_chunks c
                JOIN dd_agent.documents d ON d.id=c.document_id
                WHERE d.project_id=%s AND c.index_version=%s''', (project_id, version)).fetchone()
        return row['n']

    def read(self, project_id, document_id, unit_id, index_version):
        project_id, document_id, unit_id = UUID(str(project_id)), UUID(str(document_id)), UUID(str(unit_id))
        with self.database.connect() as conn:
            row = conn.execute('''SELECT u.id, u.document_id, u.locator, u.text, u.formula_state, c.index_version
                FROM dd_agent.source_units u
                JOIN dd_agent.documents d ON d.id=u.document_id
                LEFT JOIN dd_agent.source_unit_chunks c ON c.unit_id=u.id AND c.index_version=%s
                WHERE d.project_id=%s AND d.id=%s AND u.id=%s''',
                (index_version, project_id, document_id, unit_id)).fetchone()
        if row is None:
            return None
        return dict(row, locator_label=locator_label(row['locator'], row['formula_state']))


_OFFICE_TRIM = r"btrim(u.text, E' \t\n\r\f\v　')"


def annotate_office_outcomes(database, items, office_version):
    """给材料清单补上 Office 完成口径。PDF 保持原字段，不在这里改页状态。"""
    rows = [dict(item) for item in items]
    office_ids = [row['document_id'] for row in rows if any(_extension(name) for name in (row.get('filenames') or []))]
    stats = _catalog_stats(database, office_ids, office_version) if office_ids else {}
    phases = _catalog_phases(database, office_ids) if office_ids else {}
    version_known = bool(office_version)
    for row in rows:
        if not any(_extension(name) for name in (row.get('filenames') or [])):
            row['office_outcome'] = None
            continue
        stat = stats.get(row['document_id']) or {
            'unit_total': 0, 'unit_failed': 0, 'image_failed': 0, 'indexable': 0, 'indexed': 0,
        }
        phase = phases.get(row['document_id']) or ''
        outcome = display_office_outcome(
            document_status=row.get('status'), reason=row.get('reason') or '',
            job_status=row.get('processing_status'), job_phase=phase,
            unit_total=stat['unit_total'], unit_failed=stat['unit_failed'],
            image_failed=stat['image_failed'], indexable=stat['indexable'],
            indexed=stat['indexed'], version_known=version_known)
        row['office_outcome'] = outcome
        row['reason'] = display_office_reason(outcome, row.get('reason') or '')
    return rows


def _catalog_stats(database, document_ids, office_version):
    trimmed = _OFFICE_TRIM
    with database.connect() as conn:
        found = conn.execute(f'''SELECT u.document_id,
            count(*)::int AS unit_total,
            count(*) FILTER (WHERE u.status <> 'succeeded')::int AS unit_failed,
            count(*) FILTER (WHERE u.status <> 'succeeded' AND u.locator->>'kind' = 'docx_image')::int AS image_failed,
            count(*) FILTER (WHERE u.status = 'succeeded' AND u.formula_state <> 'unknown'
                AND {trimmed} <> '')::int AS indexable,
            count(*) FILTER (WHERE u.status = 'succeeded' AND u.formula_state <> 'unknown'
                AND {trimmed} <> '' AND %s::text IS NOT NULL AND EXISTS (
                    SELECT 1 FROM dd_agent.source_unit_chunks c
                    WHERE c.unit_id = u.id AND c.index_version = %s AND c.text = u.text
                ))::int AS indexed
            FROM dd_agent.source_units u
            WHERE u.document_id = ANY(%s::uuid[])
            GROUP BY u.document_id''', (office_version, office_version, list(document_ids))).fetchall()
    return {row['document_id']: row for row in found}


def _catalog_phases(database, document_ids):
    with database.connect() as conn:
        found = conn.execute('''SELECT DISTINCT ON (document_id) document_id, COALESCE(progress->>'phase', '') AS phase
            FROM dd_agent.processing_jobs
            WHERE document_id = ANY(%s::uuid[])
            ORDER BY document_id, created_at DESC, id DESC''', (list(document_ids),)).fetchall()
    return {row['document_id']: row['phase'] for row in found}
