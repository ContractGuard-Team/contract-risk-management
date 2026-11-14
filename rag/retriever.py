"""RAG over the Vietnamese legal store for Agent 2/3, served by the MCP tool `legal_search`.

question -> metadata filter -> hybrid search (dense + keyword, RRF) -> group parts into whole Điều -> cross-encoder
rerank -> evidence with citation and validity. Read-only; collection written by data_pipeline/.
"""
from datetime import datetime, timezone
import os

from common.exceptions import IntegrationNotConfigured
from rag.evidence_formatter import evidence_item
from rag.hybrid_search import hybrid_search, lookup
from rag.metadata_filter import build_filter

MAX_QUERY = 1000


class LegalRetriever:
    def __init__(self, *, client=None, collection=None, embedder=None, reranker=None, timeout=20):
        self.collection = collection or os.getenv("QDRANT_COLLECTION", "legal_documents")
        if client is None:
            if not os.getenv("QDRANT_URL"):
                raise IntegrationNotConfigured("TODO: set QDRANT_URL (and QDRANT_API_KEY) to connect Qdrant")
            from qdrant_client import QdrantClient
            client = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.getenv("QDRANT_API_KEY") or None, timeout=timeout)
        self.client = client
        self._embedder, self._reranker = embedder, reranker  # models load on the first question (seconds, GBs)

    def _embed(self, query):
        if self._embedder is None:
            from integrations.external.embedding_client import LocalEmbedder, shared
            self._embedder = shared("embedder", LocalEmbedder)
        return self._embedder.embed_query(query)

    def _rerank(self, query, texts):
        if self._reranker is None:
            from rag.reranker import default_reranker
            self._reranker = default_reranker() or False
        return self._reranker.scores(query, texts) if self._reranker else None

    def _articles(self, points):
        """Points (one per part) -> whole articles in first-hit order, text joined from every stored part."""
        order, keys = {}, []
        for rank, point in enumerate(points, 1):
            p = point.payload or {}
            key = (p.get("symbol"), p.get("article_no"), p.get("valid_from"))
            if key not in order:
                order[key] = {"payload": p, "rank": rank, "score": getattr(point, "score", None)}  # scroll records have no score
                keys.append(key)
        if not keys:
            return []
        from qdrant_client import models as m
        wanted = m.Filter(should=[m.Filter(must=[m.FieldCondition(key="symbol", match=m.MatchValue(value=s)),
                                                 m.FieldCondition(key="article_no", match=m.MatchValue(value=a))])
                                  for s, a in dict.fromkeys((s, a) for s, a, _ in keys)])
        parts, offset = {}, None
        while True:  # every version of these articles; keep the parts of the matched version
            batch, offset = self.client.scroll(self.collection, scroll_filter=wanted, limit=256, offset=offset,
                                               with_payload=True)
            for point in batch:
                p = point.payload or {}
                parts.setdefault((p.get("symbol"), p.get("article_no"), p.get("valid_from")), {})[p.get("part") or 0] = p.get("text") or ""
            if offset is None:
                break
        for key in keys:
            stored = parts.get(key) or {0: order[key]["payload"].get("text") or ""}
            order[key]["text"] = "\n".join(stored[i] for i in sorted(stored))
        return [order[k] for k in keys]

    def search(self, query="", *, limit=5, symbol="", article_no="", doc_class="", as_of="", include_inactive=False):
        query = (query or "").strip()
        if len(query) > MAX_QUERY:
            raise ValueError(f"query must be at most {MAX_QUERY} characters")
        if not query and not (symbol and article_no):
            raise ValueError("Give a question, or symbol + article_no to fetch one Điều")
        limit = max(1, min(int(limit), 20))
        query_filter = build_filter(symbol=symbol, article_no=article_no, doc_class=doc_class, as_of=as_of,
                                    include_inactive=include_inactive)
        if query:
            points = hybrid_search(self.client, self.collection, dense=self._embed(query), query=query,
                                   query_filter=query_filter, limit=max(limit * 6, 30))
        else:
            points = lookup(self.client, self.collection, query_filter=query_filter, limit=50)
        candidates = self._articles(points)[:max(limit * 3, 15)]
        scores = None
        if query and candidates:
            scores = self._rerank(query, [f"{c['payload'].get('heading') or ''}\n{c['text'][:2000]}" for c in candidates])
            if scores is not None:
                for candidate, score in zip(candidates, scores):
                    candidate["score"] = score
                candidates.sort(key=lambda c: c["score"], reverse=True)
        retrieved_at = datetime.now(timezone.utc).isoformat()
        evidence = [evidence_item(c, retrieved_at) for c in candidates[:limit]]
        return {"query": query, "filters": {"symbol": symbol, "article_no": article_no, "doc_class": doc_class,
                                            "as_of": as_of, "include_inactive": include_inactive},
                "reranked": scores is not None, "evidence": evidence,
                # Empty evidence is a finding: the store has no matching text, so the agent may fall back to web_search.
                "found": len(evidence)}
