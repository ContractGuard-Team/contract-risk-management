"""Validated queue payloads; AWS transport stays in workers.connectors."""
from pathlib import PurePosixPath
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from workers.contracts import canonical
from workers.contracts import SourceError


@dataclass(frozen=True)
class QueueJob:
    job_id: str
    contract_id: str
    contract_version: str
    source: dict
    runtime_id: str | None
    preprocessing: dict
    context: dict
    info: dict

    @property
    def fingerprint(self):
        return hashlib.sha256(canonical(self.info).encode()).hexdigest()

    @classmethod
    def from_body(cls, body, *, default_bucket=None):
        data = json.loads(body) if isinstance(body, str) else deepcopy(body)
        if not isinstance(data, dict):
            raise SourceError("SQS body must be a JSON object")
        for name in ("job_id", "contract_id"):
            if not isinstance(data.get(name), str) or not data[name].strip() or len(data[name]) > 256:
                raise SourceError(f"{name} is required (1..256 characters)")
        runtime = data.get("runtime_id", data.get("runtime-id"))
        if "runtime_id" in data and "runtime-id" in data and data["runtime_id"] != data["runtime-id"]:
            raise SourceError("Conflicting runtime_id and runtime-id")
        if runtime is not None and (not isinstance(runtime, str) or not runtime.strip() or len(runtime) > 128):
            raise SourceError("runtime_id must be a non-empty string of at most 128 characters")
        # Same field names as ContractGuard's request.json: source_file_key (+ bucket, default WORKER_INPUT_BUCKET).
        if not isinstance(data.get("source_file_key"), str) or not data["source_file_key"].strip():
            raise SourceError("source_file_key is required")
        source = {"type": "s3", "bucket": data.get("bucket", default_bucket), "key": data["source_file_key"]}
        validate_source(source)
        if source["type"] != "s3":
            raise SourceError("Queue worker accepts only S3 sources")
        preprocess = {key: data[key] for key in ("job_id", "contract_id", "contract_version", "options", "extraction", "source_metadata") if key in data}
        version = data.get("contract_version", "1")
        if type(version) not in (str, int) or not str(version).strip():
            raise SourceError("contract_version must be a non-empty string or integer")
        # Preserve opaque options for the HTTP transport; their business validation
        # belongs to the separate preprocessing component.
        for name in ("options", "extraction", "source_metadata"):
            if name in preprocess and not isinstance(preprocess[name], dict):
                raise SourceError(f"{name} must be an object")
        preprocess.update(job_id=data["job_id"], contract_id=data["contract_id"].strip(), contract_version=str(version).strip())
        context = data.get("context", {})
        if not isinstance(context, dict) or {"workflow", "preprocessing", "worker"} & context.keys():
            raise SourceError("context must be an object without reserved workflow/preprocessing/worker fields")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SourceError("metadata must be an object")
        info = {**preprocess, "source": source, "context": context, "metadata": metadata}
        if len(canonical(info).encode("utf-8")) > 32 * 1024:
            raise SourceError("Job metadata exceeds 32 KiB; put document content in S3")
        return cls(preprocess["job_id"], preprocess["contract_id"], preprocess["contract_version"], source, runtime,
                   preprocess, context, info)

def validate_source(source):
    if not isinstance(source, dict):
        raise SourceError("source must be an object")
    kind = source.get("type")
    fields = {"local": ({"path"}, {"type", "path"}),
              "s3": ({"bucket", "key"}, {"type", "bucket", "key", "version_id"})}
    if not isinstance(kind, str) or kind not in fields:
        raise SourceError("source.type must be local or s3")
    required, allowed = fields[kind]
    if source.keys() - allowed:
        raise SourceError("Unknown source field")
    for name in required | (source.keys() & {"version_id"}):
        if not isinstance(source.get(name), str) or not source[name].strip():
            raise SourceError(f"source.{name} must be a non-empty string")
    filename = source["path"] if kind == "local" else source["key"]
    if PurePosixPath(filename.replace("\\", "/")).suffix.lower() not in {
            ".txt", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        raise SourceError("Supported files: TXT, PDF, DOCX, PNG, JPEG, TIFF")
