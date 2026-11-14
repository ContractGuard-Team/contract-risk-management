"""Cross-encoder rerank of the fused candidates: reads question and article together, so it orders by actual
relevance rather than by vector closeness. Local ONNX model via fastembed (no API key, text stays on the host).

RAG_RERANK_MODEL (default jinaai/jina-reranker-v2-base-multilingual, ~1.1 GB, downloaded once into
EMBEDDING_CACHE_DIR). Set it empty to keep the fusion order (low-RAM hosts).
"""
import os

DEFAULT_MODEL = "jinaai/jina-reranker-v2-base-multilingual"


class CrossEncoderReranker:
    def __init__(self, model=None, *, engine=None):
        self.model_name = model or os.getenv("RAG_RERANK_MODEL", DEFAULT_MODEL)
        if engine is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            engine = TextCrossEncoder(self.model_name, cache_dir=os.getenv("EMBEDDING_CACHE_DIR") or None)
        self.engine = engine

    def scores(self, query, texts):
        return [float(s) for s in self.engine.rerank(query, texts, batch_size=8)] if texts else []


def default_reranker():
    """Process-wide instance, or None when RAG_RERANK_MODEL is set empty."""
    if not os.getenv("RAG_RERANK_MODEL", DEFAULT_MODEL).strip():
        return None
    from integrations.external.embedding_client import shared
    return shared("reranker", CrossEncoderReranker)
