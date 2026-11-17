from agents.base import BaseAgent
from agents.shared.output import duplicates, parse_json
from agents.shared.tools import external_tool
from agents.agent_3_recommendation.context.context_builder import build_context
from agents.agent_3_recommendation.schemas.input import RecommendationInput
from agents.agent_3_recommendation.schemas.output import RecommendationOutput, RecommendationResult
from agents.agent_3_recommendation.tools.qdrant_search import qdrant_search
from agents.agent_3_recommendation.tools.legal_reference import legal_reference
from agents.agent_3_recommendation.tools.clause_rewriter import clause_rewriter


class RecommendationAgent(BaseAgent):
    agent_id = "agent_3_recommendation"
    output_type = RecommendationOutput
    output_model = RecommendationResult
    tool_functions = {"qdrant_search": qdrant_search, "legal_reference": legal_reference, "clause_rewriter": clause_rewriter,
                      **{name: external_tool(name) for name in ("web_search", "postgres_query", "s3_get_object", "s3_list_objects", "qdrant_collection_info",
                                                       "supabase_select", "supabase_list_tables", "partner_document")}}

    def build_context(self, request):
        return build_context(request)

    def check_output(self, data, request):
        source = (request.context.get("workflow") or {}).get("risk_scoring") or {}
        if source.get("compacted"):  # summary + the full id list
            expected = set(source.get("ids") or [])
        else:
            risk = parse_json(source.get("text")) if isinstance(source, dict) else None
            if risk is None:
                return []
            expected = {finding.get("finding_id") for finding in risk.get("findings", [])}
        got = [item["finding_id"] for item in data["recommendations"]]
        problems = [f"thiếu đề xuất cho finding: {sorted(expected - set(got))}"] if expected - set(got) else []
        if set(got) - expected:
            problems.append(f"finding_id không có trong kết quả Agent 2: {sorted(set(got) - expected)}")
        if duplicates(got):
            problems.append(f"mỗi finding chỉ một đề xuất, trùng: {duplicates(got)}")
        return problems

    def recommend(self, request: RecommendationInput) -> RecommendationOutput:
        return self.run(request)
