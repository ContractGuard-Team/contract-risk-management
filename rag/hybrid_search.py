"""Hybrid retrieval in one Qdrant call: dense (semantic, e5) and sparse (keyword, BM25-style) candidates under the
same filter, fused with Reciprocal Rank Fusion. Keywords catch exact numbers/terms ("Điều 418", "91/2015/QH13",
"phạt vi phạm") that embeddings blur; embeddings catch paraphrases.
"""
from integrations.external.embedding_client import SPARSE_VECTOR, sparse_vector


def hybrid_search(client, collection, *, dense, query, query_filter, limit):
    from qdrant_client import models as m
    keywords = sparse_vector(query, query=True)
    prefetch = [m.Prefetch(query=dense, filter=query_filter, limit=limit)]
    if keywords["indices"]:
        prefetch.append(m.Prefetch(query=m.SparseVector(**keywords), using=SPARSE_VECTOR, filter=query_filter, limit=limit))
    result = client.query_points(collection, prefetch=prefetch, query=m.FusionQuery(fusion=m.Fusion.RRF),
                                 limit=limit, with_payload=True)
    return result.points


def lookup(client, collection, *, query_filter, limit):
    """No question, only symbol/article filters: return the matching points as stored."""
    points, _ = client.scroll(collection, scroll_filter=query_filter, limit=limit, with_payload=True)
    return points
