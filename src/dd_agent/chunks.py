"""按已有文字和 OCR 行框切片。不加载版面模型。

PaddleOCR 入库时已经保存行文字和四点坐标。条款、标题、列表和表格
从这些行以及换行、制表符里识别。GTX 1070 上再跑 PP-Structure 会多占一份
显存，300 页扫描件会在 OCR 之外再过一遍版面网络，因此不采用。
"""
import re

from .performance import count, timed


MAX_CHARS = 700
_HEADING = re.compile(
    r'^(?:第[零〇一二三四五六七八九十百千0-9]{1,12}条'
    r'|第[零〇一二三四五六七八九十百千0-9]{1,8}[章节篇]'
    r'|[（(][零〇一二三四五六七八九十0-9]{1,3}[）)]'
    r'|[一二三四五六七八九十]{1,3}、'
    r'|\d{1,2}[.、])'
)
_SENTENCE = '。！？；\n'
_HARD = 80


def _line_offsets(text):
    rows = []
    cursor = 0
    for line in text.split('\n'):
        rows.append((cursor, cursor + len(line), line))
        cursor += len(line) + 1
    return rows


def _mapped_lines(text, lines):
    """行框只有在能逐字拼回当前正文时才使用。视觉改写后的正文退回纯文本规则。"""
    if not lines:
        return None
    try:
        joined = '\n'.join(str(line.get('text', '')) for line in lines)
    except (AttributeError, TypeError):
        return None
    if joined != text:
        return None
    mapped = []
    cursor = 0
    for line in lines:
        raw = str(line.get('text', ''))
        mapped.append((cursor, cursor + len(raw), line))
        cursor += len(raw) + 1
    return mapped


def _box_y(line):
    box = line.get('box') if isinstance(line, dict) else None
    if not box:
        return None
    try:
        return sum(float(point[1]) for point in box) / len(box)
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


def _box_height(line):
    box = line.get('box') if isinstance(line, dict) else None
    if not box:
        return None
    try:
        ys = [float(point[1]) for point in box]
        return max(ys) - min(ys) or None
    except (TypeError, ValueError, IndexError):
        return None


def _table_line(line):
    if '\t' in line or line.count('|') >= 2:
        return True
    return re.search(r'\S(?: {2,}|\t)\S', line) is not None


def _kind(fragment):
    head = fragment.lstrip()
    if re.match(r'^第[零〇一二三四五六七八九十百千0-9]{1,12}条', head):
        return 'clause'
    if re.match(r'^(?:[（(][零〇一二三四五六七八九十0-9]{1,3}[）)]|\d{1,2}[.、])', head):
        return 'list'
    if _HEADING.match(head):
        return 'heading'
    lines = [line for line in fragment.split('\n') if line.strip()]
    if lines and all(_table_line(line) for line in lines):
        return 'table'
    return 'paragraph'


def _cuts(text, lines):
    cuts = {0, len(text)}
    for match in re.finditer(r'\n[ \t]*\n+', text):
        cuts.add(match.end())
    for match in re.finditer(r'(?m)^(?=\S)', text):
        if _HEADING.match(text[match.end():]):
            cuts.add(match.end())
    rows = _line_offsets(text)
    table = False
    for start, _end, line in rows:
        now = _table_line(line)
        if now != table:
            cuts.add(start)
        table = now
    mapped = _mapped_lines(text, lines)
    if mapped and len(mapped) > 1:
        for (start, _end, line), (next_start, _next_end, nxt) in zip(mapped, mapped[1:]):
            y1, y2 = _box_y(line), _box_y(nxt)
            height = _box_height(line)
            if y1 is None or y2 is None or not height:
                continue
            if y2 - y1 > height * 1.8:
                cuts.add(next_start)
    return sorted(cuts)


def _blocks(text, lines):
    blocks = []
    ordered = _cuts(text, lines)
    for left, right in zip(ordered, ordered[1:]):
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if right > left:
            blocks.append((left, right, _kind(text[left:right])))
    return blocks


def _boundary(text, start, limit, kind):
    if limit >= len(text) or limit <= start:
        return limit
    window = text[start:limit]
    if kind == 'table':
        mark = window.rfind('\n')
        return start + mark + 1 if mark > 0 else limit
    best = -1
    for mark in _SENTENCE:
        found = window.rfind(mark)
        if found > best:
            best = found
    if best >= max(40, len(window) // 3):
        return start + best + 1
    return limit


def _pack(text, start, end, kind):
    chunks = []
    cursor = start
    while cursor < end:
        while cursor < end and text[cursor].isspace():
            cursor += 1
        if cursor >= end:
            break
        limit = min(cursor + MAX_CHARS, end)
        stop = end if limit == end else _boundary(text, cursor, limit, kind)
        if stop <= cursor:
            stop = min(cursor + MAX_CHARS, end)
        trimmed = stop
        while trimmed > cursor and text[trimmed - 1].isspace():
            trimmed -= 1
        if trimmed <= cursor:
            cursor = stop
            continue
        chunks.append(dict(text=text[cursor:trimmed], start_offset=cursor, end_offset=trimmed, kind=kind))
        if stop >= end:
            break
        hard = kind == 'paragraph' and stop < end and text[stop - 1] not in _SENTENCE
        cursor = max(cursor + 1, stop - _HARD) if hard else stop
    return chunks


@timed('chunk')
def split_text(text, lines=None):
    """把一页切成不超过 700 字、且等于原文切片的片段。"""
    if not text or not str(text).strip():
        return []
    chunks = []
    for start, end, kind in _blocks(text, lines):
        chunks.extend(_pack(text, start, end, kind))
    count('chunks.created', len(chunks))
    return chunks


def _continues(left, right):
    if not left['spans'] or not right['spans']:
        return False
    if right['spans'][0]['page_number'] != left['spans'][-1]['page_number'] + 1:
        return False
    if len(left['text']) + 1 + len(right['text']) > MAX_CHARS:
        return False
    if left['text'].endswith(('。', '！', '？', '!', '?')):
        return False
    if _HEADING.match(right['text'].lstrip()):
        return False
    if left.get('kind') == 'table' or right.get('kind') == 'table':
        return left.get('kind') == right.get('kind')
    return True


def _merge(left, right):
    return dict(text=left['text'] + '\n' + right['text'], start_offset=left['start_offset'],
                end_offset=left['end_offset'], kind=left.get('kind') or 'paragraph',
                spans=list(left['spans']) + list(right['spans']))


def layout_chunks(pages):
    """同一批连续页里，未收束的段落或表格可以接到下一页。返回按页分组的片段。"""
    owned = {}
    previous = None
    for page in pages:
        number = page['page_number']
        owned[number] = []
        for piece in split_text(page.get('text') or '', page.get('lines')):
            chunk = dict(piece, spans=[dict(page_number=number, start=piece['start_offset'], end=piece['end_offset'])])
            if previous and _continues(previous, chunk):
                merged = _merge(previous, chunk)
                owner = merged['spans'][0]['page_number']
                owned[owner][-1] = merged
                previous = merged
            else:
                owned[number].append(chunk)
                previous = chunk
    return owned
