"""Already loaded file/text -> extraction/OCR -> source-aware chunks; no storage I/O."""
from dataclasses import replace
import hashlib

from input_processing.chunking import build_chunks
from input_processing.input_validation import parse_job
from input_processing.schemas.processed_contract import JobError, ProcessingLimits, ExtractedDocument, RetrievedFile
from input_processing.text_extraction import extract_document, attach_source_locations
from input_processing.structure_preservation import parse_structure
from input_processing.text_cleaning import normalize_text
from input_processing.token_counter import checked_counter



def uncovered_chars(text, chunks):
    """Non-whitespace characters of the cleaned text that no chunk's source span covers (coverage by offset)."""
    covered = bytearray(len(text))
    for chunk in chunks:
        for span in chunk["source_spans"]:
            covered[span["cleaned_start"]:span["cleaned_end"]] = b"\x01" * (span["cleaned_end"] - span["cleaned_start"])
    return sum(1 for index, char in enumerate(text) if not covered[index] and not char.isspace())

def process_job(job_dict: dict, token_counter=None, *, file=None, ocr_engine=None, limits=None) -> dict:
    """Return an error envelope for invalid jobs; unexpected programming/provider errors propagate."""
    data = job_dict if isinstance(job_dict, dict) else {}
    envelope = {name: data.get(name) for name in ("job_id", "contract_id", "contract_version")}
    envelope["command"] = data.get("command", "preprocess")
    try:
        limits = limits or ProcessingLimits()
        job = parse_job(job_dict, has_file=file is not None)
        envelope.update(job_id=job.job_id, contract_id=job.contract_id, contract_version=job.contract_version)
        if file is not None:
            if not isinstance(file, RetrievedFile):
                raise JobError("file must be a RetrievedFile containing loaded bytes")
            extracted = extract_document(file, options=job.extraction, ocr_engine=ocr_engine, limits=limits)
            job = replace(job, text=extracted.text)
        else:
            extracted = ExtractedDocument(job.text, [], {"type": "inline", "format": "text", "page_count": None})
        if len(job.text) > limits.max_text_chars:
            raise JobError("Input text exceeds max_text_chars")
        count = checked_counter(token_counter)
        document = normalize_text(job.text)
        attach_source_locations(document, extracted)
        if not document.text.strip():
            raise JobError("Text is empty after normalization")
        sections, structure_stats = parse_structure(document)
        warnings = list(extracted.warnings)
        if token_counter is None:
            warnings.append("Token counts use a UTF-8 byte estimate, not a model tokenizer.")
        if not structure_stats["articles_detected"]:
            warnings.append("No article headings detected; chunking paragraphs/tables.")
        chunks = build_chunks(sections, job, document, count, warnings)
        tokens = [chunk["token_count"] for chunk in chunks]
        if not chunks or any(value > job.options.max_tokens for value in tokens):
            raise JobError("No valid chunks within the requested budget")
        missing = uncovered_chars(document.text, chunks)
        if missing:  # text no chunk carries would never reach the agents: fail instead of a silent gap
            raise JobError(f"{missing} characters of the document are in no chunk")
        source_hash = hashlib.sha256(job.text.encode("utf-8")).hexdigest()
        for chunk in chunks:
            chunk["token_count_method"] = "injected" if token_counter is not None else "utf8_bytes_estimate"
            chunk["source_hash"] = source_hash
            chunk["document"] = extracted.metadata
        envelope.update(
            status="ok", cleaned_text=document.text,
            source_hash=source_hash, document=extracted.metadata,
            extraction={"segments": extracted.segments,
                        "ocr_segments": sum("ocr" in s["method"] for s in extracted.segments)},
            stats={"input_chars": len(job.text), "cleaned_chars": len(document.text),
                   **document.stats, **structure_stats, "chunk_count": len(chunks),
                   "max_chunk_tokens": max(tokens), "avg_chunk_tokens": round(sum(tokens) / len(tokens), 1),
                   "under_min_chunk_count": sum(value < job.options.min_tokens for value in tokens)},
            warnings=list(dict.fromkeys(warnings)), chunks=chunks,
        )
    except JobError as exc:
        envelope.update(status="error", error=str(exc), stats={}, warnings=[], chunks=[])
    return envelope
