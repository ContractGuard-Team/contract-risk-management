def aggregate_contract(contract, cache, key):
    chunks = []
    for chunk in contract["chunks"]:
        cached = cache.get(key, "classification", chunk["chunk_id"])
        if cached is None:
            raise ValueError(f"Cannot aggregate: missing chunk {chunk['chunk_id']}")
        chunks.append({"chunk_id": chunk["chunk_id"], "index": chunk["index"],
                       "metadata": chunk["metadata"], "result": cached["result"]})
    if len(chunks) != contract["expected_chunk_count"]:
        raise ValueError("Cannot aggregate an incomplete contract")
    # Lossless ordered aggregation; do not guess domain-specific merge/deduplication rules.
    return {"contract_id": contract["contract_id"], "version": contract["version"],
            "chunk_count": len(chunks), "chunks": chunks}
