"""每模型环境共用协议入口；hello／所属进程确认后才 import 重依赖。"""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from dd_agent.worker_protocol import serve


def load(config):
    kind=config['kind']
    if kind=='ocr':
        from dd_agent.workers.ocr_worker import load_ocr
        return kind,load_ocr(config)
    import torch
    torch.set_num_threads(config.get('threads',4))
    if kind=='embedding':
        from dd_agent.workers.retrieval_worker import embedding_model
        return kind,embedding_model(config['embedding'],config.get('device','auto'),config.get('dtype','auto'))
    if kind=='reranker':
        from dd_agent.workers.retrieval_worker import reranker_model
        return kind,reranker_model(config['reranker'],config.get('device','auto'),config.get('dtype','auto'))
    if kind=='vision':
        from dd_agent.workers.vision_worker import load_engine
        return kind,(load_engine(config),config['max_new_tokens'])
    raise ValueError('Model kind not yet adapted')


def infer(engine,operation,payload):
    kind,model=engine
    if kind=='embedding':
        from dd_agent.workers.retrieval_worker import embed, INSTRUCTION
        tokenizer,weights=model
        if operation=='embed':
            texts=payload['texts']
            if payload.get('query'):texts=[f'Instruct: {INSTRUCTION}\nQuery:{q}' for q in texts]
            yield {'vectors':embed(tokenizer,weights,texts)}
        elif operation=='embed_pages':
            for page in payload['pages']:
                yield {'page_number':page['page_number'],'vectors':embed(tokenizer,weights,page['texts'])}
        else:raise ValueError('Unsupported embedding operation')
    elif kind=='reranker' and operation=='rerank':
        from dd_agent.workers.retrieval_worker import rerank_loaded
        yield {'scores':rerank_loaded(model,payload['query'],payload['texts'])}
    elif kind=='ocr' and operation=='recognize':
        from dd_agent.workers.ocr_worker import recognize_pages
        yield from recognize_pages(model,payload)
    elif kind=='vision' and operation=='recognize':
        from dd_agent.workers.vision_worker import recognize
        engine,limit=model
        if payload['max_new_tokens']!=limit:raise ValueError('Vision profile token budget changed')
        yield recognize(payload,engine=engine)
    else:raise ValueError('Unsupported model')


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--serve':raise SystemExit('Use --serve nonce')
    raise SystemExit(serve(load,infer,sys.argv[2]))
