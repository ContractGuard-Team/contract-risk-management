from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from agents.shared.business_context import AgentContext
from common.schemas.agent_messages import AgentRequest


class RiskScoringWorkflow(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str = Field(min_length=1, description="Job the aggregate/result belongs to (set by LangGraph)")
    contract_id: str
    contract_version: str
    classification: dict[str, Any] = Field(description="Agent 1 aggregate: chunks[].result.text = ClassificationResult, "
                                                       "or a summary with compacted=true")


class RiskScoringContext(AgentContext):
    """Agent 2: every Agent 1 clause of the contract."""
    workflow: Optional[RiskScoringWorkflow] = None


@dataclass(frozen=True)
class RiskScoringInput(AgentRequest):
    """Transport request; `context` must match RiskScoringContext (checked in context_builder.build_context)."""
