from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from agents.shared.business_context import AgentContext
from common.schemas.agent_messages import AgentRequest


class RecommendationWorkflow(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str = Field(min_length=1, description="Job the aggregate/result belongs to (set by LangGraph)")
    contract_id: str
    contract_version: str
    risk_scoring: dict[str, Any] = Field(description="Agent 2 result: text = RiskScoringResult, or a summary")


class RecommendationContext(AgentContext):
    """Agent 3: Agent 2's findings; the original document is read through MCP partner_document with
    request.source_file_key."""
    workflow: Optional[RecommendationWorkflow] = None


@dataclass(frozen=True)
class RecommendationInput(AgentRequest):
    """Transport request; `context` must match RecommendationContext (checked in context_builder.build_context)."""
