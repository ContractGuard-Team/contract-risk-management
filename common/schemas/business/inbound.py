"""What the worker RECEIVES from ContractGuard: the SQS message and results/<job_id>/request.json.

Every field ContractGuard sends is modelled, and unknown fields are KEPT (extra="allow"), so nothing the partner sends
is dropped: the whole request travels to every agent as context["request"].
"""
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from common.schemas.business.shared import ContractType, NonEmpty

FileType = Literal["pdf", "docx", "jpg", "png"]
JobId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]


class Received(BaseModel):
    model_config = ConfigDict(extra="allow")


class Ping(Received):
    """Development health check: the worker answers `pong` and deletes the message."""
    message_version: Literal["1"]
    event_type: Literal["ping"]
    job_id: JobId


class AnalysisRequested(Received):
    """A job is ready: details are in request_key (AnalysisRequest)."""
    message_version: Literal["1"]
    event_type: Literal["analysis_requested"]
    job_id: JobId
    request_key: str = Field(pattern=r"^results/[^/]+/request\.json$")

    @model_validator(mode="after")
    def _request_of_this_job(self):
        if self.request_key != f"results/{self.job_id}/request.json":
            raise ValueError("request_key must be results/<job_id>/request.json of this job")
        return self


InboundMessage = TypeAdapter(Annotated[Union[Ping, AnalysisRequested], Field(discriminator="event_type")])


class ContractContext(Received):
    """What the user confirmed about the contract; every agent reads it."""
    own_party: Literal["buyer", "seller"]
    contract_type: ContractType
    has_personal_data: bool
    counterparty_name: Optional[str] = None
    counterparty_offering: Optional[str] = None
    service_criticality: Optional[Literal["low", "medium", "high"]] = None


class Limits(Received):
    max_pages: int = Field(gt=0, description="Compared with the page count input-processing reads; above -> ERR-UPL-004")
    max_bytes: int = Field(gt=0, description="Download limit = min(this, 20 MiB)")


class AnalysisRequest(Received):
    """results/<job_id>/request.json, written by the backend. Trusted: ids are copied into the result, never invented,
    and every S3 key the worker uses comes from here."""
    schema_version: Literal["1.0.0"]
    job_id: JobId
    tenant_id: NonEmpty
    contract_id: NonEmpty
    contract_version: str = Field(pattern=r"^[1-9][0-9]*$")
    source_file_key: str = Field(pattern=r"^(quarantine|clean)/", max_length=1024)
    clean_file_key: str = Field(pattern=r"^clean/", max_length=1024)
    result_key: str = Field(pattern=r"^results/", max_length=1024)
    declared_file_type: FileType
    output_language: Literal["vi", "en"]
    context: ContractContext
    limits: Limits

    @model_validator(mode="after")
    def _keys(self):
        for name in ("source_file_key", "clean_file_key", "result_key"):
            if ".." in getattr(self, name).split("/"):
                raise ValueError(f"{name} must not contain '..'")
        # The partner's IAM only lets this worker write results/*/extraction.json.
        if self.result_key != f"results/{self.job_id}/extraction.json":
            raise ValueError("result_key must be results/<job_id>/extraction.json")
        return self


# --- chatbot (ContractGuard docs/04 C6, docs/07 §11): chat-requests queue + results/<job_id>/request.json ----------
class ChatRequested(Received):
    """A user asked about an analysed contract: the question is in request_key (ChatRequest)."""
    message_version: Literal["1"]
    event_type: Literal["chat_requested"]
    job_id: JobId
    request_key: str = Field(pattern=r"^results/[0-9a-f-]{36}/request\.json$")

    @model_validator(mode="after")
    def _request_of_this_job(self):
        if self.request_key != f"results/{self.job_id}/request.json":
            raise ValueError("request_key must be results/<job_id>/request.json of this question")
        return self


ChatInboundMessage = TypeAdapter(ChatRequested)


class ChatTurn(Received):
    role: Literal["user", "assistant"]
    content: str


class ChatClause(Received):
    clause_id: NonEmpty
    number: str
    heading: Optional[str] = None
    text: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)


class ChatFinding(Received):
    finding_id: NonEmpty
    title: str
    severity: Literal["low", "medium", "high", "critical"]
    clause_id: Optional[str] = None
    why: str
    impact: str


class ChatRequest(Received):
    """contracts/chat-request.v1.schema.json: one question about one analysis run (BR-CHAT-001).
    question and history are the user's own text: data to answer, never instructions to follow."""
    schema_version: Literal["1.0.0"]
    job_id: JobId
    analysis_job_id: NonEmpty
    contract_id: NonEmpty
    contract_version_id: NonEmpty
    output_language: Literal["vi", "en"]
    question: str = Field(min_length=1, max_length=2000)
    history: list[ChatTurn] = Field(max_length=10)
    clauses: list[ChatClause]
    findings: list[ChatFinding]
    overall_recommendation: Optional[str] = None
