"""Read-only access to the ORIGINAL contract in ContractGuard's S3 bucket, for Agent 3 (tool partner_document).

The key comes from the job context (context.request.source_file_key: the whole request.json, passed by workers/partner.py).
Bucket = PARTNER_S3_BUCKET (compose maps it from WORKER_INPUT_BUCKET). Only quarantine/ and clean/ keys; every
S3 call other than reads is refused here, and the partner's bucket policy grants this role GetObject only.
Text files are decoded directly; PDF/DOCX/images go through the existing input-processing service (same
extraction and OCR as the contract pipeline), so the MCP image needs no document libraries.
"""
import base64
import hashlib
import json
import os

from common.exceptions import IntegrationNotConfigured
from mcp_server.s3_reader import _refuse_writes

PREFIXES = ("quarantine/", "clean/")
MAX_BYTES = 20 * 1024 * 1024  # same limit as input-processing
MAX_CHARS = 15_000            # per call, so the whole page fits the agent's tool-result limit; page with offset
CACHE_SIZE = 8


def _key(value):
    if (not isinstance(value, str) or not value.startswith(PREFIXES) or len(value) > 1024
            or ".." in value.split("/") or "//" in value):
        raise ValueError("key must be the source_file_key/clean_file_key of this job (quarantine/... or clean/...)")
    return value


class PartnerDocuments:
    def __init__(self, bucket=None, *, client=None, processor=None):
        self.bucket = (os.getenv("PARTNER_S3_BUCKET", "") if bucket is None else bucket).strip()
        if not self.bucket:
            raise IntegrationNotConfigured("TODO: set PARTNER_S3_BUCKET (ContractGuard bucket = WORKER_INPUT_BUCKET)")
        if client is None:
            import boto3
            client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or None)
        client.meta.events.register("before-call.s3", _refuse_writes)
        self.client, self._processor, self.cache, self.requests = client, processor, {}, {}

    def processor(self):
        if self._processor is None:
            from common.service_client import ServiceClient
            url = os.getenv("INPUT_PROCESSING_URL", "")
            if not url:
                raise IntegrationNotConfigured("TODO: set INPUT_PROCESSING_URL for binary documents")
            self._processor = ServiceClient(url)
        return self._processor

    def job_keys(self, job_id):
        """The documents of this job, read from its own results/<job_id>/request.json in the partner bucket
        (independent of anything the calling agent claims). Unknown job -> no document."""
        if not isinstance(job_id, str) or not job_id.strip() or "/" in job_id or ".." in job_id:
            return []
        if job_id not in self.requests:
            if len(self.requests) >= CACHE_SIZE:
                self.requests.pop(next(iter(self.requests)))
            try:
                obj = self.client.get_object(Bucket=self.bucket, Key=f"results/{job_id}/request.json")
                request = json.loads(obj["Body"].read(256 * 1024))
            except Exception:  # missing/unreadable request.json: nothing is readable for this job
                return []
            self.requests[job_id] = [request.get(name) for name in ("source_file_key", "clean_file_key")
                                     if isinstance(request.get(name), str)]
        return self.requests[job_id]

    def _extract(self, key, data):
        if key.lower().endswith(".txt"):  # by type, not by decodability: an all-ASCII PDF decodes as UTF-8 too
            return data.decode("utf-8", errors="replace"), {"extraction": "utf-8"}
        digest = hashlib.sha256(data).hexdigest()
        job_id = "partner-doc-" + digest[:32]  # same bytes -> same idempotent processing job (no second OCR)
        identity = {"job_id": job_id, "runtime_id": "mcp", "contract_id": job_id, "trace_id": digest[:32],
                    "request_id": digest[:32]}
        payload = {"identity": identity, "context": {},
                   "job": {"job_id": job_id, "contract_id": job_id, "options": {"overlap_tokens": 0}},
                   "file": {"filename": key.rsplit("/", 1)[-1], "metadata": {},
                            "data_base64": base64.b64encode(data).decode("ascii")}}
        contract = self.processor().run_job(payload)["contract"]
        chunks = sorted(contract["chunks"], key=lambda c: c["index"])
        document = (contract.get("context", {}).get("preprocessing") or {}).get("document") or {}
        return "\n".join(c["text"] for c in chunks), {"extraction": "input-processing",
                                                      "page_count": document.get("page_count")}

    def read(self, key, offset=0, max_chars=MAX_CHARS, *, allowed=()):
        """allowed: the keys of the calling job (request.json); any other document is refused."""
        key = _key(key)
        if key not in allowed:
            raise PermissionError("key is not a document of this job (use context.request.source_file_key)")
        offset, max_chars = max(0, int(offset)), max(1, min(int(max_chars), MAX_CHARS))
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        data = obj["Body"].read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Document exceeds 20 MiB")
        cache_key = (key, obj.get("ETag") or hashlib.sha256(data).hexdigest())
        if cache_key not in self.cache:
            if len(self.cache) >= CACHE_SIZE:
                self.cache.pop(next(iter(self.cache)))
            self.cache[cache_key] = self._extract(key, data)
        text, info = self.cache[cache_key]
        part = text[offset:offset + max_chars]
        end = offset + len(part)
        return {"bucket": self.bucket, "key": key, "content_type": obj.get("ContentType", ""), **info,
                "characters": len(text), "offset": offset, "text": part,
                "next_offset": end if end < len(text) else None}
