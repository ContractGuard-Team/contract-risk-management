"""SQS intake -> S3 -> external processing service -> persisted-result acknowledgement."""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from workers.connectors.input_processing import dispatch_identity
from workers.connectors.contract_source import ContractSource
from workers.runtime.heartbeat import Heartbeat
from workers.runtime.progress_worker import JobEvents
from workers.partner import MAX_RECEIVES, is_input_error
from workers.contracts import SourceError
from workers.schemas import QueueJob


logger = logging.getLogger(__name__)


class IntakeWorker:
    def __init__(self, queue, store, storage, sender, settings, *, processor=None, workflow=None, partner=None):
        self.queue, self.store, self.storage, self.sender = queue, store, storage, sender
        self.settings = settings
        self.partner = partner  # workers.partner.PartnerProtocol when WORKER_RESULTS_QUEUE_URL is set
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
            info = job.info.get("metadata", {}).get("contractguard")
            max_pages = ((info or {}).get("limits") or {}).get("max_pages")
            pages = ((contract.get("context") or {}).get("preprocessing", {}).get("document") or {}).get("page_count")
            if max_pages and isinstance(pages, int) and pages > max_pages:
                raise SourceError(f"Document has {pages} pages; the request allows {max_pages}")
            if self.partner is not None and info:  # input-processing accepted the file: store the checked copy
                self.partner.store_clean(info, file)
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
        if self.partner is None:
            return self._handle(message, message["Body"], None)
        job_id = None
        try:
            # Known before request.json is read, so even an invalid request can be reported as failed.
            job_id = json.loads(message["Body"]).get("job_id") or None
            kind, value = self.partner.read(message["Body"])
            if kind == "ping":
                self.partner.send("pong", job_id)
                self.queue.delete(message["ReceiptHandle"])
                return "pong"
            return self._handle(message, value, value["metadata"]["contractguard"])
        except Exception as exc:
            attempts = int((message.get("Attributes") or {}).get("ApproximateReceiveCount", "1"))
            # Bad input never succeeds; anything else is retried by SQS until the receive limit.
            if job_id is None or (not is_input_error(exc) and attempts < MAX_RECEIVES):
                logger.error("ContractGuard job %s attempt %d failed (%s); SQS will redeliver",
                             job_id, attempts, type(exc).__name__)
                raise
            row = self.store.client.get(job_id) if isinstance(job_id, str) else None
            if row and row.get("status") == "completed":
                # The result is stored: never send `failed`; a redelivery re-sends `completed`.
                logger.error("ContractGuard job %s completed but not acknowledged (%s); SQS will redeliver",
                             job_id, type(exc).__name__)
                raise
            logger.error("ContractGuard job %s failed (%s); reported and removed", job_id, type(exc).__name__)
            self.partner.failed(job_id, exc)
            self.queue.delete(message["ReceiptHandle"])
            return "failed"

    def _finish_partner(self, job, info):
        """§4: completed -> remove the quarantined upload; the caller deletes the SQS message last."""
        self.partner.send("completed", job.job_id, result_key=info["result_key"])
        self.partner.remove_quarantine(info)

    def _handle(self, message, body, info):
        job = QueueJob.from_body(body, default_bucket=self.settings.input_bucket)
        row = self.store.reserve(job)
        receipt, runtime_id = message["ReceiptHandle"], row["runtime_id"]
        if row["status"] in ("completed", "superseded"):
            if info and row["status"] == "completed":  # redelivered after completion: answer again, no recompute
                self._finish_partner(job, info)
            self.queue.delete(receipt)
            return "duplicate_completed" if row["status"] == "completed" else "superseded"
        owner = uuid4().hex
        if self.store.claim(job.job_id, runtime_id, owner, self.settings.lease_seconds) is None:
            return "busy"
        try:
            partner = self.partner if info else None
            with Heartbeat(self.queue, self.store, receipt, job.job_id, runtime_id, owner, self.settings,
                           on_tick=partner and (lambda: partner.keepalive(job.job_id))) as heartbeat:
                events = JobEvents(job, runtime_id, owner, self.sender, guard=heartbeat.check,
                                   on_emit=partner and (lambda stage: partner.progress(job.job_id, stage)))
                try:
                    events.emit("downloading", progress=5)
                    limit = ((info or {}).get("limits") or {}).get("max_file_bytes")
                    source = ContractSource(storage=self.storage)
                    if limit:  # the request may only tighten the worker's own limit
                        source = ContractSource(storage=self.storage, max_file_bytes=min(limit, source.max_file_bytes))
                    file = source.load(job.source)
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
                    if partner:
                        self._finish_partner(job, info)
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
