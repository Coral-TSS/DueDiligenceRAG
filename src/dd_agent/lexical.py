"""词典无关的中文精确词索引，写入 PostgreSQL 自带 tsvector。

法律尽调里要稳定命中的是公司名、人名、统一社会信用代码、金额、日期和条款编号。
这些都是原文里的连续片段，不依赖分词词典。内置全文解析器会把一整段中文收成
一个词，因此这里在 Python 里生成词项，再按 tsvector 字面量入库，用 GIN 检索。

- 连续汉字生成相邻二字组，两字及以上的人名、公司名都能被检索式覆盖。
- 六位及以上的字母数字串原样作为标识，社会信用代码不用拆开。
- 金额、日期、条款编号另存一条规范词，第十二条和第 12 条、2024年3月15日和
  2024-03-15 能对上。
入库只多一次字符串处理，不加载嵌入模型，也不需要 zhparser、pg_jieba 或 jieba。
"""
import math
import re
import unicodedata


LEXICAL_VERSION = 'cjk-bigram-exact-v1'
_MAX_TOKENS = 16000
_MAX_GROUPS = 16
_MAX_NEEDLES = 8

_CN_DIGIT = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_UNIT = {'十': 10, '百': 100, '千': 1000}
# 资本、基本都含“本”，不能拿来断开词。问句里的的／是／多少仍会切开。
_STOP_CHARS = set('的了是和与及或在为等对将把被而但从向到以于按这那其该各也又就都还吗呢吧')
_MULTI_STOPS = ('请问', '查询', '查找', '关于', '如何', '什么', '是否', '哪些', '哪个', '多少', '以及', '根据', '按照', '为了', '如果', '因为', '所以', '但是', '不过', '然而', '并且', '或者')
_PUNCT = re.compile(r'[\s,，.。;；:：?？!！、（）()【】\[\]《》<>“”"\'‘’「」·—]+')
_CJK_RUN = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]+')
_IDENT = re.compile(r'[a-z0-9]{6,}')
_COMPACT = re.compile(r'[a-z0-9]{2,}(?:[-_/][a-z0-9]{2,})+')
_AMOUNT = re.compile(r'(?:人民币|rmb|cny|¥|￥)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(亿元|万元|亿|万|元|%)')
_DATE_CN = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?')
_DATE_SEP = re.compile(r'(?<![0-9])(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?![0-9])')
_DATE_HAN = re.compile(r'([零〇一二三四五六七八九]{4})\s*年\s*([零〇一二三四五六七八九十两]{1,3})\s*月(?:\s*([零〇一二三四五六七八九十两]{1,3})\s*日)?')
_CLAUSE = re.compile(r'第\s*([0-9]{1,4}|[零〇一二三四五六七八九十百千两]{1,12})\s*条')
_AMOUNT_SUFFIX = {'亿元': 'yi', '亿': 'yi', '万元': 'wan', '万': 'wan', '元': 'yuan', '%': 'pct'}


def normalize(text):
    return unicodedata.normalize('NFKC', text or '').lower()


def cn_to_int(text):
    """条款和中文日期用。纯数字串按位读，含十百千时按位值读。"""
    if not text or any(ch not in _CN_DIGIT and ch not in _CN_UNIT for ch in text):
        return None
    if not any(ch in _CN_UNIT for ch in text):
        return int(''.join(str(_CN_DIGIT[ch]) for ch in text))
    total = 0
    current = 0
    for ch in text:
        if ch in _CN_UNIT:
            total += (current or 1) * _CN_UNIT[ch]
            current = 0
        else:
            current = _CN_DIGIT[ch]
    return total + current


def quote_lexeme(token):
    if not token or any(ord(ch) < 32 for ch in token):
        raise ValueError('词项无效。')
    return "'" + token.replace('\\', '\\\\').replace("'", "''") + "'"


def _date_token(year, month, day):
    if not 1000 <= year <= 2999 or not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None
    month_token = f'date_{year:04d}{month:02d}'
    if day is None:
        return month_token, None
    return month_token, f'date_{year:04d}{month:02d}{day:02d}'


def _amount_token(whole, fraction, unit):
    number = whole.replace(',', '')
    if fraction:
        fraction = fraction.rstrip('0')
        if fraction:
            number += 'p' + fraction
    return f"amt_{number}{_AMOUNT_SUFFIX[unit]}"


def identifier_tokens(norm):
    found = []
    seen = set()
    for match in _IDENT.finditer(norm):
        token = match.group()
        if token not in seen:
            seen.add(token)
            found.append((match.start(), token))
    for match in _COMPACT.finditer(norm):
        compact = re.sub(r'[-_/]', '', match.group())
        if len(compact) >= 6 and compact not in seen:
            seen.add(compact)
            found.append((match.start(), compact))
    return found


def canonical_tokens(norm, *, query=False):
    """规范词。查询只保留用户写出的精度，文档则同时留下可被更短查询命中的月份词。"""
    found = []
    for match in _AMOUNT.finditer(norm):
        found.append((match.start(), _amount_token(match.group(1), match.group(2), match.group(3))))
    for pattern in (_DATE_CN, _DATE_SEP):
        for match in pattern.finditer(norm):
            year, month = int(match.group(1)), int(match.group(2))
            day = int(match.group(3)) if match.group(3) else None
            month_token, day_token = _date_token(year, month, day) or (None, None)
            if month_token is None:
                continue
            if query:
                found.append((match.start(), day_token or month_token))
            else:
                found.append((match.start(), month_token))
                if day_token:
                    found.append((match.start(), day_token))
    for match in _DATE_HAN.finditer(norm):
        year = cn_to_int(match.group(1))
        month = cn_to_int(match.group(2))
        day = cn_to_int(match.group(3)) if match.group(3) else None
        if year is None or month is None or (match.group(3) and day is None):
            continue
        month_token, day_token = _date_token(year, month, day) or (None, None)
        if month_token is None:
            continue
        if query:
            found.append((match.start(), day_token or month_token))
        else:
            found.append((match.start(), month_token))
            if day_token:
                found.append((match.start(), day_token))
    for match in _CLAUSE.finditer(norm):
        raw = match.group(1)
        number = int(raw) if raw.isdigit() else cn_to_int(raw)
        if number is not None and 1 <= number <= 10000:
            found.append((match.start(), f'clause_{number}'))
    return found


def document_tokens(text):
    norm = normalize(text)
    items = []
    for match in _CJK_RUN.finditer(norm):
        run = match.group()
        start = match.start()
        for index in range(len(run) - 1):
            items.append((start + index, 0, run[index:index + 2]))
    for start, token in identifier_tokens(norm):
        items.append((start, 1, token))
    for start, token in canonical_tokens(norm, query=False):
        items.append((start, 2, token))
    items.sort()
    return [token for _, _, token in items][:_MAX_TOKENS]


def document_tsvector(text):
    tokens = document_tokens(text)
    if not tokens:
        return ''
    return ' '.join(f'{quote_lexeme(token)}:{index}' for index, token in enumerate(tokens, start=1))


def _phrases(norm):
    stripped = norm
    for stop in sorted(_MULTI_STOPS, key=len, reverse=True):
        stripped = stripped.replace(stop, '\n')
    phrases = []
    for part in _PUNCT.split(stripped):
        buffer = []
        for char in part:
            if '\u3400' <= char <= '\u9fff' or '\uf900' <= char <= '\ufaff':
                if char in _STOP_CHARS:
                    if len(buffer) >= 2:
                        phrases.append(''.join(buffer))
                    buffer = []
                else:
                    buffer.append(char)
            else:
                if len(buffer) >= 2:
                    phrases.append(''.join(buffer))
                buffer = []
        if len(buffer) >= 2:
            phrases.append(''.join(buffer))
    return list(dict.fromkeys(phrases))


def _windows(phrase):
    if len(phrase) <= 16:
        return (phrase,)
    pieces = []
    for start in range(0, len(phrase) - 7, 4):
        pieces.append(phrase[start:start + 8])
        if len(pieces) >= 8:
            break
    return tuple(pieces) or (phrase[:8],)


def _phrase_group(phrase):
    if len(phrase) < 2:
        return ()
    if len(phrase) == 2:
        return (phrase,)
    return tuple(phrase[index:index + 2] for index in range(len(phrase) - 1))


class KeywordPlan:
    """一条查询的全文检索式，以及用来把字面命中排到前面的核对项。"""

    def __init__(self, groups, needles, canonical):
        self.groups = tuple(tuple(group) for group in groups)
        self.needles = tuple(needles)
        self.canonical = tuple(canonical)

    @property
    def tsquery(self):
        if not self.groups:
            return ''
        parts = []
        for group in self.groups:
            quoted = [quote_lexeme(token) for token in group]
            parts.append(quoted[0] if len(quoted) == 1 else '(' + ' & '.join(quoted) + ')')
        return ' | '.join(parts)

    def exact(self, text):
        norm = normalize(text)
        if any(needle and needle in norm for needle in self.needles):
            return True
        if not self.canonical:
            return False
        present = {token for _, token in canonical_tokens(norm, query=False)}
        return any(token in present for token in self.canonical)

    def score(self, text, rank):
        if self.exact(text):
            return 1.0
        try:
            rank = float(rank)
        except (TypeError, ValueError):
            rank = 0.0
        if not math.isfinite(rank) or rank < 0:
            rank = 0.0
        return min(rank, 0.999)


def build_keyword_query(query):
    """长词优先。问句里已经有四字及以上的内容时，不再把“公司”这类二字词铺开。"""
    norm = normalize(query)
    phrases = _phrases(norm)
    long = [phrase for phrase in phrases if len(phrase) >= 4]
    chosen = long or phrases
    items = []
    for phrase in chosen:
        for piece in _windows(phrase):
            group = _phrase_group(piece)
            if group:
                items.append((len(piece), group, piece, None))
    identifiers = list(dict.fromkeys(token for _, token in identifier_tokens(norm)))
    for token in identifiers:
        items.append((100 + len(token), (token,), token, None))
    canonical = list(dict.fromkeys(token for _, token in canonical_tokens(norm, query=True)))
    for token in canonical:
        items.append((80 + len(token), (token,), None, token))
    items.sort(key=lambda item: -item[0])
    kept = []
    seen = set()
    for item in items:
        if item[1] in seen:
            continue
        seen.add(item[1])
        kept.append(item)
        if len(kept) >= _MAX_GROUPS:
            break
    needles = [needle for _, _, needle, _ in kept if needle][:_MAX_NEEDLES]
    return KeywordPlan([group for _, group, _, _ in kept], needles, canonical)


def prepare_keyword_rows(rows, plan):
    prepared = []
    for row in rows:
        item = dict(row)
        item['keyword_score'] = plan.score(item['text'], item.get('keyword_score'))
        prepared.append(item)
    prepared.sort(key=lambda row: (-row['keyword_score'], str(row.get('index_kind') or ''), str(row.get('build_id') or ''), str(row.get('id'))))
    return prepared


def page_key(row):
    return ('page', str(row['id']))


def mixed_key(row):
    if row.get('index_kind') == 'parent_child':
        return ('parent', str(row['build_id']), str(row['id']))
    return page_key(row)


def fuse_candidates(vector_rows, keyword_rows, *, mode, fusion, rrf_k, vector_weight, keyword_weight, limit, key):
    """向量名单和关键词名单合并。单路模式只保留对应名单，次序与该路排序一致。"""
    if mode == 'vector':
        keyword_rows = []
    elif mode == 'keyword':
        vector_rows = []
    elif mode != 'hybrid':
        raise ValueError('检索模式无效。')
    records = {}
    order = []

    def touch(row):
        identity = key(row)
        if identity not in records:
            records[identity] = dict(row)
            order.append(identity)
        return records[identity]

    vector_rank = {}
    for rank, row in enumerate(vector_rows, start=1):
        item = touch(row)
        vector_rank[key(row)] = rank
        item['similarity'] = float(row['similarity'])
    keyword_rank = {}
    keyword_score = {}
    for rank, row in enumerate(keyword_rows, start=1):
        item = touch(row)
        keyword_rank[key(row)] = rank
        keyword_score[key(row)] = float(row['keyword_score'])
        item['keyword_score'] = float(row['keyword_score'])
        if item.get('similarity') is None:
            item['similarity'] = 0.0

    def normalized(rows, field):
        if not rows:
            return {}
        values = [float(row[field]) for row in rows]
        low, high = min(values), max(values)
        return {key(row): 1.0 if high == low else (value - low) / (high - low) for row, value in zip(rows, values)}

    use_weighted = mode == 'hybrid' and fusion == 'weighted'
    vector_norm = normalized(vector_rows, 'similarity') if use_weighted else {}
    keyword_norm = normalized(keyword_rows, 'keyword_score') if use_weighted else {}
    fused = []
    for identity in order:
        item = records[identity]
        vector_at = vector_rank.get(identity)
        keyword_at = keyword_rank.get(identity)
        vector_value = float(item['similarity']) if vector_at else None
        keyword_value = keyword_score.get(identity) if keyword_at else None
        if use_weighted:
            score = vector_weight * vector_norm.get(identity, 0.0) + keyword_weight * keyword_norm.get(identity, 0.0)
            fusion_name = 'weighted'
        else:
            score = 0.0
            if vector_at:
                score += vector_weight / (rrf_k + vector_at)
            if keyword_at:
                score += keyword_weight / (rrf_k + keyword_at)
            fusion_name = fusion if mode == 'hybrid' else mode
        channels = []
        if vector_at:
            channels.append('vector')
        if keyword_at:
            channels.append('keyword')
        item['similarity'] = 0.0 if vector_value is None else vector_value
        item['recall'] = dict(channels=channels, vector_score=vector_value, keyword_score=keyword_value,
            vector_rank=vector_at, keyword_rank=keyword_at, fusion=fusion_name, fusion_score=score)
        fused.append(item)
    fused.sort(key=lambda row: (
        -row['recall']['fusion_score'],
        -(row['recall']['vector_score'] or 0),
        -(row['recall']['keyword_score'] or 0),
        str(row.get('index_kind') or ''),
        str(row.get('build_id') or ''),
        str(row.get('id')),
    ))
    return fused[:limit]


def fuse_for(settings, vector_rows, keyword_rows, *, key):
    return fuse_candidates(vector_rows, keyword_rows, mode=settings.retrieval_mode, fusion=settings.retrieval_fusion,
        rrf_k=settings.retrieval_rrf_k, vector_weight=settings.retrieval_vector_weight,
        keyword_weight=settings.retrieval_keyword_weight, limit=settings.retrieval_candidate_limit, key=key)


def backfill_lexemes(database, *, force=False, batch=200):
    """给 lexemes 为空的旧片段补写全文索引。force 时按主键重写全部片段，不重新嵌入。"""
    if type(batch) is not int or not 1 <= batch <= 1000:
        raise ValueError('补建批次无效。')
    return dict(evidence_chunks=_backfill_chunks(database, force=force, batch=batch),
        parent_index_children=_backfill_children(database, force=force, batch=batch))


def _backfill_chunks(database, *, force, batch):
    updated = 0
    last = None
    while True:
        with database.connect() as conn:
            if force:
                rows = conn.execute('''SELECT id, text FROM dd_agent.evidence_chunks
                    WHERE %s::uuid IS NULL OR id > %s::uuid ORDER BY id LIMIT %s''', (last, last, batch)).fetchall()
            else:
                rows = conn.execute('''SELECT id, text FROM dd_agent.evidence_chunks
                    WHERE lexemes IS NULL ORDER BY id LIMIT %s''', (batch,)).fetchall()
            if not rows:
                return updated
            for row in rows:
                conn.execute('UPDATE dd_agent.evidence_chunks SET lexemes=%s::tsvector WHERE id=%s',
                    (document_tsvector(row['text']), row['id']))
            updated += len(rows)
            last = rows[-1]['id']
            if len(rows) < batch:
                return updated


def _backfill_children(database, *, force, batch):
    updated = 0
    last = (None, None)
    while True:
        with database.connect() as conn:
            if force:
                rows = conn.execute('''SELECT build_id, id, text FROM dd_agent.parent_index_children
                    WHERE (%s::text IS NULL AND %s::text IS NULL) OR (build_id, id) > (%s::text, %s::text)
                    ORDER BY build_id, id LIMIT %s''', (*last, *last, batch)).fetchall()
            else:
                rows = conn.execute('''SELECT build_id, id, text FROM dd_agent.parent_index_children
                    WHERE lexemes IS NULL ORDER BY build_id, id LIMIT %s''', (batch,)).fetchall()
            if not rows:
                return updated
            for row in rows:
                conn.execute('''UPDATE dd_agent.parent_index_children SET lexemes=%s::tsvector
                    WHERE build_id=%s AND id=%s''', (document_tsvector(row['text']), row['build_id'], row['id']))
            updated += len(rows)
            last = (rows[-1]['build_id'], rows[-1]['id'])
            if len(rows) < batch:
                return updated
