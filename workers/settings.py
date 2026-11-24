"""Environment-only worker settings; AWS resources are provisioned separately."""
from dataclasses import dataclass, field
import os
from workers.connectors.http import validate_url


@dataclass(frozen=True)
class WorkerSettings:
    queue_url: str
    table_name: str
    output_bucket: str
    input_bucket: str | None = None
    region: str | None = None
    output_prefix: str = "results"
    input_processing_url: str = ""
    langgraph_url: str = ""
    service_token: str = field(default="", repr=False)
    input_processing_token: str = field(default="", repr=False)
    langfuse_base_url: str = ""
    langfuse_public_key: str = field(default="", repr=False)
    langfuse_secret_key: str = field(default="", repr=False)
    poll_seconds: int = 5
    http_timeout: int = 60
    visibility_seconds: int = 120
    heartbeat_seconds: int = 30
    lease_seconds: int = 180
    ack_timeout: int = 120
    shutdown_seconds: int = 45
    max_job_seconds: int = 39600
    events_table_name: str = ""
    results_queue_url: str = ""  # ContractGuard analysis-results queue: enables workers/partner.py
    chat_queue_url: str = ""  # ContractGuard chat-requests queue: enables workers/chat.py
    cancel_queue_url: str = ""  # optional queue of cancel_requested messages (workers/cancel.py)
    chat_url: str = ""  # agent-3, which answers chat questions (POST /v1/chat)

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.queue_url, self.table_name, self.output_bucket)):
            raise ValueError("WORKER_SQS_QUEUE_URL, WORKER_DYNAMODB_TABLE and WORKER_OUTPUT_BUCKET are required")
        if not 10 <= self.visibility_seconds <= 43200:
            raise ValueError("visibility_seconds must be 10..43200")
        if not 1 <= self.heartbeat_seconds < self.visibility_seconds / 2:
            raise ValueError("heartbeat_seconds must be less than half the visibility timeout")
        if self.lease_seconds < self.visibility_seconds + self.heartbeat_seconds:
            raise ValueError("lease_seconds must cover visibility plus one heartbeat interval")
        if min(self.ack_timeout, self.shutdown_seconds, self.max_job_seconds, self.poll_seconds, self.http_timeout) <= 0:
            raise ValueError("Worker timeouts must be positive")
        if self.max_job_seconds + self.visibility_seconds >= 43200:
            raise ValueError("max_job_seconds plus visibility must be below SQS's 12-hour limit")
        if self.events_table_name and self.events_table_name == self.table_name:
            raise ValueError("Jobs and events must use distinct DynamoDB tables")
        if self.results_queue_url and (not self.input_bucket or self.output_bucket != self.input_bucket):
            raise ValueError("ContractGuard mode: WORKER_INPUT_BUCKET and WORKER_OUTPUT_BUCKET must both be its bucket")
        if self.cancel_queue_url and not self.events_table_name:
            raise ValueError("WORKER_CANCEL_QUEUE_URL needs WORKER_DYNAMODB_EVENTS_TABLE")
        if self.chat_queue_url and (not self.results_queue_url or not self.chat_url):
            raise ValueError("WORKER_CHAT_QUEUE_URL needs WORKER_RESULTS_QUEUE_URL and WORKER_CHAT_URL")

    def validate_connections(self):
        validate_url(self.input_processing_url, "WORKER_INPUT_PROCESSING_URL")
        if self.chat_queue_url:
            validate_url(self.chat_url, "WORKER_CHAT_URL")
        if self.langgraph_url:
            validate_url(self.langgraph_url, "WORKER_LANGGRAPH_URL")
            if len(self.service_token) < 32:
                raise ValueError("INTERNAL_API_TOKEN must have at least 32 characters")
            return
        validate_url(self.langfuse_base_url, "WORKER_LANGFUSE_BASE_URL")
        if not self.langfuse_public_key or not self.langfuse_secret_key:
            raise ValueError("WORKER_LANGFUSE_PUBLIC_KEY and WORKER_LANGFUSE_SECRET_KEY are required")

    @classmethod
    def from_env(cls):
        values = {}
        names = {"queue_url": "WORKER_SQS_QUEUE_URL", "table_name": "WORKER_DYNAMODB_TABLE",
                 "events_table_name": "WORKER_DYNAMODB_EVENTS_TABLE",
                 "output_bucket": "WORKER_OUTPUT_BUCKET", "input_bucket": "WORKER_INPUT_BUCKET",
                 "region": "AWS_REGION", "output_prefix": "WORKER_OUTPUT_PREFIX",
                 "input_processing_url": "WORKER_INPUT_PROCESSING_URL",
                 "langgraph_url": "WORKER_LANGGRAPH_URL", "service_token": "INTERNAL_API_TOKEN",
                 "input_processing_token": "WORKER_INPUT_PROCESSING_TOKEN",
                 "langfuse_base_url": "WORKER_LANGFUSE_BASE_URL",
                 "langfuse_public_key": "WORKER_LANGFUSE_PUBLIC_KEY", "langfuse_secret_key": "WORKER_LANGFUSE_SECRET_KEY",
                 "results_queue_url": "WORKER_RESULTS_QUEUE_URL",
                 "cancel_queue_url": "WORKER_CANCEL_QUEUE_URL", "chat_queue_url": "WORKER_CHAT_QUEUE_URL", "chat_url": "WORKER_CHAT_URL"}
        for field, env in names.items():
            value = os.environ.get(env, "").strip()
            if value:
                values[field] = value
        for field in ("queue_url", "table_name", "output_bucket"):
            values.setdefault(field, "")
        for field in ("visibility_seconds", "heartbeat_seconds", "lease_seconds", "ack_timeout",
                      "shutdown_seconds", "max_job_seconds", "poll_seconds", "http_timeout"):
            value = os.environ.get("WORKER_" + field.upper(), "").strip()
            if value:
                values[field] = int(value)
        return cls(**values)
