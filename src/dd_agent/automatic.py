"""Report-driven local enrichment. No human adoption is part of this pipeline."""
# 自动增强层：使用基础文字确定关注区域，再按需调用小视觉模型；不是报告生成器。
# 阅读顺序：route_page() 判断是否增强 -> regions_for() 定位区域 -> choose_region() 比较结果
#          -> AutomaticProcessor.process() 复用候选、局部回填并追加机器版本。
# 策略是保守规则，不等于完整字段抽取、实体关系解析或法律事实核实。
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .config import Settings
from .db import Database
from .review import LocalVision, ReviewService, ReviewError, ReviewConflict, VisionTimeout
from .storage import Storage
from .performance import observed, timed, count, span


PROFILE = json.loads((Path(__file__).parent/'profiles/company_history.json').read_text(encoding='utf-8'))
# 画像只描述关注什么信息，不携带样例报告中的公司事实答案。
SELECTION_VERSION = 'automatic-regions-selective-v3'
# 日期或带单位／千分位的金额。不用任意数字，避免“第2行”这类序号把整页都送去视觉。
_VALUE = re.compile(r'\d{2,4}年|\d{4}[-/.]\d{1,2}|\d+(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?(?:亿元|万元|元|[%％])')
UNKNOWN = '〔该区域信息无法可靠识别〕'


class AutomaticError(ReviewError):
    pass


def normalized(text):
    return re.sub(r'\s+', '', text)


def alias_text(text):
    # OCR 别名替换仅用于匹配线索，不直接把原始 OCR 正文改成预设答案。
    text = normalized(text)
    for old, new in PROFILE['ocr_aliases'].items():
        text = text.replace(old, new)
    return text


def keyword_hits(text):
    hits = {name: [word for word in words if word in text] for name, words in PROFILE['groups'].items()}
    return {name: words for name, words in hits.items() if words}


def _score_allows(score, ceiling):
    # 没有分数时沿用旧规则：只要行本身符合条件就选。有分数时不超过配置上限才选。
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
        return True
    return float(score) <= ceiling


def line_priority(line, page_important, low_score, *, value_lines=True, value_max_score=1.0, keyword_max_score=1.0):
    """0 表示不调用视觉。1 低置信，2 行内既有线索又有数字，3 本页有线索时的数字／日期／金额行。"""
    text = alias_text(str(line.get('text') or ''))
    if not text:
        return 0
    score = line.get('score')
    low = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(float(score)) and float(score) < low_score
    keyword = bool(keyword_hits(text))
    if low:
        return 1
    # 同一行里既有画像线索又有数字，例如“注册资本2000万元”。
    if keyword and re.search(r'\d', text) and _score_allows(score, keyword_max_score):
        return 2
    # 线索在邻行时，本行仍可能是日期或金额。关闭金额行后不再单凭日期／金额入选。
    if value_lines and page_important and _VALUE.search(text) and _score_allows(score, value_max_score):
        return 3
    return 0


def _policy_from(settings=None, **policy):
    if settings is not None:
        policy.setdefault('value_lines', settings.vision_value_lines)
        policy.setdefault('value_max_score', settings.vision_value_max_score)
        policy.setdefault('keyword_max_score', settings.vision_keyword_max_score)
    return dict(value_lines=policy.get('value_lines', True),
                value_max_score=policy.get('value_max_score', 1.0),
                keyword_max_score=policy.get('keyword_max_score', 1.0))


def route_page(page, filename='', low_score=0.85, *, value_lines=True, value_max_score=1.0, keyword_max_score=1.0):
    # 关键词仍用于分类；视觉只打在低置信行，或带数字／日期／金额的关键行。
    # 不再因为整页出现一个词就把全页送进视觉模型。文字过少时仍保留一页兜底。
    text = alias_text(page.get('text', ''))
    hits = keyword_hits(text)
    poor = not text or '\ufffd' in text or (len(text) < 8 and any(t in filename for t in PROFILE['material_hints']))
    lines = page.get('lines') or []
    policy = _policy_from(value_lines=value_lines, value_max_score=value_max_score, keyword_max_score=keyword_max_score)
    if lines:
        triggered = any(line_priority(line, bool(hits), low_score, **policy) for line in lines)
    else:
        triggered = bool(hits) and (bool(re.search(r'\d', text)) or (value_lines and bool(_VALUE.search(text))))
    important = bool(hits)
    return dict(important=important, needs_vision=page.get('method') == 'ocr' and (poor or triggered),
                classification='important' if important else 'uncertain' if poor else 'ordinary',
                groups=hits, reason='低置信或含数字的关键行' if triggered and not poor else '基础文字不足，自动兜底' if poor else '未命中需增强的行',
                profile=PROFILE['version'])


REASONS = {1: 'low', 2: 'keyword_digit', 3: 'value'}


def selection_report(triggered, regions):
    """页是否触发、实际送入模型的区域，以及入选原因。不包含正文。"""
    reasons = {}
    selected = not_selected = 0
    for region in regions:
        if region.get('over_budget'):
            not_selected += 1
            continue
        selected += 1
        reason = region.get('select_reason') or 'poor'
        reasons[reason] = reasons.get(reason, 0) + 1
    return dict(triggered=bool(triggered), selected=selected, not_selected=not_selected, reasons=reasons)


def _mark_region(region, reason, tokens):
    region['select_reason'] = reason
    if tokens and tokens.get(reason):
        region['max_new_tokens'] = int(tokens[reason])
    return region


def regions_for(page, route, path, limit, low_score=0.85, *, value_lines=True, value_max_score=1.0, keyword_max_score=1.0, tokens=None):
    # 将重要行与其前后行合成上下文窗口，使用 OCR 行框换算 0–1 归一化裁剪坐标。
    # start/end 是在基础页 text 中的切片位置，和图片坐标属于两套定位，需要同时保留。
    text=page['text']; lines=page.get('lines',[])
    policy=_policy_from(value_lines=value_lines, value_max_score=value_max_score, keyword_max_score=keyword_max_score)
    def whole(reason='poor'):
        return [_mark_region(dict(bounds=[0.,0.,1.,1.],start=0,end=len(text),over_budget=False), reason, tokens)]
    if not lines or not path.is_file():
        return whole()
    try:
        with path.open('rb') as stream:
            header=stream.read(24)
        if header[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError()
        width,height=struct.unpack('>II',header[16:24])
        offsets=[];cursor=0
        for line in lines:
            start=text.find(line['text'],cursor)
            if start<0:raise ValueError()
            offsets.append((start,start+len(line['text'])));cursor=start+len(line['text'])
        important=route.get('classification')=='important' or route.get('important')
        priorities=[(i,line_priority(line, important, low_score, **policy)) for i,line in enumerate(lines)]
        anchors=[(i,priority) for i,priority in priorities if priority]
        # 没有低置信或数字关键行时，只有文字不足的页才退回整页。
        if not anchors:
            if route.get('classification')=='uncertain':
                return whole()
            return []
        windows=[]
        for i,priority in anchors:
            first,last=max(0,i-1),min(len(lines)-1,i+2)
            if windows and first<=windows[-1][1]+1 and last-windows[-1][0]<10:
                windows[-1]=(windows[-1][0],last,min(windows[-1][2],priority))
            else:windows.append((first,last,priority))
        regions=[]
        for first,last,priority in windows:
            points=[point for line in lines[first:last+1] for point in line['box']]
            box=[max(0,(min(p[0] for p in points)-12)/width),max(0,(min(p[1] for p in points)-12)/height),
                 min(1,(max(p[0] for p in points)+12)/width),min(1,(max(p[1] for p in points)+12)/height)]
            if box[0]>=box[2] or box[1]>=box[3]:raise ValueError()
            regions.append(dict(bounds=box,start=offsets[first][0],end=offsets[last][1],priority=priority))
        # 预算留给优先级更高的窗口。没选中的区域保留基础 OCR，不能标成未知。
        chosen=set(sorted(range(len(regions)),key=lambda i:(regions[i]['priority'],i))[:limit])
        for i,region in enumerate(regions):
            priority=region.pop('priority')
            region['over_budget']=i not in chosen
            _mark_region(region, REASONS[priority], tokens)
        return regions
    except (ValueError,KeyError,TypeError,struct.error):
        return whole()


def numbers(text):
    return [Decimal(value.replace(',','')) for value in re.findall(r'\d+(?:,\d{3})*(?:\.\d+)?', text)]


def summarize_regions(text, regions):
    """只改实际看过的区域。没送进模型的区域保留 OCR，也不把整页算成不确定。"""
    examined=[region for region in regions if region.get('decision')!='not_run']
    for region in sorted(examined, key=lambda item: item['start'], reverse=True):
        text=text[:region['start']]+region['text']+text[region['end']:]
    if any(region['decision']=='unknown' for region in examined):
        outcome='uncertain'
    elif any(region['decision']=='enhanced' for region in examined):
        outcome='enhanced'
    elif examined:
        outcome='checked'
    else:
        outcome='base'
    return text, outcome


def mask_unknown(text):
    # 局部结论不可靠时掩去整个被选区域，避免下一行残留的姓名／金额继续作为证据。
    # 原始 OCR 不删除；这里只影响新的机器有效文字，区域之外的内容仍保留。
    # Field values often continue on the next line. Retaining those lines could
    # expose a rejected identity as usable evidence. Only this region is masked.
    return UNKNOWN


def identity_values(text):
    # 仅做常见标签后的文字对照，不负责公司别名、同名主体或历史时点的实体归并。
    labels='企业名称|公司名称|法定代表人|股东名称|投资人|注册地址|住所|统一社会信用代码|注册号'
    return [(m[1],normalized(m[2]).strip('：:，,。;；'))
            for m in re.finditer(r'('+labels+r')[：:\s]*([^\n]*)',text)]


def choose_region(base,candidate):
    # candidate 是视觉模型对同一区域的识别结果，不是外网模型给出的法律意见。
    # 截断、空结果、超时／预算导致无候选时保持未知，不猜出一个确定字段。
    if not candidate or candidate.get('truncated') or not candidate.get('text','').strip():
        return dict(decision='unknown',text=mask_unknown(base),reason='视觉失败、空结果、预算限制或截断')
    visual=candidate['text'].strip()
    repaired=base
    # 对缺位年份只允许有限的候选对齐，再继续经过数值、单位、身份和相似度检查。
    # 这只是机器选择启发式，不能把“看起来是合理日期”当作原件事实正确的证明。
    old=list(re.finditer(r'(\d{2,4})年',base));new=list(re.finditer(r'(\d{2,4})年',visual))
    if len(old)==len(new):
        for left,right in reversed(list(zip(old,new))):
            if not 1900<=int(left[1])<=2100 and 1900<=int(right[1])<=2100:
                iterator=iter(right[1])
                if all(char in iterator for char in left[1]):
                    repaired=repaired[:left.start(1)]+right[1]+repaired[left.end(1):]
    same_numbers=numbers(repaired)==numbers(visual)
    # 两路给出不同但各自合法的金额／身份时，也不能简单认为视觉模型一定更正确。
    units=lambda s:re.findall(r'亿元|万元|元|[%％]',s)
    same_units=units(repaired)==units(visual)
    same_identity=identity_values(repaired)==identity_values(visual)
    # Invalid dates must remain unknown even if two models agree on them.
    def valid_dates(text):
        for y,m,d in re.findall(r'(\d{2,4})年(\d{1,2})月(\d{1,2})日',text):
            try:
                if not 1900<=int(y)<=2100:return False
                date(int(y),int(m),int(d))
            except ValueError:return False
        return True
    similarity=SequenceMatcher(None,normalized(repaired),normalized(visual)).ratio()
    if not same_numbers or not same_units or not same_identity or not valid_dates(visual) or similarity<.72:
        return dict(decision='unknown',text=mask_unknown(base),reason='身份文字、数值／单位、日期或文本对应关系无法消解')
    if normalized(base)==normalized(visual):
        # consistent 只表示两路文字一致，不是人工确认或事实已核实。
        return dict(decision='consistent',text=base,reason='OCR 与视觉识别相同，事实未核实')
    return dict(decision='enhanced',text=visual,reason='位置与文字对应，数值／单位保护通过')


class AutomaticProcessor:
    def __init__(self,settings=None,vision=None,database=None):
        self.settings=settings or Settings.from_env()
        self.database=database if database is not None else Database(self.settings.database_url)
        self.vision=vision or LocalVision(self.settings)
        self.review=ReviewService(self.settings,vision=self.vision,database=self.database)
        self.vision_timeouts=0

    @timed('fingerprint.automatic')
    def version(self):
        # 增强策略版本同时绑定画像、选择规则、视觉模型、区域预算、低置信阈值和生成长度。
        settings=self.settings
        selector='|'.join((SELECTION_VERSION, f'regions={settings.vision_regions_per_page}',
                           f'low={settings.vision_low_score:.4f}', f'tokens={settings.vision_max_new_tokens}',
                           f'tokens_low={settings.vision_tokens_low}', f'tokens_value={settings.vision_tokens_value}',
                           f'value_lines={int(settings.vision_value_lines)}',
                           f'value_max={settings.vision_value_max_score:.4f}',
                           f'keyword_max={settings.vision_keyword_max_score:.4f}',
                           f'budget={settings.vision_timeout_budget}'))
        return hashlib.sha256((json.dumps(PROFILE,sort_keys=True)+selector+self.vision.profile()).encode()).hexdigest()

    def state(self,project_id,document_id,number):
        # current 在此表示机器结果仍对应当前基础页修订。
        # process() 中的缓存复用还要另比 profile_version，不能只看这个布尔值。
        if self.database.document(project_id,document_id) is None:raise AutomaticError('材料不存在或不属于当前项目。')
        with self.database.connect() as conn:
            rows=conn.execute('SELECT * FROM dd_agent.machine_pages WHERE document_id=%s AND page_number=%s ORDER BY created_at DESC,id DESC',(document_id,number)).fetchall()
            source=conn.execute('SELECT raw_revision FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=%s',(document_id,number)).fetchone()
        return {**(rows[0] if rows else {'result':None}), 'current':bool(rows and source and rows[0]['base_revision']==source['raw_revision']), 'history':rows}

    @observed('automatic')
    def process(self,project_id,document_id,pages,checkpoint=None):
        project_id,document_id=UUID(str(project_id)),UUID(str(document_id))
        document=self.database.document(project_id,document_id)
        if document is None:raise AutomaticError('材料不存在或不属于当前项目。')
        if not pages or len(set(pages))>20 or any(not isinstance(n,int) or n<1 for n in pages):raise ValueError('单批指定 1–20 个有效页码。')
        version=self.version()
        raw={p['page_number']:p for p in self.database.pages(document_id) if p['status']=='succeeded'}
        result=dict(processed_pages=[],skipped_pages=[],failed_pages=[])
        for number in sorted(set(pages)):
            with span('page.vision', page=number):
                self._one_page(project_id,document_id,document,number,raw,version,result,checkpoint)
        return result

    def _one_page(self,project_id,document_id,document,number,raw,version,result,checkpoint):
        if checkpoint:checkpoint('vision',number)
        if number not in raw:
            result['failed_pages'].append(number);return
        page=raw[number]
        state=self.review.state(project_id,document_id,number); revision=state['raw_revision']
        history=self.state(project_id,document_id,number)['history']
        matching=[r for r in history if r['base_revision']==revision and r['profile_version']==version]
        # 超时页带 stop_retry，不再为了同一次超时把 300 秒重跑一遍。
        if matching and (matching[0]['result']['outcome']!='uncertain' or len(matching)>=2 or matching[0]['result'].get('stop_retry')):
            # 缓存策略曾生效但后来被另一策略替换时，重新发布缓存，不追加未知识别尝试。
            count('pages.automatic.reused')
            if history[0]['id']==matching[0]['id']:
                result['skipped_pages'].append(number)
            else:
                try:self._publish(document_id,number,revision,version,matching[0]['result'])
                except ReviewConflict:result['failed_pages'].append(number)
                else:result['processed_pages'].append(number)
            return
        policy=_policy_from(self.settings)
        route=route_page(page,document['filenames'][0],self.settings.vision_low_score,**policy)
        text=page['text']; regions=[]; timed_out=False
        if route['needs_vision']:
            count('pages.vision.triggered')
            image=Storage(self.settings.data_dir,self.settings.max_upload_bytes).path(page['image_key']) if page['image_key'] else self.settings.data_dir/'missing.png'
            tokens=dict(low=self.settings.vision_tokens_low, keyword_digit=self.settings.vision_max_new_tokens,
                        value=self.settings.vision_tokens_value, poor=self.settings.vision_max_new_tokens)
            planned=regions_for(page,route,image,self.settings.vision_regions_per_page,self.settings.vision_low_score,
                                tokens=tokens,**policy)
            report=selection_report(True, planned)
            count('vision.regions.selected', report['selected'])
            count('vision.regions.not_selected', report['not_selected'])
            for reason, amount in report['reasons'].items():
                count('vision.reason.'+reason, amount)
            decided={}
            pending=[]
            for index,region in enumerate(planned):
                if checkpoint:checkpoint('vision',number)
                if region.get('over_budget'):
                    decided[index]=('skip', None, False)
                    continue
                if self.vision_timeouts>=self.settings.vision_timeout_budget:
                    decided[index]=('run', None, True)
                    count('vision.timeout_budget_skips')
                    continue
                try:
                    with self.database.connect() as conn:
                        cached=conn.execute('''SELECT result FROM dd_agent.vision_candidates
                            WHERE document_id=%s AND page_number=%s AND base_revision=%s AND bounds=%s
                            AND profile_version=%s AND status='succeeded'
                            ORDER BY created_at DESC,id DESC LIMIT 1''',(document_id,number,revision,Jsonb(region['bounds']),version)).fetchone()
                    if cached and not cached['result'].get('truncated'):
                        count('vision.candidates.reused')
                        decided[index]=('run', cached['result'], False)
                        continue
                except OSError:
                    pass
                pending.append(index)
            if pending and hasattr(self.vision,'recognize_many'):
                self._infer_batch(project_id,document_id,number,planned,pending,revision,version,decided)
            else:
                for index in pending:
                    decided[index]=self._infer_one(project_id,document_id,number,planned[index],index,revision,version)
            for index,region in enumerate(planned):
                kind,candidate,region_timed_out=decided[index]
                if region_timed_out:timed_out=True
                if checkpoint:checkpoint('vision_saved',number)
                if kind=='skip':
                    choice=dict(decision='not_run', text=page['text'][region['start']:region['end']],
                                reason='超出每页区域预算，保留基础 OCR')
                    candidate=None
                else:
                    choice=choose_region(page['text'][region['start']:region['end']],candidate)
                    if region_timed_out:
                        choice=dict(choice,reason='视觉超时或超时预算已用尽，区域标为未知',timed_out=True)
                regions.append({**region,**choice,'candidate':candidate})
            text,outcome=summarize_regions(text, regions)
        else:
            count('pages.vision.not_triggered')
            outcome='base'
        count('pages.automatic.'+outcome)
        output=dict(text=text,route=route,regions=regions,outcome=outcome,stop_retry=timed_out,
                    selection=selection_report(route['needs_vision'], regions),
                    verification={'uncertain':'machine_uncertain','enhanced':'machine_enhanced','checked':'machine_checked','base':'unverified'}[outcome])
        try:self._publish(document_id,number,revision,version,output)
        except ReviewConflict:
            result['failed_pages'].append(number);return
        result['processed_pages'].append(number)

    def _infer_one(self,project_id,document_id,number,region,index,revision,version):
        try:
            with span('page.vision.region', page=number, region=index):
                response=self.review.recognize(project_id,document_id,number,region['bounds'],revision,profile_version=version,region_index=index)
            if response['status']=='succeeded':
                return ('run', response['result'], False)
        except VisionTimeout:
            self.vision_timeouts+=1
            count('vision.timeouts')
            return ('run', None, True)
        except (ReviewError,OSError):
            pass
        return ('run', None, False)

    def _infer_batch(self,project_id,document_id,number,planned,pending,revision,version,decided):
        items=[dict(bounds=planned[index]['bounds'], region_index=index,
                    max_new_tokens=planned[index].get('max_new_tokens') or self.settings.vision_max_new_tokens)
               for index in pending]
        try:
            with span('page.vision.region', page=number, region=pending[0]):
                rows=self.review.recognize_batch(project_id,document_id,number,items,revision,profile_version=version)
            if len(rows)!=len(pending):
                raise ReviewError('视觉批结果数量不符。')
            for index,row in zip(pending, rows):
                candidate=row['result'] if row and row.get('status')=='succeeded' else None
                decided[index]=('run', candidate, False)
        except VisionTimeout:
            self.vision_timeouts+=1
            count('vision.timeouts')
            for index in pending:
                decided[index]=('run', None, True)
        except (ReviewError,OSError):
            for index in pending:
                decided[index]=('run', None, False)

    def _publish(self,document_id,number,revision,version,output):
        with self.database.publication('vision',number,document_id=document_id) as conn:
            # 与基础写入／组装采用同一锁顺序；不能先锁页再等待阅读记录。
            conn.execute('SELECT document_id FROM dd_agent.document_readings WHERE document_id=%s FOR UPDATE',
                         (document_id,)).fetchone()
            # 模型推理期间基础 OCR 可能被更新；锁住源页并重比修订后才追加机器版本。
            # 这里只追加 machine_pages，原始 document_pages 不被候选覆盖。
            locked=conn.execute('''SELECT p.page_number FROM dd_agent.document_pages p JOIN dd_agent.document_readings r
                ON p.document_id=r.document_id AND p.pipeline_version=r.pipeline_version
                WHERE p.document_id=%s AND p.page_number=%s FOR UPDATE OF p,r''',(document_id,number)).fetchone()
            current=conn.execute('SELECT raw_revision FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=%s',(document_id,number)).fetchone()
            if not locked or not current or current['raw_revision']!=revision:
                raise ReviewConflict('页面来源已变化。')
            conn.execute('INSERT INTO dd_agent.machine_pages(id,document_id,page_number,base_revision,profile_version,result) VALUES (%s,%s,%s,%s,%s,%s)',(uuid4(),document_id,number,revision,version,Jsonb(output)))
