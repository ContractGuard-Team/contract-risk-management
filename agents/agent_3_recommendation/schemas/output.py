from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import Field

from common.schemas.agent_messages import AgentResult
from common.schemas.business.shared import Evidence, Id, NonEmpty, Reference, Strict


class Recommendation(Strict):
    """Exactly one per Agent 2 finding -> result findings[].recommendation (+ references)."""
    finding_id: Id
    recommendation: NonEmpty = Field(description="What to change and why, in words (output_language)")
    proposed_text: Optional[str] = Field(description="Suggested wording in the contract's language; kept in the internal result")
    priority: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(description="Verbatim quotes from the original document")
    references: list[Reference]


class RecommendationResult(Strict):
    """Agent 3 business output for the whole contract."""
    recommendations: list[Recommendation]
    overall_recommendation: Optional[str] = Field(max_length=4000)
    evidence_gaps: list[NonEmpty]


@dataclass(frozen=True)
class RecommendationOutput(AgentResult):
    """Transport result; `text` holds one RecommendationResult as JSON (validated by BaseAgent)."""
