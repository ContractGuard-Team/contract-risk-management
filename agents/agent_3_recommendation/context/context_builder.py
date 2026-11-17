from agents.shared.business_context import check_context
from agents.shared.context import build_context as build_shared_context
from agents.agent_3_recommendation.schemas.input import RecommendationContext
from common.schemas.agent_messages import AgentRequest


def build_context(request: AgentRequest) -> dict:
    check_context(RecommendationContext, request.context)  # business shape of the handoff (request + workflow)
    return build_shared_context(request)
