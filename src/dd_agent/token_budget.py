"""实际 tokenizer 预算检查，独立于重模型计算许可。"""
from .assembly import digest
from .model_runtime import worker_spec
from .reading import worker_records


class LocalTokenBudget:
    def __init__(self,settings):self.settings=settings

    def profile(self):
        return digest(dict(policy='embedding4096-rerank2048-reserve2048-v1',
            embedding=worker_spec(self.settings,'embedding').profile,
            reranker=worker_spec(self.settings,'reranker').profile))

    def counts(self,texts):
        if not texts:return []
        result=list(worker_records(self.settings.retrieval_python,'tokenizer_worker',
            dict(embedding=str(self.settings.embedding_dir),reranker=str(self.settings.reranker_dir),texts=texts),
            self.settings.data_dir/'tokenizer',timeout_seconds=120))
        if len(result)!=1 or not isinstance(result[0].get('counts'),list):raise ValueError('Tokenizer 输出不完整。')
        return result[0]['counts']
