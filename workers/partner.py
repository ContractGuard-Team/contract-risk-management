"""ContractGuard protocol (07-worker-aws-integration.md §1-4) layered on the existing worker.

Enabled by WORKER_RESULTS_QUEUE_URL. The worker architecture is unchanged (SQS intake, DynamoDB lease/progress,
input-processing -> LangGraph -> 3 agents, result in S3 before completion before ACK); this module only:
- turns `analysis_requested` + results/<job>/request.json into the worker's QueueJob, answers `ping` with `pong`;
- publishes progress / completed / failed to the analysis-results queue;
- writes the validated source to clean_file_key, the result to result_key, and removes the quarantine file.
Business schemas (common/schemas/business/): inbound.py = everything ContractGuard sends (InboundMessage,
AnalysisRequest — every field kept and passed to the agents as context["request"]); outbound.py = everything it
receives (OutboundMessage, AnalysisResult — built and validated by the LangGraph service). Every S3 key comes from
request.json, never composed here.
"""
import json
import logging
import mimetypes
import time

from pydantic import ValidationError

from common.schemas.business.inbound import AnalysisRequest, InboundMessage
from common.schemas.business.outbound import OutboundMessage
from common.service_client import RemoteServiceError
from workers.contracts import SourceError

log = logging.getLogger(__name__)
MESSAGE_VERSION = "1"
MAX_RECEIVES = 3            # partner queues move a message to the DLQ after 3 receives
KEEPALIVE_SECONDS = 300     # backend marks a job failed after 20 min without any message
# Worker stage -> ContractGuard progress step; stages not listed send nothing.
STEPS = {"downloading": "reading_file", "dispatching": "reading_file", "processing": "reading_file",
         "agent_1": "splitting_clauses", "agent_2": "evaluating", "agent_3": "evaluating", "saving_result": "scoring"}
# ERR-UPL-004 (more than the allowed pages) is the partner's own code; the two AI codes stay provisional.
# shortcut: provisional codes until ContractGuard shares its full ERR-* catalogue; agree them with SE.
ERR_INPUT, ERR_PROCESSING, ERR_TOO_MANY_PAGES = "ERR-AI-001", "ERR-AI-002", "ERR-UPL-004"
ERR_CANCELLED = "ERR-AI-003"  # provisional too: queue-messages.v1 has no `cancelled` event, so `failed` carries it


def input_error(message, code=None):
    """SourceError carrying the ERR-* code the `failed` message reports."""
    error = SourceError(message)
    error.error_code = code
    return error


def _invalid(what, exc: ValidationError) -> SourceError:
    detail = "; ".join(f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()[:5])
    return SourceError(f"{what}: {detail}")


def check_outbound(message):
    """Refuse to publish anything that is not an OutboundMessage (the backend drops malformed messages silently).
    Returns the message exactly as it will be sent."""
    try:
        model = OutboundMessage.validate_python(message)
    except ValidationError as exc:
        raise ValueError(str(_invalid("Outbound message", exc))) from None
    data = model.model_dump(exclude_none=True)
    if data != message:
        raise ValueError(f"Outbound message has unexpected fields: {sorted(set(message) ^ set(data))}")
    if data["event_type"] == "completed" and not data["result_key"].endswith("/extraction.json"):
        raise ValueError("Outbound result_key must be results/<job>/extraction.json")  # partner IAM: this name only
    return data


def safe_reason(exc):
    """Why a job stopped, never contract text: our own input/remote errors as written, any other only by its type."""
    return str(exc)[:300] if isinstance(exc, (SourceError, RemoteServiceError)) else type(exc).__name__


def is_input_error(exc):
    """The request itself is wrong (keys, format, size, conflicting job_id): retrying cannot help.
    Internal ValueErrors (e.g. an invalid workflow result) are NOT input errors: they are retried."""
    return isinstance(exc, (SourceError, json.JSONDecodeError))


def extraction(event, result, partner):
    """Content written to result_key: the AnalysisResult the LangGraph service built and validated."""
    if result.get("schema_version") != "2.0.0" or result.get("job_id") != event["job_id"]:
        raise ValueError("Workflow result is not the AnalysisResult of this job")
    if result["document"].get("clean_file_key") != partner["clean_file_key"]:
        raise ValueError("Result document.clean_file_key differs from request.json")
    return result


class PartnerProtocol:
    def __init__(self, results_queue, storage, bucket, *, clock=time.monotonic):
        self.results, self.storage, self.bucket, self.clock = results_queue, storage, bucket, clock
        self.last = {}  # job_id -> (monotonic time, step) of the last message sent for keepalive

    # --- inbound -------------------------------------------------------------------------------------
    def read(self, body):
        """("ping", job_id) | ("job", QueueJob body). Raises SourceError for a request that can never succeed."""
        try:
            message = InboundMessage.validate_python(json.loads(body) if isinstance(body, str) else body)
        except ValidationError as exc:
            raise _invalid("ContractGuard message", exc) from None
        if message.event_type == "ping":
            return "ping", message.job_id
        data, _ = self.storage.read(self.bucket, message.request_key, max_bytes=256 * 1024)
        try:
            request = AnalysisRequest.model_validate_json(data)
        except ValidationError as exc:
            raise _invalid("request.json", exc) from None
        if request.job_id != message.job_id:
            raise SourceError("request.json belongs to another job_id")
        # Exactly what the partner sent (every field, including any it adds later; no invented nulls).
        whole = request.model_dump(mode="json", exclude_unset=True)
        partner = {**{k: whole[k] for k in ("source_file_key", "clean_file_key", "result_key", "declared_file_type",
                                            "output_language", "limits", "tenant_id")},
                   "request_key": message.request_key}
        # The whole request reaches every agent (and the result builder) as context["request"].
        return "job", {"job_id": request.job_id, "contract_id": request.contract_id,
                       "contract_version": request.contract_version, "source_file_key": request.source_file_key,
                       "bucket": self.bucket, "context": {"request": whole}, "metadata": {"contractguard": partner}}

    # --- outbound ------------------------------------------------------------------------------------
    def send(self, event_type, job_id, **fields):
        message = check_outbound({"message_version": MESSAGE_VERSION, "event_type": event_type, "job_id": job_id, **fields})
        self.results.send(json.dumps(message, ensure_ascii=False))
        if event_type in ("completed", "failed"):
            self.last.pop(job_id, None)
        else:
            self.last[job_id] = (self.clock(), fields.get("step"))

    def progress(self, job_id, stage):
        """Worker stage -> queue-messages.v1 step; one message per step change."""
        step = STEPS.get(stage)
        if step and self.last.get(job_id, (0, None))[1] != step:
            self.send("progress", job_id, step=step)

    def keepalive(self, job_id):
        """Called by the heartbeat: repeat the current step so the backend's 20-minute timeout never fires."""
        sent, step = self.last.get(job_id, (None, None))
        if step and self.clock() - sent >= KEEPALIVE_SECONDS:
            self.send("progress", job_id, step=step)

    def failed(self, job_id, exc):
        code = getattr(exc, "error_code", None) or (ERR_INPUT if is_input_error(exc) else ERR_PROCESSING)
        self.send("failed", job_id, failure_reason=safe_reason(exc), error_code=code)

    def cancelled(self, job_id):
        self.send("failed", job_id, failure_reason="Cancelled by user", error_code=ERR_CANCELLED)

    # --- S3 ------------------------------------------------------------------------------------------
    def store_clean(self, partner, file):
        """After input-processing validated the file: keep its bytes as the checked copy under clean/."""
        content_type = ((file.metadata.get("service") or {}).get("ContentType")
                        or mimetypes.guess_type(partner["clean_file_key"])[0] or "application/octet-stream")
        self.storage.write_bytes(self.bucket, partner["clean_file_key"], file.data, content_type)

    def remove_quarantine(self, partner):
        """Cleanup after `completed` was sent: a failure is logged, never turned into a retry or a `failed`."""
        if not partner["source_file_key"].startswith("quarantine/"):
            return  # the upload is already a checked clean/ file: nothing to remove
        try:
            self.storage.delete(self.bucket, partner["source_file_key"])
        except Exception as exc:
            log.warning("could not remove %s (%s); the result is already delivered",
                        partner["source_file_key"], type(exc).__name__)
