"""Read Langfuse Observations API v2 by trace_id without installing its SDK."""
from datetime import datetime, timezone
import json
import httpx
from workers.connectors.http import request_json, validate_url


class LangfuseClient:
    def __init__(self, base_url, public_key, secret_key, *, client=None, timeout=30, max_pages=100):
        validate_url(base_url, "WORKER_LANGFUSE_BASE_URL")
        if not public_key or not secret_key:
            raise ValueError("Langfuse read credentials are required")
        self.url = base_url.rstrip("/") + "/api/public/v2/observations"
        self.auth = httpx.BasicAuth(public_key, secret_key)
        self.max_pages = max_pages
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False)

    def observations(self, trace_id, from_start_time, *, guard=lambda: None):
        params = {"traceId": trace_id, "fromStartTime": from_start_time,
                  "toStartTime": datetime.now(timezone.utc).isoformat(), "limit": 100,
                  "fields": "core,basic,metadata,io", "expandMetadata": "job_id,runtime_id,contract_id,worker_event"}
        rows, cursors = [], set()
        for _ in range(self.max_pages):
            guard()
            page = request_json(self.client, "GET", self.url, params=params, auth=self.auth)
            if not isinstance(page.get("data"), list) or not isinstance(page.get("meta", {}), dict):
                raise ValueError("Invalid Langfuse observations response")
            rows.extend(page["data"])
            cursor = page.get("meta", {}).get("cursor")
            if not cursor:
                return rows
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Invalid or repeated Langfuse pagination cursor")
            cursors.add(cursor)
            params["cursor"] = cursor
        raise ValueError("Langfuse observation page limit exceeded")

    def close(self):
        self.client.close()


def parse_object(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object in observation")
    return value


def progress_events(rows, identity):
    """Application metadata contract, not a built-in Langfuse workflow status.

    Only explicit worker_event observations may affect job state. Model generations
    and unrelated spans cannot declare the entire contract complete.
    """
    events = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("traceId") != identity["trace_id"]:
            continue
        metadata = parse_object(row.get("metadata") or {})
        if "worker_event" not in metadata:
            continue
        if any(metadata.get(key) != identity[key] for key in ("job_id", "runtime_id", "contract_id")):
            raise ValueError("Langfuse progress identity mismatch")
        event = parse_object(metadata["worker_event"])
        sequence, stage, state = event.get("sequence"), event.get("stage"), event.get("state")
        if type(sequence) is not int or sequence <= 0 or stage not in ("processing", "agent_1", "agent_2", "agent_3"):
            raise ValueError("Invalid Langfuse worker_event sequence/stage")
        if state not in ("started", "completed", "failed"):
            raise ValueError("Invalid Langfuse worker_event state")
        progress = event.get("progress")
        if type(progress) is not int or not 0 <= progress <= 95:
            raise ValueError("Worker progress must be an integer from 0 to 95")
        if state == "completed" and not row.get("endTime"):
            continue  # observation not yet finalized/visible in the read model
        result = None
        if stage == "agent_3" and state == "completed":
            result = parse_object(row.get("output"))
            if result.get("schema_version") != "2.0.0" or result.get("job_id") != identity["job_id"]:
                raise ValueError("Final observation must contain this job's extraction.v2")
        value = {"source_sequence": sequence, "stage": stage, "state": state,
                 "progress": progress, "result": result, "observation_id": row.get("id")}
        if sequence in events and events[sequence] != value:
            raise ValueError("Conflicting Langfuse progress sequence")
        events[sequence] = value
    return [events[index] for index in sorted(events)]
