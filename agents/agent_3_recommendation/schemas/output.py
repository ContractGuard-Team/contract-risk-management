from dataclasses import dataclass

from common.schemas.agent_messages import AgentResult


@dataclass(frozen=True)
class RecommendationOutput(AgentResult):
    """Shared transport fields only. TODO: define Recommendation business fields/validation."""
