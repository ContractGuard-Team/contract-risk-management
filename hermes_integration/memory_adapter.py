from agents.shared.memory import RequestMemory


def memory_context(memory: RequestMemory) -> dict:
    """Explicit snapshot for AgentRequest.context; no shared or persistent conversation."""
    return {"memory": memory.snapshot()}


def create_contract_memory(policy, summarizer, snapshot=None, counter=None):
    """Create an isolated Hermes ContextEngine from a checkpoint/cache snapshot."""
    from hermes_integration.context_engine import ContractContextEngine
    kwargs = {"counter": counter} if counter is not None else {}
    return ContractContextEngine(policy, summarizer, snapshot, **kwargs)
