"""Optional OTLP/HTTP JSON export; no SDK and no contract or secret payloads."""
import hashlib
import json
import os
import re
import httpx
from common.service_http import flag


def otlp(event):
    trace = event.get("trace_id") or hashlib.sha256(event["event_id"].encode()).hexdigest()[:32]
    end = event["timestamp_ns"]
    start = end - max(0, int(event.get("duration_ms", 0) * 1_000_000))
    metadata = {key: event[key] for key in ("job_id", "runtime_id", "contract_id", "request_id", "progress", "retry", "error_type") if key in event}
    attrs = {"service.name": event["service"], "langfuse.trace.name": "contract.workflow",
             # Progress markers (running/started/idle) are points in time, not zero-length spans.
             "langfuse.observation.type": "generation" if "input_tokens" in event else (
                 "span" if event["status"] in ("completed", "failed") else "event")}
    if event["status"] == "failed":
        attrs["langfuse.observation.level"] = "ERROR"
        attrs["langfuse.observation.status_message"] = event.get("error_type", "failed")
    attrs.update({"langfuse.observation.metadata." + key: str(value) for key, value in metadata.items()})
    attrs.update({"langfuse.trace.metadata." + key: str(value) for key, value in metadata.items()})
    if event.get("job_id"):
        attrs["session.id"] = event["job_id"]
    if event.get("model"):
        attrs["langfuse.observation.model.name"] = event["model"]
    environment = os.getenv("APP_ENV", "").lower()
    # Langfuse rejects environments outside this pattern; local fixture traces stay separate from production.
    if re.fullmatch(r"(?!langfuse)[a-z0-9_-]{1,40}", environment):
        attrs["langfuse.environment"] = environment
    usage = {key: event[field] for key, field in (("input", "input_tokens"), ("output", "output_tokens"), ("total", "total_tokens"),
                                                 ("input_cache_read", "cache_read_tokens"),
                                                 ("output_reasoning", "reasoning_tokens")) if field in event}
    if usage:
        attrs["langfuse.observation.usage_details"] = json.dumps(usage)
    span = {"traceId": trace, "spanId": event["event_id"][:16], "name": event["service"] + "." + event["stage"], "kind": 1,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
            "status": {"code": 2 if event["status"] == "failed" else 1},
            "attributes": [{"key": key, "value": {"stringValue": value}} for key, value in attrs.items()]}
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": event["service"]}}]},
                                "scopeSpans": [{"scope": {"name": "contract-risk-monitoring"}, "spans": [span]}]}]}


class LangfuseExporter:
    def __init__(self, *, client=None):
        from urllib.parse import urlsplit
        self.enabled = flag("LANGFUSE_ENABLED")
        self.public, self.secret = os.getenv("LANGFUSE_PUBLIC_KEY", ""), os.getenv("LANGFUSE_SECRET_KEY", "")
        self.active = self.enabled and bool(self.public and self.secret)
        self.url = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.query or parts.fragment:
            raise ValueError("Invalid LANGFUSE_BASE_URL")
        self.client = client if client is not None else (httpx.Client(timeout=10, follow_redirects=False) if self.active else None)

    def export(self, event):
        if not self.active:
            return False
        response = self.client.post(self.url + "/api/public/otel/v1/traces", json=otlp(event),
            auth=httpx.BasicAuth(self.public, self.secret), headers={"x-langfuse-ingestion-version": "4"})
        if response.status_code != 200:
            raise RuntimeError("Langfuse export rejected")
        if response.content:
            partial = response.json().get("partialSuccess", {})
            if int(partial.get("rejectedSpans", 0)):
                raise RuntimeError("Langfuse partially rejected spans")
        return True

    def close(self):
        if self.client is not None:
            self.client.close()
