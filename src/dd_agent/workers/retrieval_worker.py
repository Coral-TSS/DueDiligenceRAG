"""Offline Qwen inference in its own Python environment, with no DB credentials."""
# 由 reading.worker_records() 在 .venv-retrieval 中启动，只接受本地 JSON 清单。
# mode=profile：模型指纹；embed：一组文字；embed_pages：逐页流式向量；rerank：相关性排序。
# 模型权重只在本进程内复用，进程退出即释放；本文件不访问 PostgreSQL。
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dd_agent.performance import timed, span, count

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
INSTRUCTION = 'Retrieve evidence passages relevant to the due diligence question.'
PREFIX = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


@timed('fingerprint.embedding.worker')
def profile(directory, device='auto', dtype='auto', manifest_dir=None):
    # 内容指纹包含推理规则与关键库版本。float32 的字节序列保持不变，已有索引继续有效。
    # 设备与注意力不进入指纹。非 float32（例如 Ada 上 auto 选出的 bfloat16）必须换索引版本。
    # size 与 mtime 都未变时复用已存哈希，不再每次重读权重。
    # 主进程已经解析过设备时不要再 import torch，避免指纹进程和常驻模型同时占用显存。
    if device in ('cpu', 'cuda') and dtype in ('float32', 'float16', 'bfloat16'):
        spec = {'device': device, 'dtype': dtype}
    else:
        from dd_agent.devices import resolve_torch
        spec = resolve_torch('embedding', device, dtype)
    path = Path(directory)
    files = sorted(p for p in path.rglob('*') if p.is_file() and p.suffix in {'.json', '.safetensors', '.txt'} and '.cache' not in p.parts)
    if not any(p.suffix == '.safetensors' for p in files):
        raise ValueError('Local embedding weights missing')
    if spec['dtype'] == 'float32':
        marker = 'qwen-last-token-normalized-fp32-v1|' + INSTRUCTION + '|max4096|' + version('torch') + '|' + version('transformers')
    else:
        marker = f'qwen-last-token-normalized-v2|{spec["dtype"]}|' + INSTRUCTION + '|max4096|' + version('torch') + '|' + version('transformers')
    labeled = [(file.relative_to(path).as_posix().encode(), file) for file in files]
    from dd_agent.cache import verified_content_digest
    return verified_content_digest(marker.encode(), labeled, manifest_dir, 'embedding')


def _load_torch(kind, path, device, dtype, factory):
    from dd_agent.devices import load_with_attention, resolve_torch, torch_dtype
    spec = resolve_torch(kind, device, dtype)
    model, _attention = load_with_attention(lambda attention: factory(torch_dtype(spec['dtype']), attention), spec['attention'], kind)
    return model.to(spec['device']).eval()


@timed('model.embedding.load')
def embedding_model(path, device='auto', dtype='auto'):
    # 精度与设备由配置决定；CPU 或 Pascal 的 auto 仍是 float32。没有训练或云端 API。
    from transformers import AutoModel, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(path, padding_side='left', local_files_only=True)
    model = _load_torch('embedding', path, device, dtype, lambda dtype, attention: AutoModel.from_pretrained(
        path, torch_dtype=dtype, attn_implementation=attention, local_files_only=True))
    count('model.embedding.loads')
    return tokenizer, model


@timed('embedding.inference')
def embed(tokenizer, model, texts, token_budget=2048, batch_limit=32):
    # 左填充后每行最后一个位置是真实末 token，取其 hidden state 作为句向量并归一化。
    # 按 token 预算跨片段合批，不再固定每页 4 条。
    import torch
    from torch.nn.functional import normalize
    from dd_agent.batching import pack_ranges
    if not texts:
        return []
    encoded = tokenizer(texts, padding=False, truncation=False, add_special_tokens=True)
    ids = encoded['input_ids']
    lengths = [len(row) for row in ids]
    if any(length > 4096 for length in lengths):
        raise ValueError('Passage exceeds token budget; split before embedding')
    results = []
    count('embedding.passages', len(texts))
    for start, end in pack_ranges(lengths, int(token_budget), int(batch_limit)):
        inputs = tokenizer.pad({'input_ids': ids[start:end]}, padding=True, return_tensors='pt').to(model.device)
        count('embedding.tokens', int(inputs['attention_mask'].sum()))
        count('embedding.padded_tokens', int(inputs['input_ids'].numel()))
        count('embedding.batches')
        with torch.inference_mode():
            hidden = model(**inputs, use_cache=False).last_hidden_state[:, -1]
            results.extend(normalize(hidden, p=2, dim=1).tolist())
    return results


def embed_pages(tokenizer, model, pages, token_budget=2048, batch_limit=32):
    """把多页片段摊平后按 token 预算一次算完，再按页交回。"""
    flat = [(index, text) for index, page in enumerate(pages) for text in page['texts']]
    vectors = embed(tokenizer, model, [text for _, text in flat], token_budget, batch_limit)
    grouped = [[] for _ in pages]
    for (index, _), vector in zip(flat, vectors):
        grouped[index].append(vector)
    for page, rows in zip(pages, grouped):
        yield {'page_number': page['page_number'], 'vectors': rows}


@timed('model.reranker.initialize')
def reranker_model(path, device='auto', dtype='auto'):
    # 用 Qwen 因果模型比较 yes/no 的相对分数，判断片段与问题的相关程度。
    # 分数不是字段准确率，也不是法律事实为真的概率；只用于候选排序。
    from transformers import AutoModelForCausalLM, AutoTokenizer
    with span('model.reranker.load'):
        tokenizer = AutoTokenizer.from_pretrained(path, padding_side='left', local_files_only=True)
        model = _load_torch('reranker', path, device, dtype, lambda dtype, attention: AutoModelForCausalLM.from_pretrained(
            path, torch_dtype=dtype, attn_implementation=attention, local_files_only=True))
    count('model.reranker.loads')
    return tokenizer, model


def yes_no_logits(model, inputs, no_id, yes_id):
    """只取最后一个 token 的 yes／no 两行，避免在 GPU 上展开整个词表。"""
    import torch
    head = getattr(model, 'lm_head', None)
    body = getattr(model, 'model', None)
    if head is None or body is None or not hasattr(head, 'weight'):
        # 没有独立输出层时仍只保留最后一位；正常的 Qwen 重排模型不会走到这里。
        logits = model(**inputs, use_cache=False).logits[:, -1, :]
        return logits[:, [no_id, yes_id]]
    hidden = body(**inputs, use_cache=False).last_hidden_state[:, -1, :]
    index = torch.tensor([no_id, yes_id], device=head.weight.device)
    weight = head.weight.index_select(0, index)
    hidden = hidden.to(device=weight.device, dtype=weight.dtype)
    scores = hidden.matmul(weight.transpose(0, 1))
    bias = getattr(head, 'bias', None)
    if bias is not None:
        scores = scores + bias.index_select(0, index).to(dtype=scores.dtype, device=scores.device)
    return scores


@timed('rerank.worker')
def rerank_loaded(engine, query, texts, token_budget=4096):
    import torch
    from dd_agent.batching import pack_ranges
    tokenizer, model = engine
    prefix = tokenizer.encode(PREFIX, add_special_tokens=False)
    suffix = tokenizer.encode(SUFFIX, add_special_tokens=False)
    yes, no = (tokenizer.convert_tokens_to_ids(word) for word in ('yes', 'no'))
    rows, lengths = [], []
    for text in texts:
        ids = tokenizer(f'<Instruct>: {INSTRUCTION}\n<Query>: {query}\n<Document>: {text}', add_special_tokens=False, truncation=False)['input_ids']
        if len(ids) + len(prefix) + len(suffix) > 4096:
            raise ValueError('Reranking input exceeds token budget')
        row = prefix + ids + suffix
        rows.append(row)
        lengths.append(len(row))
    results = []
    # 一次查询的候选（例如 12 条）在 token 预算内一次算完，不再按 1 或 2 条切开。
    for start, end in pack_ranges(lengths, int(token_budget), max(1, len(texts))):
        inputs = tokenizer.pad({'input_ids': rows[start:end]}, padding=True, return_tensors='pt').to(model.device)
        count('rerank.pairs', end - start)
        count('rerank.batches')
        with torch.inference_mode(), span('rerank.inference'):
            chosen = yes_no_logits(model, inputs, no, yes)
            results.extend(chosen.softmax(dim=1)[:, 1].tolist())
    return results


def rerank(path, query, texts):
    return rerank_loaded(reranker_model(path), query, texts)


def main():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    mode = payload['mode']
    if mode == 'profile':
        # 仅计算指纹时不加载完整推理模型，但仍会读取权重文件内容。
        emit({'version': profile(payload['embedding'], payload.get('device', 'auto'), payload.get('dtype', 'auto'), payload.get('manifest_dir'))})
        return
    import torch
    torch.set_num_threads(int(payload.get('threads') or 4))
    device, dtype = payload.get('device', 'auto'), payload.get('dtype', 'auto')
    if mode == 'rerank':
        emit({'scores': rerank_loaded(reranker_model(payload['reranker'], device, dtype), payload['query'], payload['texts'], token_budget=int(payload.get('token_budget') or 4096))})
    elif mode in {'embed', 'embed_pages'}:
        tokenizer, model = embedding_model(payload['embedding'], device, dtype)
        budget, limit = int(payload.get('token_budget') or 2048), int(payload.get('batch_limit') or 32)
        if mode == 'embed':
            texts = payload['texts']
            if payload.get('query'):
                # 查询采用检索指令前缀；资料片段本身不追加这个 Query 前缀。
                texts = [f'Instruct: {INSTRUCTION}\nQuery:{q}' for q in texts]
            emit({'vectors': embed(tokenizer, model, texts, budget, limit)})
        else:
            # 本批共用同一个 Embedding 实例；多页片段按 token 预算合批后再逐页输出。
            for record in embed_pages(tokenizer, model, payload['pages'], budget, limit):
                emit(record)
    else:
        raise ValueError('Unknown retrieval operation')


if __name__ == '__main__':
    main()
