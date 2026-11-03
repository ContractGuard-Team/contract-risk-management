from agents.shared.memory import RequestMemory


def memory_context(memory: RequestMemory) -> dict:
    """Explicit snapshot for AgentRequest.context; no shared or persistent conversation."""
    return {"memory": memory.snapshot()}
