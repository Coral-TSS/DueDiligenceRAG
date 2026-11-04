# 06 · 检索 RAG：`retrieval.py` + `workers/retrieval_worker.py`

这是全项目的"RAG 核心"。先建立两个词的概念：
- **Embedding（向量化）**：把一段文字变成 1024 个浮点数。意思相近的文字，向量距离就近——这是"语义搜索"的基础。
- **Rerank（重排）**：另一个模型，拿"问题 + 候选片段"两两比较，输出 0–1 的相关分。比向量距离更准，但太慢，只能给少量候选打分。

分工：`retrieval.py` 管**来源/版本/SQL**（哪段文字、来自哪页、能不能信），`retrieval_worker.py` 管**模型推理**（算向量、打分）。

---

## 一、`retrieval.py`（284 行）

### 1. 分块：`split_text()`

```python
CHUNK_VERSION = 'page-paragraph-700-overlap80-v1'
```

分块规则的"名号"已说明策略：**按页、段落优先、700 字符上限、80 字符重叠、第 1 版**。改规则必须同步改版本号——否则新旧分块会被当成同一个索引混在一起。

```python
def split_text(text):
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1                          # 跳过空白
        end = min(start + 700, len(text))
        boundary = text.rfind('\n', start + 350, end)
        if end < len(text) and boundary > start:
            end = boundary + 1                  # 优先在 350–700 之间的最后一个换行处断开
        trimmed = end
        while trimmed > start and text[trimmed-1].isspace():
            trimmed -= 1                        # 去掉块尾空白
        chunks.append(dict(text=text[start:trimmed], start_offset=start, end_offset=trimmed))
        if end == len(text): break
        start = end if text[end-1].isspace() else end - 80    # 换行断则无缝接续；硬切则回退 80 字符重叠
    return chunks
```

设计意图逐条看：
- **段落优先**：在换行处切开，不会把一句话拦腰斩断（法律条文被斩断，检索出来就断了语义）。
- **80 字符重叠**：硬切时两块共享末尾/开头的 80 字符，跨界的关键信息在两块里都出现，不至于只落在一块里。
- **start_offset / end_offset**：记录每块在原文的精确位置——检索结果可以**一键回跳到原文出处**（`text[start:end]` 直接还原）。
- 注释的诚实提醒：单位是 Python 字符不是模型 token，也不跨页、不识别法律条款结构——"不要误以为这是语义分块"。

### 2. 向量安检：`valid_vector()`

```python
if not isinstance(values, list) or len(values) != 1024:
    raise RetrievalError('向量维度不符...')
if any(not isinstance(v, (float,int)) or not math.isfinite(v) for v in values):
    raise RetrievalError('向量含无效数值。')     # NaN、inf 全拦下
if not any(values):
    raise RetrievalError('向量不能全为零。')
return '[' + ','.join(str(float(v)) for v in values) + ']'    # 拼成 pgvector 的文本格式
```

三道安检（维度、有限值、非全零）后拼成 `'[0.1,0.2,...]'` 字符串——pgvector 接受这种文本表示入库。**为什么这么严？** 一个 NaN 混进向量列，余弦距离计算会产出垃圾结果甚至让查询报错；全零向量和任何向量距离都一样，是"假向量"。

### 3. 模型锁：`model_slot()`

```python
@contextmanager
def model_slot(directory):
    with (directory / 'retrieval.lock').open('a+b') as lock:
        ...
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)     # Windows 文件锁
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)      # Linux/Mac 文件锁
        except OSError:
            raise RetrievalError('本地检索模型正在处理其他任务，请稍后重试。') from None
```

- `@contextmanager` 装饰器：`with model_slot(dir):` 进入时加锁，退出（哪怕异常）时解锁。
- `LK_NBLCK` / `LOCK_NB`：**非阻塞**——锁被占着就立刻失败，不排队等。这是有意的选择：模型计算动辄几十秒，排队的体验反而更差，不如明确告诉用户"忙，稍后再试"。
- 跨进程的文件锁：主应用和 CLI 诊断命令共用同一个 data_dir，谁先拿到谁干活。
- 注意 `raise ... from None`：抛新异常时不显示旧异常链——用户只需要看到"忙"，不需要看到 Windows 内部报错。

### 4. `LocalModels` —— 推理请求的打包器

```python
def records(self, mode, **payload):
    manifest = dict(mode=mode, embedding=str(self.settings.embedding_dir.resolve()),
                    reranker=str(self.settings.reranker_dir.resolve()), **payload)
    try:
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as directory:
            yield from worker_records(self.settings.retrieval_python, 'retrieval_worker', manifest, directory)
    except ReadingError:
        raise RetrievalError('本地检索模型运行失败...') from None

def one(self, mode, **payload):
    records = list(self.records(mode, **payload))     # 必须耗尽生成器！
    if len(records) != 1:
        raise RetrievalError('检索工作进程返回结果数量不符。')
    return records[0]
```

- 把模式（profile/embed/rerank）+ 模型路径 + 数据打包成清单，交给通用 worker 协议跑。
- `one()` 的注释是重点：**"不只取第一条输出：必须确认进程完整退出，防止成功一行后崩溃仍被当作完成"**。用 `list()` 把生成器耗尽——子进程正常退出（exit 0）且恰好返回 1 条，才算成功。
- `embed_pages()` 保持生成器形态：一页向量出来了，主进程立刻可以入库这一页（后面 index() 靠这个实现逐页提交）。

### 5. `CURRENT` —— 全文件最精妙的 SQL 片段

```python
CURRENT = '''FROM dd_agent.indexed_pages i
 JOIN dd_agent.documents d ON d.id=i.document_id
 JOIN dd_agent.effective_document_pages p ON p.document_id=i.document_id AND p.page_number=i.page_number
   AND p.pipeline_version=i.pipeline_version AND p.updated_at=i.source_updated_at
   AND p.source_revision=i.source_revision AND p.status='succeeded'
 WHERE d.project_id=%s AND i.index_version=%s'''
```

**"当前有效索引页"的定义**：一条页级索引记录，必须同时满足——
1. 属于当前项目、当前索引版本；
2. 源页的阅读版本、更新时间、修订摘要**全部**与 effective 视图当前值一致；
3. 源页状态是成功。

这个片段在建库跳过判断和查询过滤里**复用同一份文本**——保证"建库时认为有效的页"和"查询时认为有效的页"用的是同一个标准。注释总结："不匹配的旧向量可能仍在表中留档，但不会被当前检索返回"。

### 6. `index()` —— 逐页原子发布

```python
def index(self, project_id, document_id, pages, checkpoint=None):
    project_id, document_id = UUID(str(project_id)), UUID(str(document_id))
    if not pages or len(set(pages)) > 20 or any(not isinstance(n, int) or n < 1 for n in pages):
        raise ValueError('请指定 1–20 个有效物理页码。')
    if self.database.document(project_id, document_id) is None:
        raise RetrievalError('材料不存在或不属于当前项目。')
```

老三样：转 UUID、参数范围校验、**再次校验项目归属**（注释：外部调用即使传了合法材料 UUID，也必须再查归属）。

```python
with model_slot(self.settings.data_dir):        # 占住模型资源
    version = self.version()
    ...
    requests = [dict(page_number=n, texts=[c['text'] for c in chunks]) for n, chunks in pending.items()]
    outputs = self.models.embed_pages(requests) if hasattr(...) else (...兜底...)
    for output in outputs:
        try:
            self.publish(document_id, snapshots[number], version, pending[number], output['vectors'])
            result['indexed_pages'].append(number)
            if checkpoint: checkpoint('embedding', number)
        except RetrievalError:
            result['failed_pages'].append(number)     # 单页失败不中断整批
    except RetrievalError:
        pass   # Successfully committed pages survive a later worker failure.
    finally:
        if 'outputs' in locals() and hasattr(outputs,'close'): outputs.close()   # 提前结束时清理子进程
```

- 单页 publish 成功 = 这一页的事务已提交，后面页崩了也不回滚前面的（**发布粒度是页**）。
- `finally` 里关闭生成器：暂停等情况下提前退出，worker 子进程会被 worker_records 的 finally 杀干净（呼应 04 篇）。

### 7. `publish()` —— 写库前的最后一次核对

```python
with self.database.connect() as conn:
    current = conn.execute('''SELECT p.updated_at, p.pipeline_version FROM dd_agent.document_pages p
        JOIN dd_agent.document_readings r ON ...
        WHERE p.document_id=%s AND p.page_number=%s AND p.status='succeeded'
        FOR UPDATE OF p, r''', ...).fetchone()
    effective = conn.execute('SELECT * FROM dd_agent.effective_document_pages WHERE ...', ...).fetchone()
    if current is None or effective is None or effective['status'] != 'succeeded' \
       or effective['source_revision'] != snapshot['source_revision']:
        raise RetrievalError('页面已更新，请重新建立该页索引。')
```

`FOR UPDATE` 行锁的意图（注释）：**"识别／增强可能在 Embedding 推理期间更新来源，因此写入前还要再次比较修订"**。一个区域视觉增强可能跑 300 秒，期间如果基础页被更新，snapshot 里的修订号就对不上了——拒发，让这页下次重算。然后 DELETE 旧的同版本记录 + INSERT 页级登记 + 逐块写入带向量的 evidence_chunks。

### 8. `search()` —— 两阶段检索全程

```python
total = conn.execute('''SELECT coalesce(sum(r.page_count),0) AS total_pages,
    count(*) FILTER (WHERE r.page_count IS NULL) AS unread_documents ...''').fetchone()
indexed = conn.execute('SELECT count(*) AS n ' + CURRENT, ...).fetchone()['n']
coverage = {**total, 'indexed_pages': indexed,
            'complete': bool(indexed) and indexed == total['total_pages'] and not total['unread_documents']}
```

先算"覆盖度"：项目里登记了多少页、还有几份材料没预检、当前版本索引了多少页。网页上"可检索 X / 已知 Y 页；Z 份材料尚未预检"就是它。**注释强调这只是页级粗略覆盖，不是质量统计**。

```python
rows = conn.execute('''SELECT ..., 1-(c.embedding <=> %s::public.vector) AS similarity
    FROM dd_agent.evidence_chunks c
    JOIN (...) p ON ...
    ORDER BY c.embedding <=> %s::public.vector, c.id LIMIT 12''', ...).fetchall()
```

第一阶段：`<=>` 是 pgvector 的余弦距离操作符，`1-距离` 就是相似度。按距离升序取 12 块。

```python
scores = self.models.rerank(query, [r['text'] for r in rows])     # 第二阶段：重排
if len(scores) != len(rows) or any(not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
    raise RetrievalError('重排序输出无效，请重试。')
```

12 块全量送 reranker 打 0–1 的分。**分数范围都要验**——模型输出异常宁可报错不给垃圾。

```python
with self.database.connect() as conn:
    current_ids = {r['id'] for r in conn.execute('''SELECT c.id FROM dd_agent.evidence_chunks c
        JOIN (...) live ON ... WHERE c.id=ANY(%s)''', ...)}
rows = [r for r in rows if r['id'] in current_ids]
```

**返回前的二次新鲜度检查**——这是本文件最见功力的一处。注释："Reranking can be slow: recheck source freshness immediately before publishing. 排序耗时期间来源可能变化：最终响应前再剔除失效证据，宁可少返回几条。" rerank 可能耗时几十秒，期间某页可能被重新 OCR 了——用 CURRENT 再筛一遍候选 id，把过期块剔掉。

```python
response['results'] = sorted(rows, key=lambda r: (-r['rerank_score'], -r['similarity'], str(r['id'])))[:5]
```

最终排序：rerank 分优先、相似度次之、id 兜底（保证结果顺序**确定性**——同样的输入永远同样的输出，方便测试和排查）。取前 5，附上文件名和 `/reader?...` 的来源跳转链接。

---

## 二、`workers/retrieval_worker.py`（119 行）—— Qwen3 双模型小工

### 指纹：`profile()`

```python
files = sorted(p for p in path.rglob('*') if p.is_file() and p.suffix in {'.json','.safetensors','.txt'} and '.cache' not in p.parts)
digest.update(('qwen-last-token-normalized-fp32-v1|' + INSTRUCTION + '|max4096|' + version('torch') + '|' + version('transformers')).encode())
for file in files:
    ...digest.update(block)
```

Embedding 权重**全部文件逐字节**哈希 + 推理规则（"末 token 归一化 fp32"）+ torch/transformers 版本。注释承认代价：每次查询都要读一遍几个 GB 的权重算指纹，I/O 不小（RG-05 是待优化项）。但换来的是：**换任何一克模型权重，索引版本立刻变，旧向量自动失效**。

### `embed()` —— 怎么把文字变向量

```python
tokenizer = AutoTokenizer.from_pretrained(path, padding_side='left', local_files_only=True)
model = AutoModel.from_pretrained(path, torch_dtype=torch.float32, local_files_only=True).to('cpu').eval()

for start in range(0, len(texts), 4):                       # 每批 4 条
    inputs = tokenizer(texts[start:start+4], padding=True, truncation=False, return_tensors='pt')
    if inputs['input_ids'].shape[1] > 4096:
        raise ValueError('Passage exceeds token budget; split before embedding')
    with torch.inference_mode():
        hidden = model(**inputs, use_cache=False).last_hidden_state[:, -1]   # 取最后一个 token
        results.extend(normalize(hidden, p=2, dim=1).tolist())
```

三个关键点：
- **padding_side='left'（左填充）**：批量推理时短句要补齐到同一长度。左填充保证"最后一个位置"永远是每个句子自己真实的末 token——然后取 `last_hidden_state[:, -1]` 作为整句向量。这是 Qwen3-Embedding 的标准用法（补齐在右边的话，末位置会是填充符！）。
- **truncation=False + 4096 上限**：超长直接报错，**绝不默默截断**——截掉的可能就是关键证据，应该由上游分块解决。
- 批 4 是内存权衡：CPU fp32 下批次越大内存峰值越高。

### `rerank()` —— yes/no 打分法

```python
PREFIX = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query...<|im_end|>\n<|im_start|>user\n'
SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
yes, no = (tokenizer.convert_tokens_to_ids(word) for word in ('yes','no'))
...
logits = model(**inputs, logits_to_keep=1, use_cache=False).logits[:, -1]
results.extend(logits[:, [no, yes]].softmax(dim=1)[:, 1].tolist())
```

妙处：reranker 本质是个对话模型。构造 prompt 让它判断"文档是否满足查询"，取它回答**第一个 token 的 logits**，比较 `yes` 和 `no` 两个 token 的概率——softmax 后 `yes` 的概率就是 0–1 的相关分。不需要真的生成文字，一步出分。批 2 是因为要保留全部词表的 logits（几万个数 × 序列长度），CPU 内存吃不消更大的批。

### 查询和资料的不对称处理

```python
if payload.get('query'):
    texts = [f'Instruct: {INSTRUCTION}\nQuery:{q}' for q in texts]    # 查询加指令前缀
```

`INSTRUCTION = 'Retrieve evidence passages relevant to the due diligence question.'`——**查询向量带任务说明前缀，资料片段向量不带**。这是 Qwen3-Embedding 官方的非对称检索用法：告诉模型"这是个尽调检索任务"，能显著提升召回质量。理解错了（两边都加或都不加）检索质量会明显下降。

---

## 本篇小结

- `retrieval.py`：分块保原文偏移（可回跳）、向量三重安检、文件锁防并发、**CURRENT 统一"有效性"定义**、逐页原子发布、rerank 后二次新鲜度过滤、排序确定性。
- `retrieval_worker.py`：权重逐字节指纹、左填充取末 token、超长报错不截断、yes/no 概率当相关分、查询带指令前缀。
- 一句话记住：**这里返回的永远是"候选证据 + 出处链接"，不是"答案"**。
