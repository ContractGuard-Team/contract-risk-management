from dataclasses import dataclass

from common.schemas.agent_messages import AgentRequest


@dataclass(frozen=True)
class ClassificationInput(AgentRequest):
    """Shared transport fields only. TODO: define Classification business fields/validation."""
