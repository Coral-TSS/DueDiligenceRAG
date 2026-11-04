"""仅计算本地 token 数；不加载 Embedding／重排模型权重。"""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from dd_agent.workers.retrieval_worker import INSTRUCTION,PREFIX,SUFFIX


def main():
    from transformers import AutoTokenizer
    payload=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    texts=payload['texts']
    if not 1<=len(texts)<=128 or any(not isinstance(t,str) or len(t)>700 for t in texts):
        raise ValueError('Tokenizer batch exceeds budget')
    embedding=AutoTokenizer.from_pretrained(payload['embedding'],local_files_only=True)
    reranker=AutoTokenizer.from_pretrained(payload['reranker'],local_files_only=True)
    prefix=len(reranker.encode(PREFIX,add_special_tokens=False))
    suffix=len(reranker.encode(SUFFIX,add_special_tokens=False))
    result=[]
    for text in texts:
        # 与当前 rerank_loaded 的格式一致；额外问题预算由父子分块入口预留。
        pair=f'<Instruct>: {INSTRUCTION}\n<Query>: \n<Document>: {text}'
        result.append(dict(embedding=len(embedding.encode(text)),
            reranker=prefix+len(reranker.encode(pair,add_special_tokens=False))+suffix))
    print(json.dumps(dict(counts=result)),flush=True)


if __name__=='__main__':main()
