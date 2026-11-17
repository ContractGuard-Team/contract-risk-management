from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from agents.shared.business_context import AgentContext
from common.schemas.agent_messages import AgentRequest


class ChunkWorkflow(BaseModel):
    model_config = ConfigDict(extra="allow")
    contract_id: str
    contract_version: str
    chunk_id: str
    chunk_index: int = Field(ge=0)
    chunk_count: int = Field(ge=1)
    source_metadata: dict[str, Any] = Field(default_factory=dict, description="pages, heading_path, source spans")


class ChunkContext(AgentContext):
    """Agent 1: one chunk of the contract (content) + where it sits (workflow)."""
    workflow: Optional[ChunkWorkflow] = None


@dataclass(frozen=True)
class ClassificationInput(AgentRequest):
    """Transport request; `context` must match ChunkContext (checked in context_builder.build_context)."""
