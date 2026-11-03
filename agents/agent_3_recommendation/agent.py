from agents.base import BaseAgent
from agents.agent_3_recommendation.context.context_builder import build_context
from agents.agent_3_recommendation.schemas.input import RecommendationInput
from agents.agent_3_recommendation.schemas.output import RecommendationOutput
from agents.agent_3_recommendation.tools.qdrant_search import qdrant_search
from agents.agent_3_recommendation.tools.legal_reference import legal_reference
from agents.agent_3_recommendation.tools.clause_rewriter import clause_rewriter


class RecommendationAgent(BaseAgent):
    agent_id = "agent_3_recommendation"
    task_prompt = "prompts/recommend.md"
    output_type = RecommendationOutput
    tool_functions = {"qdrant_search": qdrant_search, "legal_reference": legal_reference, "clause_rewriter": clause_rewriter}

    def build_context(self, request):
        return build_context(request)

    def recommend(self, request: RecommendationInput) -> RecommendationOutput:
        return self.run(request)
