from agents.base import BaseAgent
from agents.shared.output import duplicates, parse_json
from agents.shared.tools import external_tool
from agents.agent_2_risk_scoring.context.context_builder import build_context
from agents.agent_2_risk_scoring.schemas.input import RiskScoringInput
from agents.agent_2_risk_scoring.schemas.output import RiskScoringOutput, RiskScoringResult
from agents.agent_2_risk_scoring.tools.qdrant_search import qdrant_search
from agents.agent_2_risk_scoring.tools.legal_search import legal_search
from agents.agent_2_risk_scoring.tools.market_search import market_search
from agents.agent_2_risk_scoring.tools.risk_calculator import risk_calculator


def clause_ids(request) -> set:
    """Clause ids Agent 1 returned for this contract (context.workflow.classification, or its `ids` when compacted)."""
    aggregate = (request.context.get("workflow") or {}).get("classification") or {}
    if aggregate.get("compacted"):
        return set(aggregate.get("ids") or [])
    ids = set()
    for chunk in aggregate.get("chunks") or []:
        output = parse_json((chunk.get("result") or {}).get("text"))
        ids |= {clause.get("clause_id") for clause in (output or {}).get("clauses", [])}
    return ids - {None}


class RiskScoringAgent(BaseAgent):
    agent_id = "agent_2_risk_scoring"
    output_type = RiskScoringOutput
    output_model = RiskScoringResult
    tool_functions = {"qdrant_search": qdrant_search, "legal_search": legal_search, "market_search": market_search, "risk_calculator": risk_calculator,
                      **{name: external_tool(name) for name in ("web_search", "postgres_query", "s3_get_object", "s3_list_objects", "qdrant_collection_info",
                                                       "supabase_select", "supabase_list_tables")}}

    def build_context(self, request):
        return build_context(request)

    def check_output(self, data, request):
        problems = []
        finding_ids = [finding["finding_id"] for finding in data["findings"]]
        if duplicates(finding_ids):
            problems.append(f"finding_id trùng: {duplicates(finding_ids)}")
        problems += [f"check {c['check_code']} trỏ tới finding không tồn tại {c['finding_id']}"
                     for c in data["checks"] if c["finding_id"] and c["finding_id"] not in finding_ids]
        known = clause_ids(request)
        if known:
            used = {v["clause_id"] for v in data["clause_verdicts"]}
            for finding in data["findings"]:
                used |= {finding["clause_id"]} - {None} | {e["clause_id"] for e in finding["evidence"]}
            for check in data["checks"]:
                used |= set(check["clause_ids"])
            if used - known:
                problems.append(f"clause_id không có trong kết quả Agent 1: {sorted(used - known)[:20]}")
        return problems

    def evaluate(self, request: RiskScoringInput) -> RiskScoringOutput:
        return self.run(request)
