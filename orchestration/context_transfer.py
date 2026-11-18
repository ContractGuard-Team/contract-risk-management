import json

from common.schemas.agent_messages import AgentRequest
from agents.hermes.context_budget import ContextBudgetExceeded


def source_ids(source_name, source) -> list:
    """Ids the receiving agent must refer to: every clause_id of Agent 1, every finding_id of Agent 2.
    Kept as data next to a summary, so the agent's cross-checks still work when the source itself is compacted."""
    from agents.shared.output import parse_json
    if source_name == "classification":
        return sorted({clause.get("clause_id") for chunk in source.get("chunks", [])
                       for clause in (parse_json((chunk.get("result") or {}).get("text")) or {}).get("clauses", [])} - {None})
    return [finding.get("finding_id") for finding in (parse_json(source.get("text")) or {}).get("findings", [])]


def handoff_request(agent, contract, source_name, source, policy, summarizer, counter, cache_key, *, job_id=None):
    from agents.shared.context import agent_context
    context = agent_context(contract["context"])
    if "workflow" in context:
        raise ValueError("'workflow' is reserved for orchestration context")
    context["workflow"] = {"job_id": job_id, "contract_id": contract["contract_id"], "contract_version": contract["contract_version"],
                           "cache_key": cache_key, source_name: source}
    request = AgentRequest(f"contract:{contract['contract_id']} contract_version:{contract['contract_version']}", context)
    if counter(agent.build_messages(request)) <= policy.max_input_tokens:
        return request
    summary = summarizer.summarize(json.dumps(source, ensure_ascii=False),
                                   policy.summary_max_tokens, policy.max_input_tokens)
    context["workflow"][source_name] = {
        "summary": summary, "full_result_cache_key": cache_key, "ids": source_ids(source_name, source),
        "source_stage": source_name, "compacted": True,
        "chunk_count": contract["expected_chunk_count"],
    }
    if counter(agent.build_messages(request)) > policy.max_input_tokens:
        raise ContextBudgetExceeded("Handoff still exceeds receiving agent's input budget")
    return request
