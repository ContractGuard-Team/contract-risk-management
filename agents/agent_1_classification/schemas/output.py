from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import Field

from common.schemas.agent_messages import AgentResult
from common.schemas.business.shared import (ClauseType, ContractMetadata, Fact, FactKey, Id, InjectionWarning,
                                            Language, NonEmpty, Strict)

# ContractGuard 4.2-4.5 categories: Agent 2 matches risk_policy.json rules on them.
Category = Literal["preamble_and_scope", "term_and_termination", "compensation_and_payment", "rights_and_obligations",
                   "confidentiality_and_ip", "breach_and_liabilities", "tax_and_compliance", "force_majeure",
                   "dispute_and_governing_law", "general_and_boilerplate", "other"]


class ClauseFact(Strict):
    attribute_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    value_text: NonEmpty
    evidence_quote: str = Field(min_length=1, max_length=500)
    uncertain: bool


class ChunkClause(Strict):
    """One clause (or the part of it) found in this chunk -> result clauses[]."""
    clause_id: Id = Field(description="<contract_id>-C<4 digits>, increasing over the whole contract")
    continuation: bool = Field(description="Continues the clause with this id cut at the end of the previous chunk")
    complete: bool = Field(description="false when the clause is cut at the end of this chunk")
    number: str = Field(max_length=50, description="Numbering as printed; '' when none")
    heading: Optional[str] = Field(max_length=500)
    text: NonEmpty = Field(description="Verbatim text in this chunk (only the new part for a continuation)")
    category: Category
    clause_type: ClauseType
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    facts: list[ClauseFact] = Field(max_length=40)


class Correction(Strict):
    """Public fix of a clause returned for an earlier chunk."""
    clause_id: Id
    category: Category
    clause_type: ClauseType
    reason: str = Field(min_length=1, max_length=500)


class ClassificationResult(Strict):
    """Agent 1 business output: ONE object per chunk. Merged by orchestration/extraction.py into the result's
    clauses, metadata, facts, injection_warnings and document.language."""
    chunk_id: NonEmpty = Field(description="Copied from context.workflow.chunk_id")
    language: Language
    clauses: list[ChunkClause]
    metadata: ContractMetadata = Field(description="What this chunk states; null/[] for the rest")
    fact_values: dict[FactKey, Fact] = Field(description="Keys from the fact_key catalog; {} until it is provided")
    injection_warnings: list[InjectionWarning]
    corrections: list[Correction]


@dataclass(frozen=True)
class ClassificationOutput(AgentResult):
    """Transport result; `text` holds one ClassificationResult as JSON (validated by BaseAgent)."""
