"""Submit bytes to an external preprocessing service; no local pipeline imports."""
import hashlib
import httpx
from workers.contracts import canonical
from workers.connectors.http import request_json, validate_url


def dispatch_identity(job_id, runtime_id, digest):
    key = hashlib.sha256(canonical([job_id, runtime_id, digest]).encode()).hexdigest()
    return key, key[:32]


class InputProcessingClient:
    def __init__(self, url, *, token="", timeout=60, client=None):
        validate_url(url, "WORKER_INPUT_PROCESSING_URL")
        self.url, self.token = url, token
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False)

    def submit(self, job, runtime_id, file):
        key, trace_id = dispatch_identity(job.job_id, runtime_id, hashlib.sha256(file.data).hexdigest())
        payload = {**job.preprocessing, "runtime_id": runtime_id, "trace_id": trace_id,
                   "context": job.context, "file_metadata": file.metadata}
        headers = {"Idempotency-Key": key}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        response = request_json(self.client, "POST", self.url, max_bytes=64 * 1024, accepted=(200, 202),
            headers=headers, data={"job": canonical(payload)},
            files={"file": (file.filename, file.data, "application/octet-stream")})
        expected = {"job_id": job.job_id, "runtime_id": runtime_id, "contract_id": job.contract_id, "trace_id": trace_id}
        if response.get("status") != "accepted" or any(response.get(k) != v for k, v in expected.items()):
            raise ValueError("Preprocessing acknowledgement has an invalid identity or status")
        return trace_id

    def close(self):
        self.client.close()
