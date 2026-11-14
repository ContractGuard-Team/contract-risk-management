"""Short-term memory: the context of ONE contract run, deleted when that run is finished.

- Each agent call gets an isolated Hermes ContextEngine (agents/hermes/context_engine.py) restored from the job's
  cache snapshot; it carries earlier chunks/turns of the same contract (summarised with the agent's own
  agents/<agent>/memory/short_term_summary.md when over budget) and never another contract's or agent's context.
- Every agent has its own snapshot (cache rows per agent stage); an agent is only ever given its own.
- Deletion is done by orchestration once results are safely stored: Agent 1 snapshots after the aggregate
  (ContractCache.release_memory), Agent 2/3 snapshots after the final result (checkpoint.release_run), and the
  LangGraph thread state after every run (checkpoint.forget). Results stay; only working memory goes.
"""
from agents.shared.memory import RequestMemory


def memory_context(memory: RequestMemory) -> dict:
    """Explicit snapshot for AgentRequest.context; no shared or persistent conversation."""
    return {"memory": memory.snapshot()}


def create_contract_memory(policy, summarizer, snapshot=None, counter=None):
    """Create an isolated Hermes ContextEngine from a checkpoint/cache snapshot."""
    from agents.hermes.context_engine import ContractContextEngine
    kwargs = {"counter": counter} if counter is not None else {}
    return ContractContextEngine(policy, summarizer, snapshot, **kwargs)
