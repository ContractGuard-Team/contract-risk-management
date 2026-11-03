from agents.shared.memory import RequestMemory


class ChunkMemory(RequestMemory):
    """Store explicit chunk summaries by chunk_id for one job.

    TODO: Define what to retain; no automatic summarization or persistence.
    """
