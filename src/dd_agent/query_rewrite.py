"""规则改写尽调查询。不调用大模型。

词表只收工商登记和公司文件里的通用字段名：职务、注册资本和实缴、
常见组织文件。不含客户名称、项目名称或统一社会信用代码的具体取值。
向量和重排用一句稍作补充的自然语言；关键词另加官方词，避免把同义词
整表塞进嵌入。
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re


LEXICON_PATH = Path(__file__).parent / 'lexicon' / 'dd_official_terms.json'
MAX_VECTOR_ADDS = 2
MAX_KEYWORD_TERMS = 6
MAX_VECTOR_EXTRA_CHARS = 24
_CJK_TERM = re.compile(r'^[\u3400-\u9fff\uf900-\ufaff]{2,16}$')
_LEXICON = None


@dataclass(frozen=True)
class QueryRewrite:
    """用户原句，以及向量／重排用的轻改写。keyword_terms 只进全文检索。"""

    original: str
    rewritten: str
    keyword_terms: tuple
    enabled: bool

    def public(self):
        return {
            'original': self.original,
            'rewritten': self.rewritten,
            'enabled': self.enabled,
        }


def load_lexicon(path=None):
    file = Path(path) if path is not None else LEXICON_PATH
    payload = json.loads(file.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('entries'), list):
        raise ValueError('术语词表无效。')
    seen = set()
    entries = []
    for entry in payload['entries']:
        if not isinstance(entry, dict):
            raise ValueError('术语词表无效。')
        item_id = entry.get('id')
        if not isinstance(item_id, str) or not item_id or item_id in seen:
            raise ValueError('术语词表无效。')
        seen.add(item_id)
        match = _terms(entry.get('match'), required=True)
        keyword = _terms(entry.get('keyword'), required=True)
        vector = entry.get('vector')
        if not isinstance(vector, str) or not _CJK_TERM.fullmatch(vector) or vector not in keyword:
            raise ValueError('术语词表无效。')
        exclude = _terms(entry.get('exclude') or [], required=False)
        entries.append(dict(id=item_id, match=match, keyword=keyword, vector=vector, exclude=exclude))
    if not entries:
        raise ValueError('术语词表无效。')
    return dict(version=str(payload.get('version') or ''), entries=tuple(entries))


def lexicon():
    global _LEXICON
    if _LEXICON is None:
        _LEXICON = load_lexicon()
    return _LEXICON


def resolve_query(query, *, enabled=True):
    """校验用户原句长度，再按开关决定是否扩展。扩展结果可以长于 500 字。"""
    if not isinstance(query, str):
        raise ValueError('查询须为 1–500 个字符。')
    text = query.strip()
    if not 1 <= len(text) <= 500:
        raise ValueError('查询须为 1–500 个字符。')
    if not enabled:
        return QueryRewrite(original=text, rewritten=text, keyword_terms=(), enabled=False)
    rewritten, terms = expand_query(text)
    return QueryRewrite(original=text, rewritten=rewritten, keyword_terms=tuple(terms), enabled=True)


def expand_query(text, entries=None):
    """返回 (轻改写, 关键词补充词)。补充词不写进向量句子。"""
    if entries is None:
        entries = lexicon()['entries']
    matched = _match(text, entries)
    vector_added = []
    fresh = []
    present = []
    for entry in matched:
        canonical = entry['vector']
        if canonical not in text and canonical not in vector_added and len(vector_added) < MAX_VECTOR_ADDS:
            vector_added.append(canonical)
        for term in entry['keyword']:
            bucket = present if term in text else fresh
            if term in fresh or term in present:
                continue
            bucket.append(term)
    # 问句里没有的官方词优先。已经写出的官方词也保留，避免长词过滤把二字职务丢掉。
    keyword_terms = (fresh + present)[:MAX_KEYWORD_TERMS]
    return _light_rewrite(text, vector_added), tuple(keyword_terms)


def attach_query_rewrite(response, rewrite):
    if not isinstance(response, dict) or not isinstance(rewrite, QueryRewrite):
        return response
    copied = dict(response)
    copied['query_rewrite'] = rewrite.public()
    return copied


def _terms(values, *, required):
    if not isinstance(values, list) or (required and not values):
        raise ValueError('术语词表无效。')
    terms = []
    for value in values:
        if not isinstance(value, str) or not _CJK_TERM.fullmatch(value):
            raise ValueError('术语词表无效。')
        if value not in terms:
            terms.append(value)
    if required and not terms:
        raise ValueError('术语词表无效。')
    return tuple(terms)


def _match(text, entries):
    found = []
    for entry in entries:
        if any(blocked and blocked in text for blocked in entry['exclude']):
            continue
        positions = [text.find(trigger) for trigger in entry['match'] if trigger in text]
        if not positions:
            continue
        found.append((min(positions), entry['id'], entry))
    found.sort()
    return [entry for _, _, entry in found]


def _light_rewrite(text, added):
    if not added:
        return text
    suffix = '（' + '、'.join(added) + '）'
    if len(suffix) > MAX_VECTOR_EXTRA_CHARS:
        suffix = '（' + added[0] + '）'
    if len(suffix) > MAX_VECTOR_EXTRA_CHARS:
        return text
    return text + suffix
