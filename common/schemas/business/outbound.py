"""What the worker RETURNS to ContractGuard: messages on the results queue and the result file at result_key.

Every field ContractGuard accepts is modelled (optional ones are left out when we have no value); unknown keys are
forbidden because the backend rejects them. The result is built in the LangGraph service (orchestration/extraction.py)
from the agents' business outputs and validated here before the worker writes it.
"""
from typing import Annotated, Literal, Optional, Union

from pydantic import Field, TypeAdapter, model_validator

from common.schemas.business.shared import (CheckCode, Check, ClauseType, ContractMetadata, Domain, Evidence, Fact,
                                            Id, InjectionWarning, NonEmpty, Reference, RiskLevel, Strict,
                                            check_codes_once)

ProgressStep = Literal["reading_file", "splitting_clauses", "evaluating", "scoring"]


# --- results queue ---------------------------------------------------------------------------------------
class Progress(Strict):
    message_version: Literal["1"] = "1"
    event_type: Literal["progress"] = "progress"
    job_id: NonEmpty
    step: ProgressStep


class Completed(Strict):
    message_version: Literal["1"] = "1"
    event_type: Literal["completed"] = "completed"
    job_id: NonEmpty
    result_key: str = Field(pattern=r"^results/[^/]+/[A-Za-z0-9_.-]+\.json$")


class Failed(Strict):
    message_version: Literal["1"] = "1"
    event_type: Literal["failed"] = "failed"
    job_id: NonEmpty
    failure_reason: str = Field(min_length=1, max_length=2000)
    error_code: Optional[str] = Field(default=None, pattern=r"^ERR-[A-Z]+-[0-9]{3}$")


class Pong(Strict):
    message_version: Literal["1"] = "1"
    event_type: Literal["pong"] = "pong"
    job_id: NonEmpty


class ChatAnswered(Strict):
    """The whole answer to a chat_requested (not streamed). cited_clause_ids come from the request's clauses only."""
    message_version: Literal["1"] = "1"
    event_type: Literal["chat_answered"] = "chat_answered"
    job_id: NonEmpty
    answer_text: str = Field(min_length=1, max_length=8000)
    cited_clause_ids: list[NonEmpty]


OutboundMessage = TypeAdapter(Annotated[Union[Progress, Completed, Failed, Pong, ChatAnswered],
                                        Field(discriminator="event_type")])


# --- result file (results/<job_id>/extraction.json) ------------------------------------------------------
class Region(Strict):
    """Rectangle as fractions of the page, origin top-left."""
    page: int = Field(ge=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)


class Location(Strict):
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    regions: list[Region] = Field(default_factory=list)


class Document(Strict):
    file_type: Literal["pdf", "docx", "jpg", "png"] = Field(description="Real type read from the content")
    page_count: int = Field(ge=1, le=100)
    clean_file_key: Optional[str] = Field(default=None, pattern=r"^clean/")
    language: Literal["vi", "en", "mixed"]


class Clause(Strict):
    clause_id: Id
    order: int = Field(ge=1)
    number: str = Field(max_length=50, description="Numbering as printed; '' when none")
    heading: Optional[str] = Field(default=None, max_length=500)
    text: NonEmpty = Field(description="Verbatim clause text")
    clause_type: ClauseType
    location: Location


class Finding(Strict):
    finding_id: Id
    finding_kind: Literal["problematic_clause", "missing_clause"]
    title: str = Field(min_length=1, max_length=300)
    primary_domain: Domain
    secondary_domains: Optional[list[Domain]] = None
    category: Optional[str] = Field(default=None, max_length=200)
    severity: RiskLevel
    clause_id: Optional[str] = None
    evidence: Optional[list[Evidence]] = None
    missing_description: Optional[str] = None
    why: NonEmpty
    impact: NonEmpty
    recommendation: NonEmpty = Field(description="Direction in words, not replacement wording")
    references: Optional[list[Reference]] = None
    check_code: Optional[CheckCode] = None

    @model_validator(mode="after")
    def _kind(self):
        if self.finding_kind == "problematic_clause" and (not self.clause_id or not self.evidence):
            raise ValueError("problematic_clause needs clause_id and at least one evidence quote")
        if self.finding_kind == "missing_clause" and not self.missing_description:
            raise ValueError("missing_clause needs missing_description")
        if self.secondary_domains is not None and len(set(self.secondary_domains)) != len(self.secondary_domains):
            raise ValueError("secondary_domains must be unique")
        return self


class DomainScore(Strict):
    domain: Domain
    applicable: bool
    score: Optional[int] = Field(default=None, ge=0, le=100)
    risk_level: Optional[RiskLevel] = None
    na_reason: Optional[str] = Field(default=None, max_length=1000)
    re_enabled: Optional[bool] = None


class Scoring(Strict):
    rule_version: NonEmpty
    overall_score: int = Field(ge=0, le=100)
    risk_level: RiskLevel
    domains: list[DomainScore] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def _each_domain_once(self):
        if len({d.domain for d in self.domains}) != 7:
            raise ValueError("domains must hold the 7 domains once")
        return self


class Trace(Strict):
    model_version: NonEmpty
    prompt_version: NonEmpty
    pipeline_version: Optional[str] = None


class AnalysisResult(Strict):
    """results/<job_id>/extraction.json. The backend checks clause ids, quotes and pages against `clauses`
    and rejects the whole file on one mismatch, so orchestration/extraction.py verifies them first."""
    schema_version: Literal["2.0.0"] = "2.0.0"
    job_id: NonEmpty
    contract_id: NonEmpty
    contract_version: NonEmpty
    document_complete: bool
    document: Document
    clauses: list[Clause] = Field(min_length=1)
    metadata: Optional[ContractMetadata] = None
    findings: list[Finding]
    checks: list[Check] = Field(min_length=20, max_length=20)
    facts: dict[str, Fact]
    injection_warnings: Optional[list[InjectionWarning]] = None
    overall_recommendation: Optional[str] = Field(default=None, max_length=4000)
    scoring: Optional[Scoring] = None
    trace: Trace

    @model_validator(mode="after")
    def _checks_once(self):
        problems = check_codes_once(self.checks)
        if problems:
            raise ValueError(problems[0])
        return self

    def to_json(self) -> dict:
        """The file content: optional fields without a value are left out."""
        return self.model_dump(mode="json", exclude_none=True)
