def chunk_scope(contract, index):
    """Identity binding each Agent 1 result/memory snapshot to one contract chunk."""
    return {"contract_id": contract["contract_id"], "version": contract["version"],
            "chunk_id": contract["chunks"][index]["chunk_id"], "chunk_index": index}


def aggregate_contract(contract, cache, key):
    chunks = []
    for index, chunk in enumerate(contract["chunks"]):
        cached = cache.get(key, "classification", chunk["chunk_id"])
        if cached is None:
            raise ValueError(f"Cannot aggregate: missing chunk {chunk['chunk_id']}")
        if cached.get("scope") != chunk_scope(contract, index):
            raise ValueError(f"Cannot aggregate: chunk {chunk['chunk_id']} belongs to another contract")
        chunks.append({"chunk_id": chunk["chunk_id"], "index": chunk["index"],
                       "metadata": chunk["metadata"], "result": cached["result"]})
    if len(chunks) != contract["expected_chunk_count"]:
        raise ValueError("Cannot aggregate an incomplete contract")
    # Lossless ordered aggregation; do not guess domain-specific merge/deduplication rules.
    return {"contract_id": contract["contract_id"], "version": contract["version"],
            "chunk_count": len(chunks), "chunks": chunks}
