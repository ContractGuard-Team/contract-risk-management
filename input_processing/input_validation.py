"""Validation for extracted-text jobs (HTTP/file validation belongs at its own boundary)."""
from copy import deepcopy
from uuid import uuid4
import re

from common.schemas.contract import canonical
from input_processing.schemas.processed_contract import ChunkOptions, Job, JobError


def parse_job(data: dict, *, has_file=False) -> Job:
    if not isinstance(data, dict):
        raise JobError("Job must be a JSON object")
    for name in ("contract_id",):
        if not isinstance(data.get(name), str) or not data[name].strip():
            raise JobError(f"{name} must be a non-empty string")
    if "source" in data:
        raise JobError("Load source in the worker and pass file= to preprocessing")
    if ("text" in data) == has_file:
        raise JobError("Provide exactly one of text or an already loaded file")
    text = data.get("text", "")
    if "text" in data and (not isinstance(text, str) or not text.strip()):
        raise JobError("text must be a non-empty string")
    extraction = data.get("extraction", {})
    if not isinstance(extraction, dict) or extraction.keys() - {"ocr", "languages"}:
        raise JobError("extraction supports only ocr and languages")
    if extraction.get("ocr", "auto") not in ("auto", "always", "never"):
        raise JobError("extraction.ocr must be auto, always or never")
    languages = extraction.get("languages", "vie+eng")
    if not isinstance(languages, str) or not re.fullmatch(r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", languages):
        raise JobError("Invalid OCR language list")
    command = data.get("command", "preprocess")
    if command != "preprocess":
        raise JobError("Only command='preprocess' is supported")
    version = data.get("contract_version", "1")
    if type(version) not in (str, int) or not str(version).strip():
        raise JobError("contract_version must be a non-empty string or integer")
    job_id = data.get("job_id", f"job-{uuid4().hex}")
    if not isinstance(job_id, str) or not job_id.strip():
        raise JobError("job_id must be a non-empty string")
    options = data.get("options", {})
    if not isinstance(options, dict):
        raise JobError("options must be an object")
    if options.keys() - ChunkOptions.__dataclass_fields__.keys():
        raise JobError("Unknown chunk option")
    maximum = options.get("max_tokens", 512)
    if type(maximum) is not int or maximum < 32:
        raise JobError("options.max_tokens must be an integer >= 32")
    options = {"overlap_tokens": min(64, (maximum - 1) // 2),
               "min_tokens": min(80, maximum - 1), **options}
    opts = ChunkOptions(**options)
    opts.validate()
    metadata = data.get("source_metadata", {})
    if not isinstance(metadata, dict):
        raise JobError("source_metadata must be an object")
    try:
        canonical(metadata)
    except (ValueError, TypeError) as exc:
        raise JobError("source_metadata must contain JSON-compatible values") from exc
    return Job(data["contract_id"].strip(), text, job_id, str(version).strip(),
               command, opts, deepcopy(metadata), deepcopy(extraction))
