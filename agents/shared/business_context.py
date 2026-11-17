"""What every agent receives in AgentRequest.context: the whole ContractGuard request (when the job came from
ContractGuard) plus the orchestration `workflow` block of that agent. Unknown keys are kept."""
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from common.schemas.business.inbound import AnalysisRequest


class AgentContext(BaseModel):
    model_config = ConfigDict(extra="allow")
    request: Optional[AnalysisRequest] = Field(default=None, description="results/<job>/request.json, every field")
    workflow: Optional[dict[str, Any]] = None


def check_context(model, context: dict) -> None:
    """Raise ValueError when the handoff context does not have the business shape this agent expects."""
    model.model_validate(context)
