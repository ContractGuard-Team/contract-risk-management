from copy import deepcopy
import json

from common.schemas.agent_messages import AgentRequest
from agents.hermes.context_budget import ContextBudgetExceeded


def handoff_request(agent, contract, source_name, source, policy, summarizer, counter, cache_key, *, job_id=None):
    context = deepcopy(contract["context"])
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
        "summary": summary, "full_result_cache_key": cache_key,
        "source_stage": source_name, "compacted": True,
        "chunk_count": contract["expected_chunk_count"],
    }
    if counter(agent.build_messages(request)) > policy.max_input_tokens:
        raise ContextBudgetExceeded("Handoff still exceeds receiving agent's input budget")
    return request
