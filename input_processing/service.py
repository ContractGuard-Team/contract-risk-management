"""HTTP adapter for existing preprocessing, with a structured ContractInput output."""
import base64
from dataclasses import asdict
from common.schemas.contract import ContractInput, ContractChunk
from common.schemas.files import RetrievedFile
from common.service_jobs import job_app
from common.service_http import serve
from input_processing.pipeline import process_job


def validate(payload):
    if not isinstance(payload.get("job"), dict) or not isinstance(payload.get("file"), dict):
        raise ValueError("job and file objects are required")
    file = payload["file"]
    if not isinstance(file.get("filename"), str) or not file["filename"] or len(file["filename"]) > 512:
        raise ValueError("filename is required")
    data = base64.b64decode(file["data_base64"], validate=True)
    if not 0 < len(data) <= 20 * 1024 * 1024:
        raise ValueError("File must contain 1..20 MiB")
    if not isinstance(file.get("metadata", {}), dict):
        raise ValueError("file metadata must be an object")
    for key in ("job_id", "contract_id"):
        if payload["job"].get(key) != payload["identity"][key]:
            raise ValueError("Preprocessing identity mismatch")
    context = payload.get("context", {})
    if not isinstance(context, dict) or {"workflow", "preprocessing", "worker"} & context.keys():
        raise ValueError("Reserved or invalid context")


def handler(payload, progress, guard):
    validate(payload)
    file = payload["file"]
    progress("processing", 10)
    result = process_job(payload["job"], file=RetrievedFile(base64.b64decode(file["data_base64"], validate=True),
                                                         file["filename"], file.get("metadata", {})))
    guard()
    if result["status"] != "ok":
        raise ValueError("Preprocessing rejected the document")
    context = {**payload.get("context", {}), "preprocessing": {
        key: result[key] for key in ("document", "stats", "warnings", "source_hash", "extraction")}}
    chunks = [ContractChunk(c["chunk_id"], c["chunk_index"], c["text"],
                            {k: v for k, v in c.items() if k != "text"}) for c in result["chunks"]]
    contract = ContractInput(result["contract_id"], result["version"], chunks, len(chunks), context)
    contract.validate()
    progress("processing", 95)
    return {"contract": asdict(contract), "stats": result["stats"], "warnings": result["warnings"]}


def create_app():
    return job_app("input-processing", handler, validate=validate)


if __name__ == "__main__":
    serve(create_app, "input-processing")
