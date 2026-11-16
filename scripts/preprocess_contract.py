"""Standalone local-file/text preprocessing CLI; no worker or agent connections."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from input_processing.pipeline import process_job
from common.schemas.files import RetrievedFile
from input_processing.schemas.processed_contract import JobError, ProcessingLimits


def load_local_file(source, local_root=None):
    if not isinstance(source, dict) or source.get("type") != "local":
        raise JobError("Standalone preprocessing accepts local files or text; S3 belongs to the worker")
    if not isinstance(source.get("path"), str) or not source["path"].strip():
        raise JobError("source.path must be a non-empty local path")
    root = Path(local_root).resolve() if local_root else None
    path = Path(source["path"])
    path = ((root / path) if root and not path.is_absolute() else path).resolve()
    if root and not path.is_relative_to(root):
        raise JobError("Local file is outside the permitted directory")
    maximum = ProcessingLimits().max_file_bytes
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if not data or len(data) > maximum:
        raise JobError("Local file is empty or exceeds max_file_bytes")
    return RetrievedFile(data, path.name, {"type": "local", "filename": path.name,
        "file_size": len(data), "file_sha256": hashlib.sha256(data).hexdigest()})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preprocess extracted contract text")
    parser.add_argument("job", nargs="?", help="Job JSON path, or '-' for stdin")
    parser.add_argument("--text-file")
    parser.add_argument("--file", help="Local TXT/PDF/DOCX/PNG/JPEG/TIFF")
    parser.add_argument("--local-root", help="Restrict local paths to an upload directory")
    parser.add_argument("--ocr", choices=("auto", "always", "never"))
    parser.add_argument("--languages", help="Installed Tesseract languages, e.g. vie+eng")
    parser.add_argument("--contract-id")
    parser.add_argument("--job-id")
    parser.add_argument("--contract-version", default="1")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--overlap-tokens", type=int)
    parser.add_argument("--jsonl", action="store_true")
    parser.add_argument("-o", "--output")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.job, args.text_file, args.file)) != 1:
        parser.error("Choose exactly one job JSON, --text-file or --file")
    try:
        if args.text_file or args.file:
            job = {"contract_id": args.contract_id, "contract_version": args.contract_version,
                   "source": {"type": "local", "path": args.text_file or args.file}}
        else:
            raw = sys.stdin.read() if args.job == "-" else Path(args.job).read_text(encoding="utf-8-sig")
            job = json.loads(raw)
        if isinstance(job, dict):
            if args.job_id is not None:
                job["job_id"] = args.job_id
            options = job.get("options", {})
            if isinstance(options, dict):
                for name in ("max_tokens", "overlap_tokens"):
                    if getattr(args, name) is not None:
                        options[name] = getattr(args, name)
                job["options"] = options
            extraction = job.get("extraction", {})
            if isinstance(extraction, dict):
                for name in ("ocr", "languages"):
                    if getattr(args, name) is not None:
                        extraction[name] = getattr(args, name)
                job["extraction"] = extraction
        file = None
        if isinstance(job, dict) and "source" in job:
            file = load_local_file(job.pop("source"), args.local_root)
        result = process_job(job, file=file)
        if result["status"] != "ok":
            print(result["error"], file=sys.stderr)
            if args.jsonl:
                return 1  # Never overwrite an existing output with a false-success empty file.
        payload = ("\n".join(json.dumps(c, ensure_ascii=False) for c in result["chunks"])
                   if args.jsonl else json.dumps(result, ensure_ascii=False, indent=2))
        if args.output:
            Path(args.output).write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
        return 0 if result["status"] == "ok" else 1
    except (OSError, UnicodeError, json.JSONDecodeError, JobError) as exc:
        print(f"Input/output error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Windows consoles may otherwise use a code page unable to represent Vietnamese.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
