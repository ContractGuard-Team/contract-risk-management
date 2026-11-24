"""SQS intake -> S3 -> external processing service -> persisted-result acknowledgement."""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from common.exceptions import JobCancelled
from common.schemas.business.outbound import error_result
from monitoring.client import emit
from workers.connectors.input_processing import dispatch_identity
from workers.connectors.contract_source import ContractSource
from workers.runtime.heartbeat import Heartbeat
from workers.runtime.progress_worker import JobEvents
from workers.partner import MAX_RECEIVES, is_input_error, safe_reason
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

    def _run_pipeline(self, job, identity, file, events, heartbeat):
        def progress(stage, percentage, status="running", result=None):
            events.emit(stage, status=status, progress=percentage, result=result)
            emit("worker", stage, status, progress=percentage, **identity)
        progress("processing", 10)
        contract = self.processor.process(job, file, identity, guard=heartbeat.check)
        info = job.info.get("metadata", {}).get("contractguard")
        max_pages = ((info or {}).get("limits") or {}).get("max_pages")
        document = ((contract.get("context") or {}).get("preprocessing", {}).get("document") or {})
        pages = document.get("page_count")
        if max_pages and isinstance(pages, int) and pages > max_pages:
            from workers.partner import ERR_TOO_MANY_PAGES, input_error
            raise input_error(f"Document has {pages} pages; the request allows {max_pages}", ERR_TOO_MANY_PAGES)
        declared = (info or {}).get("declared_file_type")
        detected = str(document.get("format") or "")
        if declared and {"jpeg": "jpg"}.get(detected, detected) != declared:  # FR-UPL-003: real type, not extension
            raise SourceError(f"File content is {detected or 'unknown'}, request declared {declared}")
        if self.partner is not None and info:  # input-processing accepted the file: store the checked copy
            self.partner.store_clean(info, file)
        progress("langgraph", 20)
        def observe(row):
            if row["status"] == "running":
                # "queued"/"retry" of the LangGraph job queue are not places: the contract is at langgraph.
                stage = row["stage"] if row["stage"].startswith("agent_") else "langgraph"
                progress(stage, min(90, max(20, row["progress"])))
        final = self.workflow.execute(contract, identity, guard=heartbeat.check, on_progress=observe)
        progress("saving_result", 95)
        progress("completed", 100, status="completed", result=final)

    def handle(self, message):
        """One message to its end: completed, failed for good or cancelled (logged, answered, SQS deleted), or an
        exception (SQS redelivers it, at most MAX_RECEIVES times)."""
        if self.processor is None:
            raise ValueError("Input processing HTTP connector is required")
        job_id = job = None
        try:
            # Known before the request is read, so even an invalid request can be reported as failed.
            job_id = json.loads(message["Body"]).get("job_id") or None
            body, info = message["Body"], None
            if self.partner is not None:
                kind, body = self.partner.read(message["Body"])
                if kind == "ping":
                    self.partner.send("pong", job_id)
                    self.queue.delete(message["ReceiptHandle"])
                    return "pong"
                info = body["metadata"]["contractguard"]
            job = QueueJob.from_body(body, default_bucket=self.settings.input_bucket)
            return self._handle(message, job, info)
        except Exception as exc:
            if not isinstance(job_id, str):
                raise
            if isinstance(exc, JobCancelled) or self.store.cancelled(job_id):
                return self._cancelled(message, job_id)
            if not self._final(message, exc):
                logger.error("Job %s attempt %d failed (%s); SQS will redeliver",
                             job_id, self._attempts(message), type(exc).__name__)
                raise
            if self.store.completed(job_id):
                # The result is stored: never report a failure; a redelivery re-sends `completed`.
                logger.error("Job %s completed but not acknowledged (%s); SQS will redeliver", job_id, type(exc).__name__)
                raise
            return self._failed(message, job_id, job, exc)

    @staticmethod
    def _attempts(message):
        return int((message.get("Attributes") or {}).get("ApproximateReceiveCount", "1"))

    def _final(self, message, exc):
        """Bad input never succeeds; anything else is retried by SQS until the receive limit."""
        return is_input_error(exc) or self._attempts(message) >= MAX_RECEIVES

    def _details(self, message, exc):
        return {"error_type": type(exc).__name__, "reason": safe_reason(exc), "attempts": self._attempts(message)}

    def _failed(self, message, job_id, job, exc):
        logger.error("Job %s failed (%s); logged, reported and removed", job_id, type(exc).__name__)
        try:  # usually logged already, under the lease; this covers a request rejected before its run
            self.store.record_failure(job_id, job.contract_id if job else None, self._details(message, exc))
        except Exception as log_error:  # the backend must still learn that the job failed
            logger.error("Job %s failure log not written: %s", job_id, type(log_error).__name__)
        if job is None:  # no run, so no run event reached monitoring
            emit("worker", "pipeline", "failed", job_id=job_id, error_type=type(exc).__name__,
                 trace_id=dispatch_identity(job_id, "", "")[1])
        if self.partner is not None:
            self.partner.failed(job_id, exc)
        self.queue.delete(message["ReceiptHandle"])
        return "failed"

    def _cancelled(self, message, job_id):
        logger.info("Job %s cancelled; logged, reported and removed", job_id)
        self.store.mark_cancelled(job_id)  # no-op when the run already wrote it under its lease
        if self.partner is not None:
            self.partner.cancelled(job_id)
        self.queue.delete(message["ReceiptHandle"])
        return "cancelled"

    def _finish_partner(self, job, info):
        """§4: completed -> remove the quarantined upload; the caller deletes the SQS message last."""
        self.partner.send("completed", job.job_id, result_key=info["result_key"])
        self.partner.remove_quarantine(info)

    def _handle(self, message, job, info):
        row = self.store.reserve(job)
        receipt, runtime_id = message["ReceiptHandle"], row["runtime_id"]
        if row["status"] == "cancelled":  # cancelled before it started, or before a crash
            raise JobCancelled("Cancelled by user")
        if row["status"] == "completed":
            if info:  # redelivered after completion: answer again, no recompute
                self._finish_partner(job, info)
            self.queue.delete(receipt)
            return "duplicate_completed"
        owner = uuid4().hex
        if self.store.claim(job.job_id, runtime_id, owner, self.settings.lease_seconds) is None:
            return "busy"
        try:
            partner = self.partner if info else None
            with Heartbeat(self.queue, self.store, receipt, job.job_id, runtime_id, owner, self.settings,
                           on_tick=partner and (lambda: partner.keepalive(job.job_id))) as heartbeat:
                events = JobEvents(job, runtime_id, owner, self.sender, guard=heartbeat.check,
                                   on_emit=partner and (lambda stage: partner.progress(job.job_id, stage)))
                # Until the content hash is known, telemetry of this run uses a trace derived from its ids.
                trace_id = dispatch_identity(job.job_id, runtime_id, "")[1]
                identity = {"job_id": job.job_id, "runtime_id": runtime_id, "contract_id": job.contract_id,
                            "trace_id": trace_id, "request_id": trace_id}
                try:
                    events.emit("downloading", progress=5)
                    limit = ((info or {}).get("limits") or {}).get("max_bytes")
                    source = ContractSource(storage=self.storage)
                    if limit:  # the request may only tighten the worker's own limit
                        source = ContractSource(storage=self.storage, max_file_bytes=min(limit, source.max_file_bytes))
                    file = source.load(job.source)
                    digest = hashlib.sha256(file.data).hexdigest()
                    self.store.bind_input(job.job_id, owner, digest)
                    _, trace_id = dispatch_identity(job.job_id, runtime_id, digest)
                    identity.update(trace_id=trace_id, request_id=trace_id)
                    self.store.bind_trace(job.job_id, owner, trace_id)
                    events.emit("dispatching", progress=10)
                    emit("worker", "dispatching", "running", progress=10, **identity)
                    if self.workflow is not None:
                        self._run_pipeline(job, identity, file, events, heartbeat)
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
                    cancelled = isinstance(exc, JobCancelled) or self.store.cancelled(job.job_id)
                    status = "cancelled" if cancelled else "failed" if self._final(message, exc) else "retrying"
                    emit("worker", "pipeline", status, **identity, **({} if cancelled else {"error_type": type(exc).__name__}))
                    try:
                        current = self.store.assert_owned({"job_id": job.job_id, "runtime_id": runtime_id, "owner": owner})
                        if current["status"] == "completed":
                            raise RuntimeError("Completion already persisted; retain SQS for replay")
                        events.sequence, events.progress, events.stage = int(current["sequence"]), int(current["progress"]), current["stage"]
                        events.guard = lambda: None  # the guard is what stopped the job: the log must still be written
                        events.emit(events.stage, status=status,
                                    details={"reason": "Cancelled by user"} if cancelled else self._details(message, exc),
                                    result=error_result(job.job_id, job.contract_id, job.contract_version)
                                    if status == "failed" else None)
                    except Exception as log_error:  # the original error propagates; handle() closes what is missing
                        logger.warning("Job %s: %s not written under its lease (%s)", job.job_id, status, type(log_error).__name__)
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
