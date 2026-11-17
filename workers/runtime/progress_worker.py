"""Process 2: persist ordered progress and Agent 3 output before acknowledging events."""
import hashlib
import time
from queue import Empty
from uuid import uuid4
from types import SimpleNamespace
from threading import Event
from workers.contracts import canonical
from workers.contracts import RemoteJobFailed, TransientRemoteError
from workers.connectors.langfuse import progress_events


class ProgressWorker:
    def __init__(self, store, storage, output_bucket, prefix="results", *, langfuse=None,
                 poll_seconds=5, max_watch_seconds=39600, inbox=None):
        self.store, self.storage, self.output_bucket, self.prefix = store, storage, output_bucket, prefix.strip("/")
        self.langfuse, self.poll_seconds, self.max_watch_seconds = langfuse, poll_seconds, max_watch_seconds
        self.stop = Event()
        self.inbox = inbox

    def drain_monitoring(self):
        if self.inbox is None:
            return
        item = self.inbox.next()
        if item is None:
            return
        event_id, event, attempts = item
        try:
            self.store.observe(event)
        except ValueError:
            self.inbox.finish(event_id, "rejected")
            print('{"service":"worker","stage":"monitoring_persist","status":"rejected"}', flush=True)
        except Exception:
            self.inbox.retry(event_id, attempts)
            print('{"service":"worker","stage":"monitoring_persist","status":"retry"}', flush=True)
        else:
            self.inbox.finish(event_id)

    def handle(self, event):
        if event.get("operation") == "watch":
            return self.watch(event)
        if not self.store.check_event(event):
            return
        output = None
        if event["status"] == "completed":
            result = event.get("result")
            if not isinstance(result, dict) or result.get("schema_version") != "2.0.0":
                raise ValueError("Completion requires the workflow's extraction.v2 result")
            partner = event.get("partner")
            if partner:  # ContractGuard decides the key; a redelivered job simply overwrites it (§4.6)
                from workers.partner import extraction
                output = self.storage.write_json(self.output_bucket, partner["result_key"],
                                                 extraction(event, result, partner))
            else:
                identity = hashlib.sha256(canonical([event["job_id"], event["runtime_id"]]).encode()).hexdigest()
                # Attempt-scoped key prevents a stale writer from overwriting a newer attempt's artifact.
                key = f"{self.prefix}/{identity}/{event['owner']}/extraction.json"
                output = self.storage.write_json(self.output_bucket, key, {
                    "job_id": event["job_id"], "runtime_id": event["runtime_id"],
                    "contract_id": event["contract_id"], "result": result})
        self.store.apply(event, output=output)

    def watch(self, command):
        if self.langfuse is None:
            raise ValueError("Langfuse read client is not configured")
        timeout = command["timeout_seconds"]
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= self.max_watch_seconds:
            raise ValueError("Invalid trace watch timeout")
        deadline = time.monotonic() + timeout

        def guard():
            if self.stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Trace result was not available before the worker deadline")
            return self.store.assert_owned(command)

        row = guard()
        if row["status"] == "completed":
            return
        if row.get("trace_id") != command["trace_id"]:
            raise ValueError("Trace is not bound to this job")
        events = JobEvents(SimpleNamespace(job_id=command["job_id"], contract_id=command["contract_id"]),
                           command["runtime_id"], command["owner"], self.handle, guard=guard)
        events.sequence, events.progress = int(row["sequence"]), int(row["progress"])
        seen = 0
        while True:
            guard()
            try:
                rows = self.langfuse.observations(command["trace_id"], command["from_start_time"], guard=guard)
            except TransientRemoteError:
                self.stop.wait(min(self.poll_seconds, max(0, deadline - time.monotonic())))
                continue
            for item in progress_events(rows, command):
                if item["source_sequence"] <= seen:
                    continue
                details = {"source": "langfuse", "observation_id": item["observation_id"],
                           "state": item["state"], "source_sequence": item["source_sequence"]}
                if item["state"] == "failed":
                    events.emit(item["stage"], status="failed", progress=item["progress"], details=details)
                    raise RemoteJobFailed("Remote contract processing failed")
                if item["result"] is not None:
                    events.emit("saving_result", progress=95, details=details)
                    events.emit("completed", status="completed", progress=100, details=details, result=item["result"])
                    return
                events.emit(item["stage"], progress=item["progress"], details=details)
                seen = item["source_sequence"]
            self.stop.wait(min(self.poll_seconds, max(0, deadline - time.monotonic())))

    def serve(self, requests, responses, stop):
        self.stop = stop
        while not stop.is_set():
            try:
                event = requests.get(timeout=0.5)
            except Empty:
                self.drain_monitoring()
                continue
            try:
                self.handle(event)
                response = {"event_id": event["event_id"], "ok": True}
            except Exception as exc:
                response = {"event_id": event["event_id"], "ok": False, "error_type": type(exc).__name__}
            responses.put(response)
            self.drain_monitoring()


class ProgressChannel:
    """One outstanding event per intake process; ack only after durable writes."""
    def __init__(self, requests, responses, timeout=120):
        self.requests, self.responses, self.timeout = requests, responses, timeout

    def send(self, event):
        self.requests.put(event, timeout=self.timeout)
        timeout = self.timeout + event.get("timeout_seconds", 0) if event.get("operation") == "watch" else self.timeout
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Progress worker did not acknowledge persistence")
            try:
                response = self.responses.get(timeout=remaining)
            except Empty as exc:
                raise TimeoutError("Progress worker did not acknowledge persistence") from exc
            if response["event_id"] != event["event_id"]:
                continue  # response from a timed-out earlier attempt
            if not response["ok"]:
                raise RuntimeError("Progress persistence failed: " + response["error_type"])
            return


class JobEvents:
    def __init__(self, job, runtime_id, owner, sender, guard=lambda: None, on_emit=None):
        self.job, self.runtime_id, self.owner, self.sender, self.guard = job, runtime_id, owner, sender, guard
        self.on_emit = on_emit
        self.sequence = 0
        self.progress = 0
        self.stage = "received"

    def emit(self, stage, status="running", progress=0, details=None, result=None):
        self.guard()
        self.sequence += 1
        next_progress = max(self.progress, progress)
        event = {"job_id": self.job.job_id, "contract_id": self.job.contract_id,
                 "runtime_id": self.runtime_id, "owner": self.owner, "sequence": self.sequence,
                 "event_id": uuid4().hex, "stage": stage, "status": status,
                 "progress": next_progress, "details": details or {}, "result": result}
        partner = (getattr(self.job, "info", None) or {}).get("metadata", {}).get("contractguard")
        if partner and status == "completed":
            event["partner"] = partner  # the writer puts the result at ContractGuard's result_key
        self.sender(event)
        if self.on_emit is not None and status == "running":
            self.on_emit(stage)
        # Advance local progress only after persistence is acknowledged. A failed final
        # S3 write must not leave a failed job at 100%.
        self.progress, self.stage = next_progress, stage

    def watch(self, trace_id, from_start_time, timeout_seconds):
        self.guard()
        self.sender({"operation": "watch", "event_id": uuid4().hex,
                     "job_id": self.job.job_id, "contract_id": self.job.contract_id,
                     "runtime_id": self.runtime_id, "owner": self.owner,
                     "trace_id": trace_id, "from_start_time": from_start_time,
                     "timeout_seconds": timeout_seconds})
