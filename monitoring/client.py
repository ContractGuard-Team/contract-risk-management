"""Bounded, non-blocking telemetry push; stdout JSON stays the always-on fallback.

emit() never waits on the network: a collector outage must not slow contract processing.
shortcut: the send queue is in memory, events still queued when a process dies are only in stdout;
add a per-service file outbox if every event must reach the collector.
"""
import atexit
import json
import os
import queue
import threading
import time
from uuid import uuid4

import httpx


FIELDS = {"job_id", "runtime_id", "contract_id", "trace_id", "request_id", "duration_ms", "progress",
          "error_type", "retry", "input_tokens", "output_tokens", "total_tokens", "model",
          "document", "count"}  # data pipeline: law symbol, number of items (pages/documents/articles)

_queue = queue.Queue(maxsize=int(os.getenv("MONITORING_QUEUE_SIZE", "1000")))
_started = threading.Lock()
_sender = None


def _send_loop():
    with httpx.Client(timeout=2, follow_redirects=False) as client:
        while True:
            url, event = _queue.get()
            try:
                response = client.post(url.rstrip("/") + "/v1/events", json=event,
                    headers={"Authorization": "Bearer " + os.getenv("INTERNAL_API_TOKEN", "")})
                response.raise_for_status()
            except Exception:
                # Telemetry failure cannot change business job state or reveal a response body.
                print(json.dumps({"service": event["service"], "telemetry": "collector_unavailable",
                                  "event_id": event["event_id"]}), flush=True)
            finally:
                _queue.task_done()


def _ensure_sender():
    global _sender
    with _started:
        if _sender is None or not _sender.is_alive():
            _sender = threading.Thread(target=_send_loop, name="telemetry-sender", daemon=True)
            _sender.start()


def flush(timeout=2.0):
    """Give queued events a bounded chance to leave before shutdown."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)
    return _queue.unfinished_tasks == 0


atexit.register(flush)


def emit(service, stage, status, *, echo=True, **fields):
    """echo=False when the caller already logs a readable line for this event (data pipeline)."""
    event = {"event_id": uuid4().hex, "timestamp_ns": time.time_ns(),
             "service": service, "stage": stage, "status": status,
             **{key: value for key, value in fields.items() if key in FIELDS and value is not None}}
    if echo:
        print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)
    url = os.getenv("MONITORING_URL", "")
    if url:
        _ensure_sender()
        try:
            _queue.put_nowait((url, event))
        except queue.Full:
            print(json.dumps({"service": service, "telemetry": "dropped_queue_full", "event_id": event["event_id"]}), flush=True)
    return event
