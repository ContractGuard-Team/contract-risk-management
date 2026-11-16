"""ContractGuard protocol (07-worker-aws-integration.md §1-4) layered on the existing worker.

Enabled by WORKER_RESULTS_QUEUE_URL. The worker architecture is unchanged (SQS intake, DynamoDB lease/progress,
input-processing -> LangGraph -> 3 agents, result in S3 before completion before ACK); this module only:
- turns `analysis_requested` + results/<job>/request.json into the worker's QueueJob, answers `ping` with `pong`;
- publishes progress / completed / failed to the analysis-results queue;
- writes the validated source to clean_file_key, the result to result_key, and removes the quarantine file.
Every S3 key comes from request.json (never composed here) and is checked against its required prefix.
"""
import json
import logging
import mimetypes
import re
import time

from workers.contracts import SourceError

log = logging.getLogger(__name__)
MESSAGE_VERSION = "1"
MAX_RECEIVES = 3            # partner queues move a message to the DLQ after 3 receives
KEEPALIVE_SECONDS = 300     # backend marks a job failed after 20 min without any message
PREFIXES = {"source_file_key": "quarantine/", "clean_file_key": "clean/", "result_key": "results/"}
# Limits the worker enforces itself (only ever tighter than the host limits).
# shortcut: key names assumed from the integration doc; confirm against contracts/analysis-request.v1.schema.json.
LIMIT_KEYS = ("max_file_bytes", "max_pages")
# shortcut: provisional codes until ContractGuard shares its error_code catalogue; agree them with SE.
ERR_INPUT, ERR_PROCESSING = "ERR-AI-001", "ERR-AI-002"


# §3 worker -> backend messages: exact field set per event_type (message_version/event_type/job_id always present).
OUTBOUND = {"progress": {"step"}, "completed": {"result_key"}, "failed": {"failure_reason", "error_code"}, "pong": set()}


def check_outbound(message):
    """Refuse to publish anything that does not match §3 (backend drops malformed messages silently).
    shortcut: hand-written from the integration doc; swap in contracts/queue-messages.v1.schema.json once shared."""
    fields = OUTBOUND.get(message.get("event_type"))
    if fields is None or set(message) != {"message_version", "event_type", "job_id", *fields}:
        raise ValueError(f"Outbound message does not match §3: {sorted(message)}")
    for name in ("message_version", "job_id", *fields):
        if not isinstance(message[name], str) or not message[name].strip():
            raise ValueError(f"Outbound {name} must be a non-empty string")
    if message["message_version"] != MESSAGE_VERSION or len(message["job_id"]) > 256:
        raise ValueError("Outbound message_version/job_id invalid")
    if "result_key" in message and not re.fullmatch(r"results/[^/]+/extraction\.json", message["result_key"]):
        raise ValueError("Outbound result_key must be results/<job>/extraction.json")
    if len(message.get("failure_reason", "")) > 300 or len(message.get("step", "")) > 64:
        raise ValueError("Outbound failure_reason/step too long")
    return message


def is_input_error(exc):
    """The request itself is wrong (keys, format, size, conflicting job_id): retrying cannot help.
    Internal ValueErrors (e.g. an invalid workflow result) are NOT input errors: they are retried."""
    return isinstance(exc, (SourceError, json.JSONDecodeError))


def _key(value, name, prefix):
    if not isinstance(value, str) or not value.startswith(prefix) or ".." in value or len(value) > 1024:
        raise SourceError(f"{name} must be an S3 key under {prefix}")
    return value


def extraction(event, result, partner):
    """Content written to result_key.
    shortcut: provisional layout (document.clean_file_key + agent output); map to contracts/extraction.v2.schema.json
    once ContractGuard shares it, the backend rejects results that do not validate."""
    return {"schema": "extraction.v2", "job_id": event["job_id"],
            "document": {"clean_file_key": partner["clean_file_key"], "source_file_key": partner["source_file_key"]},
            "output_language": partner.get("output_language"), "result": result}


class PartnerProtocol:
    def __init__(self, results_queue, storage, bucket, *, clock=time.monotonic):
        self.results, self.storage, self.bucket, self.clock = results_queue, storage, bucket, clock
        self.last = {}  # job_id -> (monotonic time, step) of the last message sent for keepalive

    # --- inbound -------------------------------------------------------------------------------------
    def read(self, body):
        """("ping", job_id) | ("job", QueueJob body). Raises SourceError for a request that can never succeed."""
        message = json.loads(body) if isinstance(body, str) else body
        if not isinstance(message, dict) or message.get("message_version") != MESSAGE_VERSION:
            raise SourceError("Unsupported ContractGuard message_version")
        job_id = message.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 256:
            raise SourceError("job_id is required")
        if message.get("event_type") == "ping":
            return "ping", job_id
        if message.get("event_type") != "analysis_requested":
            raise SourceError("Unsupported event_type")
        request_key = message.get("request_key")
        if request_key != f"results/{job_id}/request.json":
            raise SourceError("request_key must be results/<job_id>/request.json of this job")
        data, _ = self.storage.read(self.bucket, request_key, max_bytes=256 * 1024)
        request = json.loads(data)
        if not isinstance(request, dict):
            raise SourceError("request.json must be an object")
        if request.get("job_id") not in (None, job_id):
            raise SourceError("request.json belongs to another job_id")
        keys = {name: _key(request.get(name), name, prefix) for name, prefix in PREFIXES.items()}
        # The partner's IAM only allows results/*/extraction.json: catch a wrong key before running the agents.
        if keys["result_key"] != f"results/{job_id}/extraction.json":
            raise SourceError("result_key must be results/<job_id>/extraction.json")
        # clean/{workspace}/{contract}/v{n}/source.{ext}: contract and version identify the document version.
        parts = keys["clean_file_key"].split("/")
        contract_id = request.get("contract_id") or (parts[2] if len(parts) > 4 else job_id)
        version = str(request.get("contract_version") or (parts[3].lstrip("v") if len(parts) > 4 else "1"))
        context = request.get("context") if isinstance(request.get("context"), dict) else {}
        language, limits = request.get("output_language"), request.get("limits")
        if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", language)):
            raise SourceError("output_language must be a language code such as vi or en")
        if limits is not None and not isinstance(limits, dict):
            raise SourceError("limits must be an object")
        for name in LIMIT_KEYS:
            value = (limits or {}).get(name)
            if value is not None and (type(value) is not int or value <= 0):
                raise SourceError(f"limits.{name} must be a positive integer")
        # Agents read these from their context (input-processing passes context through unchanged).
        context = {**context, **({"output_language": language} if language else {}), **({"limits": limits} if limits else {}),
                   # Agent 3 reads the original through MCP partner_document with these keys (never builds keys itself).
                   "source_document": {"bucket": self.bucket, "source_file_key": keys["source_file_key"],
                                       "clean_file_key": keys["clean_file_key"]}}
        partner = {**keys, "request_key": request_key, "output_language": language, "limits": limits}
        return "job", {"job_id": job_id, "contract_id": str(contract_id), "contract_version": version,
                       "source_file_key": keys["source_file_key"], "bucket": self.bucket,
                       "context": {k: v for k, v in context.items() if k not in ("workflow", "preprocessing", "worker")},
                       "metadata": {"contractguard": partner}}

    # --- outbound ------------------------------------------------------------------------------------
    def send(self, event_type, job_id, **fields):
        message = check_outbound({"message_version": MESSAGE_VERSION, "event_type": event_type, "job_id": job_id, **fields})
        self.results.send(json.dumps(message, ensure_ascii=False))
        if event_type in ("completed", "failed"):
            self.last.pop(job_id, None)
        else:
            self.last[job_id] = (self.clock(), fields.get("step"))

    def progress(self, job_id, step):
        if self.last.get(job_id, (0, None))[1] != step:
            self.send("progress", job_id, step=step)

    def keepalive(self, job_id):
        """Called by the heartbeat: repeat the current step so the backend's 20-minute timeout never fires."""
        sent, step = self.last.get(job_id, (None, None))
        if step and self.clock() - sent >= KEEPALIVE_SECONDS:
            self.send("progress", job_id, step=step)

    def failed(self, job_id, exc):
        reason = str(exc)[:300] if isinstance(exc, SourceError) else type(exc).__name__  # never contract text
        self.send("failed", job_id, failure_reason=reason, error_code=ERR_INPUT if is_input_error(exc) else ERR_PROCESSING)

    # --- S3 ------------------------------------------------------------------------------------------
    def store_clean(self, partner, file):
        """After input-processing validated the file: keep its bytes as the checked copy under clean/."""
        content_type = ((file.metadata.get("service") or {}).get("ContentType")
                        or mimetypes.guess_type(partner["clean_file_key"])[0] or "application/octet-stream")
        self.storage.write_bytes(self.bucket, partner["clean_file_key"], file.data, content_type)

    def remove_quarantine(self, partner):
        """Cleanup after `completed` was sent: a failure is logged, never turned into a retry or a `failed`."""
        try:
            self.storage.delete(self.bucket, partner["source_file_key"])
        except Exception as exc:
            log.warning("could not remove %s (%s); the result is already delivered",
                        partner["source_file_key"], type(exc).__name__)
