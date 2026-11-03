from agents.shared.context import build_context as build_shared_context
from common.schemas.agent_messages import AgentRequest


def build_context(request: AgentRequest) -> dict:
    # TODO: Add RiskScoring context/retrieval rules when specified.
    # For now, preserve only the explicit input; no external API call or hidden retrieval.
    return build_shared_context(request)
