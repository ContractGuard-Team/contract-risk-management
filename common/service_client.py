"""Retry bounded, idempotent job submissions and polls between containers."""
import hashlib
import json
import os
import time
from urllib.parse import urlsplit
import httpx


class RemoteServiceError(RuntimeError):
    pass


def call_key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ServiceClient:
    def __init__(self, url, *, token=None, client=None, timeout=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Invalid internal service URL")
        self.url = url.rstrip("/")
        self.token = os.getenv("INTERNAL_API_TOKEN", "") if token is None else token
        if len(self.token) < 32:
            raise ValueError("INTERNAL_API_TOKEN must have at least 32 characters")
        self.client = client or httpx.Client(timeout=float(timeout or os.getenv("HTTP_TIMEOUT_SECONDS", "120")), follow_redirects=False)

    def request(self, method, path, *, guard=lambda: None, request_id=None, **kwargs):
        headers = {"Authorization": "Bearer " + self.token, **kwargs.pop("headers", {})}
        if request_id:
            headers["X-Request-ID"] = request_id
        for attempt in range(3):
            guard()
            try:
                with self.client.stream(method, self.url + path, headers=headers, **kwargs) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise httpx.TransportError("Transient remote status")
                    if response.status_code not in (200, 202):
                        raise RemoteServiceError(f"Remote HTTP status {response.status_code}")
                    content = bytearray()
                    for part in response.iter_bytes():
                        content.extend(part)
                        if len(content) > 40 * 1024 * 1024:
                            raise RemoteServiceError("Remote response is too large")
                    value = json.loads(content)
                    if not isinstance(value, dict):
                        raise RemoteServiceError("Remote response must be an object")
                    return value
            except httpx.TransportError:
                if attempt == 2:
                    raise RemoteServiceError("Remote service unavailable after retries") from None
                time.sleep(0.2 * 2 ** attempt)

    def run_job(self, payload, *, guard=lambda: None, on_progress=lambda _: None, timeout=None):
        key = call_key(payload)
        deadline = time.monotonic() + float(timeout or os.getenv("JOB_TIMEOUT_SECONDS", "3600"))
        def check():
            guard()
            if time.monotonic() >= deadline:
                raise TimeoutError("Internal job exceeded deadline")
        request_id = payload["identity"]["request_id"]
        row = self.request("POST", "/v1/jobs", json=payload, headers={"Idempotency-Key": key},
                           guard=check, request_id=request_id)
        last = None
        while True:
            check()
            if row.get("key") != key or row.get("identity") != payload["identity"]:
                raise RemoteServiceError("Remote job identity mismatch")
            marker = (row.get("stage"), row.get("progress"), row.get("status"))
            if marker != last:
                on_progress(row)
                last = marker
            if row["status"] == "completed":
                if not isinstance(row.get("result"), dict):
                    raise RemoteServiceError("Missing structured job result")
                return row["result"]
            if row["status"] == "failed":
                raise RemoteServiceError("Remote job failed: " + str(row.get("error_type", "Unknown")))
            if row["status"] not in ("queued", "running"):
                raise RemoteServiceError("Unknown remote job status")
            time.sleep(0.25)
            row = self.request("GET", "/v1/jobs/" + key, guard=check, request_id=request_id)

    def close(self):
        self.client.close()
