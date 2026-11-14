"""Resume points and cleanup derived from the durable ContractCache (single source of truth).

The LangGraph checkpointer is per-run scratch state only; durable progress is the SQLite cache.
"""


def resume_chunk(cache, key, contract):
    """First chunk without a saved Agent 1 result; == chunk count when Agent 1 is finished."""
    for index, chunk in enumerate(contract["chunks"]):
        if cache.get(key, "classification", chunk["chunk_id"]) is None:
            return index
    return contract["expected_chunk_count"]


def release_run(cache, key, checkpointer=None, stages=("agent_2_risk_scoring", "agent_3_recommendation")):
    """After the final result is stored: drop Agent 2/3 working memory and in-RAM graph state.

    Results, Agent 1 aggregate and handoff inputs stay in the cache. Safe to call repeatedly.
    """
    for stage in stages:
        cache.release_memory(key, stage)
    forget(checkpointer, key)


def forget(checkpointer, key):
    # InMemorySaver otherwise keeps every contract's full state for the life of the service.
    if checkpointer is not None:
        checkpointer.delete_thread(key)
