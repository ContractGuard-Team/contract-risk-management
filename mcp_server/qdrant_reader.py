"""Qdrant vector search for Agent 2/3 through the official qdrant-client. Search and collection info only.

Low-level access (dense vector only). Agents normally use legal_search (rag/: hybrid + rerank + evidence).
"""
import os
import re

from common.exceptions import IntegrationNotConfigured


class QdrantReader:
    def __init__(self, url=None, api_key=None, collection=None, *, client=None, timeout=10, embedder=None):
        self.collection = os.getenv("QDRANT_COLLECTION", "") if collection is None else collection
        self._embedder = embedder  # created on the first text query: the model load takes seconds and ~2 GB RAM
        if client is None:
            url = os.getenv("QDRANT_URL", "") if url is None else url
            if not url:
                raise IntegrationNotConfigured("TODO: set QDRANT_URL (and QDRANT_API_KEY) to connect Qdrant")
            from qdrant_client import QdrantClient
            client = QdrantClient(url=url, api_key=(os.getenv("QDRANT_API_KEY") if api_key is None else api_key) or None,
                                  timeout=timeout)
        self.client = client

    def _name(self, collection):
        name = collection or self.collection
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name or ""):
            raise ValueError("Set QDRANT_COLLECTION or pass a valid collection name")
        return name

    def search(self, vector=None, *, query=None, collection="", limit=5, query_filter=None):
        """query: text, embedded here with the pipeline's model. query_filter=None -> current articles of laws in force;
        pass {} to search everything (history, replaced laws)."""
        from qdrant_client import models
        if query:
            if not isinstance(query, str) or len(query) > 2000:
                raise ValueError("query must be text of at most 2000 characters")
            if self._embedder is None:
                from integrations.external.embedding_client import LocalEmbedder, shared
                self._embedder = shared("embedder", LocalEmbedder)  # same instance as legal_search (RAG)
            vector = self._embedder.embed_query(query)
        if not vector or not all(isinstance(v, (int, float)) for v in vector):
            raise ValueError("Give a text query or a non-empty vector")
        if query_filter is None:
            query_filter = {"must": [{"key": "current", "match": {"value": True}},
                                     {"key": "status", "match": {"value": "in_force"}}]}
        result = self.client.query_points(
            collection_name=self._name(collection), query=[float(v) for v in vector],
            limit=max(1, min(int(limit), 50)), with_payload=True,
            query_filter=models.Filter.model_validate(query_filter) if query_filter else None)
        return [{"id": p.id, "score": p.score, "payload": p.payload or {}} for p in result.points]

    def info(self, collection=""):
        name = self._name(collection)
        details = self.client.get_collection(name)
        vectors = details.config.params.vectors
        shapes = {key: v for key, v in vectors.items()} if isinstance(vectors, dict) else {"": vectors}
        return {"collection": name, "status": str(details.status.value if hasattr(details.status, "value") else details.status),
                "points_count": details.points_count,
                "vectors": {key: {"size": v.size, "distance": str(v.distance.value)} for key, v in shapes.items()},
                "payload_indexes": sorted((details.payload_schema or {}).keys())}
