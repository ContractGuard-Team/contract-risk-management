from dataclasses import dataclass
from typing import Literal, Optional, Union

from pydantic import Field, model_validator

from common.schemas.agent_messages import AgentResult
from common.schemas.business.shared import (CheckCode, Check, Evidence, Id, NonEmpty, Reference, Severity, Strict,
                                            check_codes_once)

RuleId = Literal["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R09", "R10", "R11", "R12", "R13", "R14",
                 "R15", "R16", "R17", "R18", "R19", "R20"]
SectionRule = Literal["M01", "M02", "M03"]


class Verdict(Strict):
    rule_id: RuleId
    verdict: Literal["confirmed", "needs_review", "rejected"]
    evidence_quote: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=300)


class AdditionalHit(Strict):
    rule_id: RuleId
    evidence_quote: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=300)


class ClauseVerdicts(Strict):
    """Mode verify_clause for one clause; the code scores the contract (4.5b) from these."""
    clause_id: Id
    verdicts: list[Verdict]
    additional: list[AdditionalHit] = Field(max_length=3)


class RiskFinding(Strict):
    """One identified risk -> result findings[] (the code adds primary_domain/category, Agent 3 the recommendation)."""
    finding_id: Id
    finding_kind: Literal["problematic_clause", "missing_clause"]
    title: str = Field(min_length=1, max_length=300)
    rule_id: Optional[Union[RuleId, SectionRule]]
    check_code: Optional[CheckCode]
    severity: Severity
    clause_id: Optional[Id]
    evidence: list[Evidence]
    missing_description: Optional[str]
    why: NonEmpty
    impact: NonEmpty
    references: list[Reference]

    @model_validator(mode="after")
    def _consistent(self):
        if self.rule_id is None and self.check_code is None:
            raise ValueError("a finding needs rule_id or check_code")
        if self.finding_kind == "problematic_clause" and (self.clause_id is None or not self.evidence):
            raise ValueError("problematic_clause needs clause_id and at least one evidence quote")
        if self.finding_kind == "missing_clause" and (self.clause_id is not None or not self.missing_description):
            raise ValueError("missing_clause needs clause_id null and missing_description")
        return self


class RiskScoringResult(Strict):
    """Agent 2 business output for the whole contract."""
    clause_verdicts: list[ClauseVerdicts]
    findings: list[RiskFinding]
    checks: list[Check] = Field(min_length=20, max_length=20, description="CY-01..12, PR-01..05, TP-01..03, each once")
    evidence_gaps: list[NonEmpty] = Field(description="What still lacks a source; non-empty -> search again and re-evaluate")

    @model_validator(mode="after")
    def _checks_once(self):
        problems = check_codes_once(self.checks)
        if problems:
            raise ValueError(problems[0])
        return self


@dataclass(frozen=True)
class RiskScoringOutput(AgentResult):
    """Transport result; `text` holds one RiskScoringResult as JSON (validated by BaseAgent)."""
