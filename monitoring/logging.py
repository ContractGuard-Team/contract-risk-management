"""One log setup for every service: JSON lines for Docker/journald/CloudWatch, readable lines in a terminal.

LOG_FORMAT=auto (default: text when stdout is a terminal, else JSON) | json | text.
Errors always carry `error_type` and `error_at` (innermost file:line in function) plus the stack frames (code lines
only). The exception MESSAGE is left out by default because it can quote contract text; services that handle only
public data (the legal data pipeline) call configure(..., error_details=True) to include message and full traceback.
"""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import traceback

# These log full request URLs or protocol chatter at INFO; queries may quote contract text.
QUIET = ("httpx", "httpcore", "botocore", "boto3", "urllib3", "mcp")


def _error_fields(exc_info, details):
    kind, value, tb = exc_info
    frames = traceback.extract_tb(tb)
    fields = {"error_type": kind.__name__}
    if frames:
        where = [f"{'/'.join(Path(f.filename).parts[-3:])}:{f.lineno} in {f.name}" for f in frames[-12:]]
        fields["error_at"], fields["stack"] = where[-1], where
    if details:
        fields["error"] = str(value)[:2000]
        fields["traceback"] = "".join(traceback.format_exception(kind, value, tb))[-8000:]
    return fields


class JsonFormatter(logging.Formatter):
    def __init__(self, service, details=False):
        super().__init__()
        self.service, self.details = service, details

    def entry(self, record):
        entry = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                 "level": record.levelname, "service": self.service, "logger": record.name,
                 "message": record.getMessage()}
        if record.processName != "MainProcess":
            entry["process"] = record.processName
        if record.exc_info and record.exc_info[0]:
            entry.update(_error_fields(record.exc_info, self.details))
        return entry

    def format(self, record):
        return json.dumps(self.entry(record), ensure_ascii=False, default=str)


class TextFormatter(JsonFormatter):
    """Human-readable terminal line; the error block follows on the next lines."""
    def format(self, record):
        entry = self.entry(record)
        time = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        line = f"{time} {entry['level']:<7} {self.service} {entry['logger']}: {entry['message']}"
        if "error_type" in entry:
            line += f"\n    {entry['error_type']} at {entry.get('error_at', '?')}"
            if "traceback" in entry:
                line += "\n" + entry["traceback"].rstrip()
            elif entry.get("stack"):
                line += "\n" + "\n".join("      " + frame for frame in entry["stack"])
        return line


def configure(service, level=None, *, error_details=False, stream=None):
    stream = stream or sys.stdout
    mode = os.getenv("LOG_FORMAT", "auto").lower()
    text = mode == "text" or (mode == "auto" and hasattr(stream, "isatty") and stream.isatty())
    handler = logging.StreamHandler(stream)
    handler.setFormatter((TextFormatter if text else JsonFormatter)(service, error_details))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.getenv("LOG_LEVEL", "INFO"))
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    return text
