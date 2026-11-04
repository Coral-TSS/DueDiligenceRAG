"""保守的原文组装器：不调用模型，不改写原文，不建立向量。

每批只处理有界页快照；未封口尾段通过 JSON 检查点交给下一批。
引用跨度与判定边界所用的依赖分开，插入的分隔符没有伪造的来源坐标。
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import re

VERSION = 'conservative-assembly-v1'
MAX_CHARACTERS = 4000
TARGET_CHARACTERS = 3000
MAX_PAGES = 3
MAX_STATE_BYTES = 512 * 1024

_HEADING = re.compile(r'^(?:第[零〇一二三四五六七八九十百千\d]+[条章节]|[一二三四五六七八九十]+[、．]|[（(][一二三四五六七八九十\d]+[）)])')
_FORM = re.compile(r'^(?:营业执照|公司[变更设立]*登记[申请审查]*[书表]|股东[会大会]*决议|董事会决议|公司章程|企业信用信息公示报告)')
_CONNECTOR = re.compile(r'^(?:并(?:且|应|说明|提供|补充)?|且|以及|其中|或者|但|应当|不得|须)')


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class PageSnapshot:
    project_id: str
    document_id: str
    page_number: int
    source_revision: str
    text: str
    readiness: str = 'ready'
    quality: str = 'unverified'

    def __post_init__(self):
        if any(not isinstance(v,str) or not 1<=len(v)<=256 for v in
               (self.project_id,self.document_id,self.source_revision,self.quality)):
            raise ValueError('快照身份与来源修订必须明确。')
        if type(self.page_number) is not int or self.page_number<1:
            raise ValueError('物理页号必须为正整数。')
        if self.readiness not in ('ready','missing','failed','pending'):
            raise ValueError('未知页就绪状态。')
        if not isinstance(self.text,str) or len(self.text)>100000:
            raise ValueError('单页文字最多 100000 字符。')
        if self.readiness!='ready' and self.text:
            raise ValueError('未就绪页不能作为正文输入。')

    def dependency(self):
        return dict(project_id=self.project_id,document_id=self.document_id,page_number=self.page_number,
                    source_revision=self.source_revision,readiness=self.readiness,quality=self.quality,
                    table_uncertain=('\t' in self.text or '|' in self.text),
                    form=bool(_FORM.match(self.text.lstrip())))


def _paragraphs(text):
    # 删除的只是段首尾空白；保留正文内部的换行、字词与标点。
    cuts={0,len(text)}
    for match in re.finditer(r'\n[ \t]*\n+',text):cuts.update((match.start(),match.end()))
    for match in re.finditer(r'(?m)^\s*(?=\S)',text):
        start=match.end()
        if _HEADING.match(text[start:]) or _FORM.match(text[start:]):cuts.add(start)
    ordered=sorted(cuts)
    for left,right in zip(ordered,ordered[1:]):
        while left<right and text[left].isspace():left+=1
        while right>left and text[right-1].isspace():right-=1
        if right>left:yield left,right


def _boundary(text):
    if _FORM.match(text):return 'form'
    if _HEADING.match(text):return 'heading'
    return None


def _depend(block,dependency):
    if dependency is not None and dependency not in block['dependencies']:
        block['dependencies'].append(dependency)
        block['dependencies'].sort(key=lambda d:d['page_number'])


def _sealed_state(state):
    state['checksum']=digest(state)
    if len(canonical(state).encode('utf-8'))>MAX_STATE_BYTES:
        raise ValueError('尾段检查点超过容量上限。')
    return state


def assemble(pages,*,state=None,final=False):
    """输入须按物理页连续，缺页须显式传 missing 快照；final 才封口材料尾段。

    窗口结束不等于材料结束。调用者保存返回 state，并在下一批恢复；
    同输入与检查点产生相同父块 ID，便于上层幂等发布。
    """
    pages=list(pages)
    if len(pages)>20 or sum(len(p.text) for p in pages)>200000:
        raise ValueError('单批最多 20 页／200000 字符。')
    if state is None:
        if not pages:raise ValueError('首批需要明确材料快照。')
        state=dict(version=VERSION,project_id=pages[0].project_id,document_id=pages[0].document_id,
                   next_page=pages[0].page_number,tail=None,last=None,closed=False)
    else:
        state=deepcopy(state)
        checksum=state.pop('checksum',None)
        if checksum!=digest(state) or state.get('version')!=VERSION or state.get('closed'):
            raise ValueError('检查点损坏、版本不符或材料已封口。')
    for index,page in enumerate(pages):
        if (page.project_id,page.document_id,page.page_number)!=(state['project_id'],state['document_id'],state['next_page']+index):
            raise ValueError('快照必须属于同项目／材料并按连续物理页输入。')
    parents=[]
    tail=state['tail']

    def flush(reason,lookahead=None):
        nonlocal tail
        if tail is None:return None
        _depend(tail,lookahead)
        tail['boundary']=reason
        tail['id']=digest(dict(version=VERSION,**tail))
        parents.append(tail)
        if len(parents)>2000:raise ValueError('单批父块数量超过上限。')
        identity=tail['id'];tail=None
        return identity

    def append(page,start,end,separator='',continuation_from=None):
        nonlocal tail
        if tail is None:
            tail=dict(text='',spans=[],separators=[],dependencies=[],continuation_from=continuation_from)
            # 上一页可能只是边界证据，不一定贡献引用文字。
            _depend(tail,state['last'])
        if separator and tail['text']:
            offset=len(tail['text']);tail['text']+=separator
            tail['separators'].append(dict(start=offset,end=offset+len(separator),text=separator))
        offset=len(tail['text']);tail['text']+=page.text[start:end]
        tail['spans'].append(dict(project_id=page.project_id,document_id=page.document_id,
            page_number=page.page_number,source_revision=page.source_revision,start=start,end=end,
            derived_start=offset,derived_end=len(tail['text'])))
        _depend(tail,page.dependency())

    for page in pages:
        dependency=page.dependency();parts=list(_paragraphs(page.text))
        first=page.text[parts[0][0]:parts[0][1]] if parts else ''
        reason=None;linked=None
        if page.readiness!='ready':reason=page.readiness
        elif not parts:reason='blank_page'
        elif _boundary(first):reason=_boundary(first)
        elif (state['last'] or {}).get('form') or (tail and _boundary(tail['text'])=='form'):reason='form'
        elif dependency['table_uncertain'] or (state['last'] or {}).get('table_uncertain'):reason='table_uncertain'
        elif page.quality in ('uncertain','machine_uncertain') or (state['last'] or {}).get('quality') in ('uncertain','machine_uncertain'):reason='quality_uncertain'
        elif tail:
            # 仅缺少句号不是续句证据，必须同时出现前页连接标点和后页连接词。
            if not (tail['text'].endswith(('，','；',',',';')) and _CONNECTOR.match(first)):
                reason='page_boundary'
            elif len({s['page_number'] for s in tail['spans']})>=MAX_PAGES:reason='page_limit'
        if reason:
            linked=flush(reason,dependency) if reason=='page_limit' else None
            if reason!='page_limit':flush(reason,dependency)
        if page.readiness=='ready':
            for part_index,(start,end) in enumerate(parts):
                if part_index and _boundary(page.text[start:end]):flush(_boundary(page.text[start:end]),dependency)
                separator='\n\n' if part_index and tail else ''
                while start<end:
                    room=MAX_CHARACTERS-(len(tail['text']) if tail else 0)-(len(separator) if tail else 0)
                    if room<=0:
                        linked=flush('character_limit',dependency);separator='';continue
                    stop=min(end,start+room)
                    # 在足够长的位置优先断句；找不到时采用硬上限并显式标明续块。
                    if stop<end:
                        choices=[page.text.rfind(mark,start+1500,stop) for mark in ('。','；','\n')]
                        if max(choices)>=start+1500:stop=max(choices)+1
                    append(page,start,stop,separator,linked)
                    linked=None;start=stop;separator=''
                    if start<end:linked=flush('character_limit',dependency)
                if tail and len(tail['text'])>=TARGET_CHARACTERS and part_index<len(parts)-1:
                    flush('paragraph_target',dependency)
        state['last']=dependency
        state['next_page']=page.page_number+1
    if final:flush('document_end');state['closed']=True
    state['tail']=tail
    return dict(parents=parents,state=_sealed_state(state))


def reverse_dependencies(parents):
    """引用和前瞻／缺口共同参与反向依赖；不只关注出现原文的页。"""
    result={}
    for parent in parents:
        for dependency in parent['dependencies']:
            key=(dependency['document_id'],dependency['page_number'])
            result.setdefault(key,set()).add(parent['id'])
    return result
