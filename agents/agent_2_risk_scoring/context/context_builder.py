from agents.shared.business_context import check_context
from agents.shared.context import build_context as build_shared_context
from agents.agent_2_risk_scoring.schemas.input import RiskScoringContext
from common.schemas.agent_messages import AgentRequest


def build_context(request: AgentRequest) -> dict:
    check_context(RiskScoringContext, request.context)  # business shape of the handoff (request + workflow)
    return build_shared_context(request)
