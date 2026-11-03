from agents.shared.memory import RequestMemory
from agents.shared.tools import ToolHandler, invoke_tool


class EvidenceManager(RequestMemory):
    """Collect evidence by source key within a job; stored entries are not verified."""

    def validate(self, *, validator: ToolHandler | None = None) -> dict:
        # TODO: Define source, effective-date and evidence validation requirements.
        return invoke_tool("agent_2_risk_scoring.validate_evidence", self.snapshot(), validator)
