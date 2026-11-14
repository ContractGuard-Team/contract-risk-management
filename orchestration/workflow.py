from dataclasses import asdict
from langgraph.graph import END, START, StateGraph

from common.schemas.contract import ContractChunk, ContractInput
from agents.agent_1_classification.context.context_builder import build_chunk_request
from agents.memory.short_term import create_contract_memory
from orchestration.context_transfer import handoff_request
from orchestration.result_aggregator import aggregate_contract, chunk_scope
from orchestration.retry_policy import AGENT_STAGE_RETRY
from orchestration.state import ContractState

AGENT_IDS = ("agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation")


class WorkflowSteps:
    def __init__(self, agents, cache, policies, summarizers, observer, counter):
        self.agents, self.cache, self.policies = agents, cache, policies
        self.summarizers, self.observer, self.counter = summarizers, observer, counter

    def classify_chunk(self, state):
        contract, key, index = state["contract"], state["cache_key"], state["next_chunk"]
        chunk = contract["chunks"][index]
        agent_id = AGENT_IDS[0]
        cached = self.cache.get(key, "classification", chunk["chunk_id"])
        scope = chunk_scope(contract, index)
        if cached is not None and cached.get("scope") != scope:
            raise ValueError("Cached Agent 1 result belongs to another contract chunk")
        with self.observer.span("agent_1_classification.chunk", kind="agent", metadata={"chunk_id": chunk["chunk_id"],
                                "chunk_index": index, "cache_hit": cached is not None}) as observation:
            observation.summary(input={"chunk_index": index, "chunk_count": contract["expected_chunk_count"],
                                       "text_characters": len(chunk["text"])})
            cache_hit = cached is not None
            if cached is None:
                snapshot = None  # first chunk of a contract always starts with empty memory
                if index:
                    # Continue only from this contract's previous chunk; never from another contract's context.
                    prior = self.cache.get(key, "classification", contract["chunks"][index - 1]["chunk_id"])
                    if prior is None or prior.get("scope") != chunk_scope(contract, index - 1) or "memory" not in prior:
                        raise ValueError("Agent 1 memory of the previous chunk in this contract is missing")
                    snapshot = prior["memory"]
                memory = create_contract_memory(self.policies[agent_id], self.summarizers[agent_id],
                                                snapshot, self.counter)
                manifest = ContractInput(contract["contract_id"], contract["version"],
                                         [ContractChunk(**item) for item in contract["chunks"]],
                                         contract["expected_chunk_count"], contract["context"])
                request = build_chunk_request(manifest, index)
                result = self.agents[agent_id].run(request, memory=memory,
                           observer=self.observer, trace_metadata={"chunk_id": chunk["chunk_id"], "chunk_index": index})
                # Result and memory are written in one row so resume never sees one without the other.
                cached = {"scope": scope, "result": asdict(result), "memory": memory.snapshot()}
                self.cache.put(key, "classification", cached, chunk["chunk_id"])
            observation.summary(output={"status": "completed", "cache_hit": cache_hit,
                                        "text_characters": len(cached["result"]["text"])})
        return {"next_chunk": index + 1}

    def aggregate(self, state):
        with self.observer.span("aggregate_classification") as observation:
            observation.summary(input={"expected_chunk_count": state["contract"]["expected_chunk_count"]})
            key = state["cache_key"]
            aggregate = aggregate_contract(state["contract"], self.cache, key)
            self.cache.put(key, "aggregate", aggregate)
            if self.cache.get(key, "aggregate") != aggregate:
                raise ValueError("Agent 1 aggregate could not be read back; keep memory for retry")
            # Contract confirmed complete: Agent 1 working memory is no longer needed, outputs are kept.
            self.cache.release_memory(key, "classification")
            observation.summary(output={"chunk_count": aggregate["chunk_count"], "status": "cached",
                                        "agent_1_memory": "released"})
        return {"aggregate": aggregate, "results": {AGENT_IDS[0]: aggregate}}

    def _downstream(self, state, agent_id, source_name, source):
        key, contract = state["cache_key"], state["contract"]
        cached = self.cache.get(key, agent_id)
        with self.observer.span(agent_id + ".stage", kind="agent",
                                metadata={"cache_hit": cached is not None}) as observation:
            observation.summary(input={"source_stage": source_name,
                                       "contract_chunk_count": contract["expected_chunk_count"]})
            cache_hit = cached is not None
            if cached is None:
                memory = create_contract_memory(self.policies[agent_id], self.summarizers[agent_id],
                                                counter=self.counter)
                request = handoff_request(self.agents[agent_id], contract, source_name, source,
                                          self.policies[agent_id], self.summarizers[agent_id], self.counter, key)
                result = self.agents[agent_id].run(request, memory=memory, observer=self.observer)
                cached = {"result": asdict(result), "memory": memory.snapshot(),
                          "input_context": request.context}
                self.cache.put(key, agent_id, cached)
            observation.summary(output={"status": "completed", "cache_hit": cache_hit,
                                        "text_characters": len(cached["result"]["text"])})
        return {"results": {**state["results"], agent_id: cached["result"]}}

    def evaluate(self, state):
        return self._downstream(state, AGENT_IDS[1], "classification", state["aggregate"])

    def recommend(self, state):
        return self._downstream(state, AGENT_IDS[2], "risk_scoring", state["results"][AGENT_IDS[1]])


def build_workflow(steps, checkpointer):
    graph = StateGraph(ContractState)
    # Agent nodes re-run only on transient model/service failures (see retry_policy.py).
    for name, node, retry in (("classify_chunk", steps.classify_chunk, AGENT_STAGE_RETRY),
                              ("aggregate", steps.aggregate, None),
                              ("evaluate", steps.evaluate, AGENT_STAGE_RETRY),
                              ("recommend", steps.recommend, AGENT_STAGE_RETRY)):
        graph.add_node(name, node, retry_policy=retry)
    # Resume: a run whose Agent 1 chunks are all cached starts at aggregate -> Agent 2 directly.
    graph.add_conditional_edges(START, lambda state:
        "classify_chunk" if state["next_chunk"] < state["contract"]["expected_chunk_count"] else "aggregate",
        {"classify_chunk": "classify_chunk", "aggregate": "aggregate"})
    graph.add_conditional_edges("classify_chunk", lambda state:
        "classify_chunk" if state["next_chunk"] < state["contract"]["expected_chunk_count"] else "aggregate",
        {"classify_chunk": "classify_chunk", "aggregate": "aggregate"})
    graph.add_edge("aggregate", "evaluate")
    graph.add_edge("evaluate", "recommend")
    graph.add_edge("recommend", END)
    return graph.compile(checkpointer=checkpointer)
