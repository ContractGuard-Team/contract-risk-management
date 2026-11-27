"""Write side of the Qdrant `legal_documents` collection: one dense vector (e5, cosine) + one sparse keyword vector
(BM25-style, IDF by Qdrant) per point, so rag/ can run hybrid search. Agents search current=true AND status=in_force,
or a date (as_of) through valid_from / valid_to / replaced_on.

Strict mode rejects filters on unindexed payload fields, so every filtered field gets an index up front.
"""
import os

from data_pipeline.ingestion.chunker import embedding_parts
from data_pipeline.router.collection_mapper import payload, point_id
from integrations.external.embedding_client import SPARSE_VECTOR, sparse_vector


class QdrantWriter:
    def __init__(self, client=None, collection=None, dimensions=None):
        if client is None:
            from qdrant_client import QdrantClient
            client = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.getenv("QDRANT_API_KEY") or None, timeout=60)
        from qdrant_client import models
        self.client, self.models = client, models
        self.collection = collection or os.getenv("QDRANT_COLLECTION", "legal_documents")
        self.dimensions = int(dimensions or os.getenv("EMBEDDING_DIMENSIONS", "1024"))

    def ensure_collection(self):
        m = self.models
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection, vectors_config=m.VectorParams(size=self.dimensions, distance=m.Distance.COSINE),
                sparse_vectors_config={SPARSE_VECTOR: m.SparseVectorParams(modifier=m.Modifier.IDF)})
            return
        params = self.client.get_collection(self.collection).config.params
        if SPARSE_VECTOR not in (params.sparse_vectors or {}):
            raise RuntimeError(f"Collection {self.collection} has no sparse vector '{SPARSE_VECTOR}': recreate it "
                               "(empty) so hybrid search works; see docs/ai_agents.md")

    def ensure_indexes(self):
        self.ensure_collection()
        m = self.models
        wanted = {"symbol": m.PayloadSchemaType.KEYWORD, "doc_class": m.PayloadSchemaType.KEYWORD,
                  "status": m.PayloadSchemaType.KEYWORD, "article_no": m.PayloadSchemaType.KEYWORD,
                  "current": m.PayloadSchemaType.BOOL, "effective_on": m.PayloadSchemaType.DATETIME,
                  "valid_from": m.PayloadSchemaType.DATETIME, "valid_to": m.PayloadSchemaType.DATETIME,
                  "replaced_on": m.PayloadSchemaType.DATETIME, "legal_references": m.PayloadSchemaType.KEYWORD}
        existing = set((self.client.get_collection(self.collection).payload_schema or {}).keys())
        for field, schema in wanted.items():
            if field not in existing:
                self.client.create_payload_index(self.collection, field, schema, wait=True)

    def _filter(self, **values):
        m = self.models
        return m.Filter(must=[m.FieldCondition(key=k, match=m.MatchValue(value=v)) for k, v in values.items()])

    def upsert(self, document, rows, parts, vectors, *, amended_by=None):
        """parts: [(row_index, part_no, text)] aligned with vectors."""
        m = self.models
        # Keywords also see the citation line, so "Điều 418 ... 91/2015/QH13" matches by number.
        points = [m.PointStruct(
            id=point_id(document["symbol"], rows[i]["article_no"], rows[i].get("valid_from"), part),
            vector={"": vector, SPARSE_VECTOR: m.SparseVector(**sparse_vector(
                f"Điều {rows[i]['article_no']} {document['title']} {document['symbol']}\n{text}"))},
            payload=payload(document, rows[i], part, text, amended_by=amended_by))
            for (i, part, text), vector in zip(parts, vectors)]
        for start in range(0, len(points), 64):
            self.client.upsert(self.collection, points[start:start + 64], wait=True)
        return len(points)

    def retire(self, symbol, article_no, valid_to=None):
        self.client.set_payload(self.collection, {"current": False, "valid_to": valid_to}, wait=True,
                                points=self._filter(symbol=symbol, article_no=article_no, current=True))

    def _ids(self, symbol, rows):
        # Same ids upsert() gave these article versions (deterministic): no date filter needed.
        return [point_id(symbol, r["article_no"], r.get("valid_from"), part)
                for r in rows for part in range(len(embedding_parts(r)))]

    def drop_versions(self, symbol, rows, keep=()):
        """Remove the points of these article versions, except ids `keep` rows reuse (missing points are ignored)."""
        ids = sorted(set(self._ids(symbol, rows)) - set(self._ids(symbol, keep)), key=str)
        if ids:
            self.client.delete(self.collection, points_selector=self.models.PointIdsList(points=ids), wait=True)

    def reopen_versions(self, symbol, rows):
        """Make retired article versions current again (undo of retire)."""
        if rows:
            self.client.set_payload(self.collection, {"current": True, "valid_to": None},
                                    points=self._ids(symbol, rows), wait=True)

    def set_status(self, symbol, status, **fields):
        self.client.set_payload(self.collection, {"status": status, **fields}, points=self._filter(symbol=symbol), wait=True)

    def flag_pending(self, symbol, article_no):
        self.client.set_payload(self.collection, {"has_pending_amendment": True}, wait=True,
                                points=self._filter(symbol=symbol, article_no=article_no, current=True))
