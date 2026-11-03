from agents.shared.memory import RequestMemory


class RecommendationMemory(RequestMemory):
    """Store explicit recommendation context by clause key for one job.

    TODO: Define retention rules; no automatic retrieval or persistent memory.
    """
