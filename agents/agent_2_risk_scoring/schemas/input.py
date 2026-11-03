from dataclasses import dataclass

from common.schemas.agent_messages import AgentRequest


@dataclass(frozen=True)
class RiskScoringInput(AgentRequest):
    """Shared transport fields only. TODO: define RiskScoring business fields/validation."""
