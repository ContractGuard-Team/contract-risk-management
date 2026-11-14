"""SQS intake -> S3 -> external processing service -> persisted-result acknowledgement."""
import hashlib
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from workers.connectors.input_processing import dispatch_identity
from workers.connectors.contract_source import ContractSource
from workers.runtime.heartbeat import Heartbeat
from workers.runtime.progress_worker import JobEvents
from workers.schemas import QueueJob


logger = logging.getLogger(__name__)


class IntakeWorker:
    def __init__(self, queue, store, storage, sender, settings, *, processor=None, workflow=None):
        self.queue, self.store, self.storage, self.sender = queue, store, storage, sender
        self.settings = settings
        self.processor = processor
        self.workflow = workflow

    def _run_pipeline(self, job, runtime_id, file, trace_id, events, heartbeat):
        from monitoring.client import emit
        identity = {"job_id": job.job_id, "runtime_id": runtime_id, "contract_id": job.contract_id,
                    "trace_id": trace_id, "request_id": trace_id}
        def progress(stage, percentage, status="running", result=None):
            events.emit(stage, status=status, progress=percentage, result=result,
                        details={"trace_id": trace_id, "request_id": identity["request_id"]})
            emit("worker", stage, status, progress=percentage, **identity)
        try:
            progress("processing", 10)
            contract = self.processor.process(job, file, identity, guard=heartbeat.check)
            progress("processing", 20)
            def observe(row):
                if row["status"] == "running":
                    progress(row["stage"], min(90, max(20, row["progress"])))
            final = self.workflow.execute(contract, identity, guard=heartbeat.check, on_progress=observe)
            progress("saving_result", 95)
            progress("completed", 100, status="completed", result=final)
        except Exception as exc:
            emit("worker", "pipeline", "failed", error_type=type(exc).__name__, **identity)
            raise

    def handle(self, message):
        """Delete SQS only after the writer confirms S3 and DynamoDB completion."""
        if self.processor is None:
            raise ValueError("Input processing HTTP connector is required")
        job = QueueJob.from_body(message["Body"], default_bucket=self.settings.input_bucket)
        row = self.store.reserve(job)
        receipt, runtime_id = message["ReceiptHandle"], row["runtime_id"]
        if row["status"] in ("completed", "superseded"):
            self.queue.delete(receipt)
            return "duplicate_completed" if row["status"] == "completed" else "superseded"
        owner = uuid4().hex
        if self.store.claim(job.job_id, runtime_id, owner, self.settings.lease_seconds) is None:
            return "busy"
        try:
            with Heartbeat(self.queue, self.store, receipt, job.job_id, runtime_id, owner, self.settings) as heartbeat:
                events = JobEvents(job, runtime_id, owner, self.sender, guard=heartbeat.check)
                try:
                    events.emit("downloading", progress=5)
                    file = ContractSource(storage=self.storage).load(job.source)
                    digest = hashlib.sha256(file.data).hexdigest()
                    self.store.bind_input(job.job_id, owner, digest)
                    _, trace_id = dispatch_identity(job.job_id, runtime_id, digest)
                    self.store.bind_trace(job.job_id, owner, trace_id)
                    events.emit("dispatching", progress=10)
                    if self.workflow is not None:
                        self._run_pipeline(job, runtime_id, file, trace_id, events, heartbeat)
                    else:
                        # Compatibility for callers of the previous explicit trace-poll adapter.
                        # The multi-container entrypoint always supplies a WorkflowServiceClient.
                        accepted_trace = self.processor.submit(job, runtime_id, file)
                        if accepted_trace != trace_id:
                            raise ValueError("Processing service acknowledged another trace")
                        events.emit("processing", progress=10, details={"state": "accepted", "trace_id": trace_id})
                        from_time = datetime.fromtimestamp(int(row["created_at"]) - 300, timezone.utc).isoformat()
                        remaining = self.settings.max_job_seconds - (time.monotonic() - heartbeat.started)
                        events.watch(trace_id, from_time, remaining)
                    saved = self.store.assert_owned({"job_id": job.job_id, "runtime_id": runtime_id, "owner": owner})
                    if saved["status"] != "completed" or not saved.get("output"):
                        raise RuntimeError("Writer acknowledged without a persisted final result")
                except Exception as exc:
                    try:
                        current = self.store.assert_owned({"job_id": job.job_id, "runtime_id": runtime_id, "owner": owner})
                        if current["status"] == "completed":
                            raise RuntimeError("Completion already persisted; retain SQS for replay")
                        events.sequence, events.progress, events.stage = int(current["sequence"]), int(current["progress"]), current["stage"]
                        events.emit(events.stage, status="failed", details={"error_type": type(exc).__name__})
                    except Exception:
                        pass  # The original error propagates; the SQS message remains available for retry.
                    raise
            self.queue.delete(receipt)
            return "completed"
        finally:
            self.store.release(job.job_id, owner)

    def serve(self, stop):
        if self.processor is None:
            raise ValueError("Input processing HTTP connector is required before polling SQS")
        while not stop.is_set():
            try:
                for message in self.queue.receive(visibility=self.settings.visibility_seconds):
                    if stop.is_set():
                        break
                    self.handle(message)
            except Exception as exc:
                logger.error("Intake attempt failed (%s); SQS retained", type(exc).__name__)
                stop.wait(1)
