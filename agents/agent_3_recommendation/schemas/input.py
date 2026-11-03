from dataclasses import dataclass

from common.schemas.agent_messages import AgentRequest


@dataclass(frozen=True)
class RecommendationInput(AgentRequest):
    """Shared transport fields only. TODO: define Recommendation business fields/validation."""
