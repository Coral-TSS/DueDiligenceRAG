"""Offline Qwen inference in its own Python environment, with no DB credentials."""
# 由 reading.worker_records() 在 .venv-retrieval 中启动，只接受本地 JSON 清单。
# mode=profile：模型指纹；embed：一组文字；embed_pages：逐页流式向量；rerank：相关性排序。
# 模型权重只在本进程内复用，进程退出即释放；本文件不访问 PostgreSQL。
import hashlib
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
def profile(directory, device='auto', dtype='auto'):
    # 完整读取 Embedding 文件做内容指纹，包含推理规则与关键库版本。
    # 这比仅看目录名可靠，但每次查询都重算会有较大 I/O 开销，属于 RG-05 优化点。
    # 设备与注意力不进入指纹：float32 下换 GPU 不应让整卷重新嵌入。
    # 非 float32（例如 Ada 上 auto 选出的 bfloat16）数值不同，必须换索引版本。
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
    digest = hashlib.sha256()
    if spec['dtype'] == 'float32':
        marker = 'qwen-last-token-normalized-fp32-v1|' + INSTRUCTION + '|max4096|' + version('torch') + '|' + version('transformers')
    else:
        marker = f'qwen-last-token-normalized-v2|{spec["dtype"]}|' + INSTRUCTION + '|max4096|' + version('torch') + '|' + version('transformers')
    digest.update(marker.encode())
    for file in files:
        digest.update(file.relative_to(path).as_posix().encode())
        with file.open('rb') as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                digest.update(block)
                count('fingerprint.embedding.bytes', len(block))
    return digest.hexdigest()


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
def embed(tokenizer, model, texts):
    # 左填充后每行最后一个位置是真实末 token，取其 hidden state 作为句向量并归一化。
    # 小批量 4 控制峰值内存；这不是 4 个页面并行 worker。
    import torch
    from torch.nn.functional import normalize
    results = []
    count('embedding.passages', len(texts))
    for start in range(0, len(texts), 4):
        inputs = tokenizer(texts[start:start + 4], padding=True, truncation=False, return_tensors='pt').to(model.device)
        count('embedding.tokens', int(inputs['attention_mask'].sum()))
        count('embedding.padded_tokens', int(inputs['input_ids'].numel()))
        if inputs['input_ids'].shape[1] > 4096:
            # 不允许默默截掉尾部证据；超预算应由上游分块／错误处理解决。
            raise ValueError('Passage exceeds token budget; split before embedding')
        with torch.inference_mode():
            hidden = model(**inputs, use_cache=False).last_hidden_state[:, -1]
            results.extend(normalize(hidden, p=2, dim=1).tolist())
    return results


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
def rerank_loaded(engine, query, texts):
    import torch
    tokenizer, model = engine
    prefix = tokenizer.encode(PREFIX, add_special_tokens=False)
    suffix = tokenizer.encode(SUFFIX, add_special_tokens=False)
    yes, no = (tokenizer.convert_tokens_to_ids(word) for word in ('yes', 'no'))
    results = []
    # GPU 上一次一条，避免长序列激活和词表 logits 叠到 8GB 上限。CPU 仍两条一批。
    on_gpu = next(model.parameters()).device.type == 'cuda'
    batch = 1 if on_gpu else 2
    for start in range(0, len(texts), batch):
        pairs = [f'<Instruct>: {INSTRUCTION}\n<Query>: {query}\n<Document>: {text}' for text in texts[start:start + batch]]
        ids = tokenizer(pairs, add_special_tokens=False, truncation=False)['input_ids']
        if any(len(row) + len(prefix) + len(suffix) > 4096 for row in ids):
            raise ValueError('Reranking input exceeds token budget')
        inputs = tokenizer.pad({'input_ids': [prefix + row + suffix for row in ids]}, padding=True, return_tensors='pt').to(model.device)
        count('rerank.pairs', len(pairs))
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
        emit({'version': profile(payload['embedding'], payload.get('device', 'auto'), payload.get('dtype', 'auto'))})
        return
    import torch
    torch.set_num_threads(int(payload.get('threads') or 4))
    device, dtype = payload.get('device', 'auto'), payload.get('dtype', 'auto')
    if mode == 'rerank':
        emit({'scores': rerank_loaded(reranker_model(payload['reranker'], device, dtype), payload['query'], payload['texts'])})
    elif mode in {'embed', 'embed_pages'}:
        tokenizer, model = embedding_model(payload['embedding'], device, dtype)
        if mode == 'embed':
            texts = payload['texts']
            if payload.get('query'):
                # 查询采用检索指令前缀；资料片段本身不追加这个 Query 前缀。
                texts = [f'Instruct: {INSTRUCTION}\nQuery:{q}' for q in texts]
            emit({'vectors': embed(tokenizer, model, texts)})
        else:
            # 本批共用同一个 Embedding 实例，一页完成就输出，主进程可马上事务发布。
            for page in payload['pages']:
                emit({'page_number': page['page_number'], 'vectors': embed(tokenizer, model, page['texts'])})
    else:
        raise ValueError('Unknown retrieval operation')


if __name__ == '__main__':
    main()
