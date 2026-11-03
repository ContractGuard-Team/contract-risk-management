from dataclasses import dataclass

from common.schemas.agent_messages import AgentResult


@dataclass(frozen=True)
class ClassificationOutput(AgentResult):
    """Shared transport fields only. TODO: define Classification business fields/validation."""
