from agents.base import BaseAgent
from agents.agent_2_risk_scoring.context.context_builder import build_context
from agents.agent_2_risk_scoring.schemas.input import RiskScoringInput
from agents.agent_2_risk_scoring.schemas.output import RiskScoringOutput
from agents.agent_2_risk_scoring.tools.qdrant_search import qdrant_search
from agents.agent_2_risk_scoring.tools.legal_search import legal_search
from agents.agent_2_risk_scoring.tools.market_search import market_search
from agents.agent_2_risk_scoring.tools.risk_calculator import risk_calculator


class RiskScoringAgent(BaseAgent):
    agent_id = "agent_2_risk_scoring"
    task_prompt = "prompts/evaluate.md"
    output_type = RiskScoringOutput
    tool_functions = {"qdrant_search": qdrant_search, "legal_search": legal_search, "market_search": market_search, "risk_calculator": risk_calculator}

    def build_context(self, request):
        return build_context(request)

    def evaluate(self, request: RiskScoringInput) -> RiskScoringOutput:
        return self.run(request)
