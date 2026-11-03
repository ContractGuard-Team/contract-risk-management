from dataclasses import dataclass

from common.schemas.agent_messages import AgentResult


@dataclass(frozen=True)
class RiskScoringOutput(AgentResult):
    """Shared transport fields only. TODO: define RiskScoring business fields/validation."""
