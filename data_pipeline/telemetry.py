"""What the data pipeline is doing: a readable log line on the terminal/journald AND a monitoring event.

Events go to the monitoring service (MONITORING_URL, set from DATA_MONITORING_URL) as data-collector /
data-processor; one crawl run = one trace_id, so GET /v1/traces/<trace_id> on monitoring shows the run timeline.
They carry no job_id, so monitoring never forwards them to the contract worker. Legal texts are public: messages
and tracebacks are logged in full (configure(..., error_details=True)).
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
import time
from uuid import uuid4

from monitoring.client import emit

log = logging.getLogger("data_pipeline")
_trace = ContextVar("data_pipeline_trace", default=None)


def service():
    return os.getenv("SERVICE_NAME", "data-collector")


def report(stage, status, message="", *, level=logging.INFO, exc_info=None, **fields):
    """fields: document (law symbol), count, duration_ms, error_type; anything else is only logged."""
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None and k != "error_type")
    log.log(level, "[%s %s] %s%s", stage, status, message, f" ({extra})" if extra else "", exc_info=exc_info)
    if exc_info is not None and "error_type" not in fields:
        fields["error_type"] = type(exc_info).__name__ if isinstance(exc_info, BaseException) else None
    emit(service(), stage, status, echo=False, trace_id=_trace.get(), **fields)


@contextmanager
def run_trace():
    """One trace id for a crawl run / processor cycle; asyncio.to_thread copies it into worker threads."""
    token = _trace.set(uuid4().hex)
    try:
        yield _trace.get()
    finally:
        _trace.reset(token)


def elapsed_ms(start):
    return round((time.monotonic() - start) * 1000, 1)
