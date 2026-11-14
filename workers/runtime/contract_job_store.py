"""Current progress per contract plus immutable per-job event history.

jobs PK = contract:<contract_id>; active_job_id is the SQS job_id.
events PK = actual job_id, SK = registered / completed / worker:<id> / monitoring:<id>.
No event is updated or deleted. Transactions bind journal writes to state changes.
"""
import time
from uuid import uuid4

from botocore.exceptions import ClientError

from workers.runtime.job_store import JobStore, LeaseLost


def conflict(exc):
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code")
    if code == "ConditionalCheckFailedException":
        return True
    reasons = exc.response.get("CancellationReasons", [])
    return code == "TransactionCanceledException" and any(
        reason.get("Code") == "ConditionalCheckFailed" for reason in reasons)


class ContractBusy(RuntimeError):
    pass


class CurrentClient:
    """Adapt the existing lease protocol to the contract-keyed current projection."""
    def __init__(self, tables):
        self.tables = tables

    @staticmethod
    def key(contract_id):
        return "contract:" + contract_id

    def locate(self, job_id):
        receipt = self.tables.event(job_id, "registered")
        if not receipt:
            raise LeaseLost("Unknown job registration")
        return {"job_id": self.key(receipt["contract_id"])}

    def physical(self, contract_id):
        return self.tables.get(self.tables.jobs_table, {"job_id": self.key(contract_id)})

    @staticmethod
    def logical(row):
        return {**row, "job_id": row["active_job_id"]}

    def get(self, job_id):
        row = self.tables.get(self.tables.jobs_table, self.locate(job_id))
        return self.logical(row) if row and row.get("active_job_id") == job_id else None

    def operation(self, job_id, *, expression, condition, names, values):
        return {"Update": {"TableName": self.tables.jobs_table, "Key": self.locate(job_id),
            "UpdateExpression": expression, "ConditionExpression": "(" + condition + ") AND active_job_id=:active_job",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": {**values, ":active_job": job_id}}}

    def update(self, job_id, *, expression, condition, names, values):
        operation = self.operation(job_id, expression=expression, condition=condition, names=names, values=values)["Update"]
        wire = dict(operation)
        for field in ("Key", "ExpressionAttributeValues"):
            wire[field] = self.tables.encode(wire[field])
        response = self.tables.client.update_item(**wire, ReturnValues="ALL_NEW")
        return self.logical(self.tables.decode(response["Attributes"]))


class ContractJobStore(JobStore):
    def __init__(self, tables, *, clock=time.time, lease_seconds=180):
        super().__init__(CurrentClient(tables), clock=clock)
        self.tables, self.lease_seconds = tables, lease_seconds

    def reserve(self, job):
        for _ in range(4):
            receipt = self.tables.event(job.job_id, "registered")
            if receipt:
                if receipt["fingerprint"] != job.fingerprint or receipt["contract_id"] != job.contract_id:
                    raise ValueError("job_id already exists with different input; submit a new job_id")
                if job.runtime_id is not None and receipt["runtime_id"] != job.runtime_id:
                    raise ValueError("job_id already belongs to another runtime_id")
                completed = self.tables.event(job.job_id, "completed")
                if completed:
                    return {**receipt, "status": "completed", "output": completed["output"]}
                current = self.client.get(job.job_id)
                return current if current else {**receipt, "status": "superseded"}
            now = int(self.clock())
            previous = self.client.physical(job.contract_id)
            if previous and previous.get("status") not in ("completed", "failed") and previous.get("lease_until", 0) >= now:
                raise ContractBusy("Another run of this contract still owns its lease")
            runtime = job.runtime_id or uuid4().hex
            row = {"job_id": self.client.key(job.contract_id), "active_job_id": job.job_id,
                   "contract_id": job.contract_id, "runtime_id": runtime, "fingerprint": job.fingerprint,
                   "job_info": job.info, "status": "queued", "stage": "received", "progress": 0,
                   "sequence": 0, "created_at": now, "updated_at": now, "lease_until": now + self.lease_seconds,
                   "run_started_ns": int(self.clock() * 1_000_000_000)}
            receipt = {"job_id": job.job_id, "event_id": "registered", "record_type": "job_registered",
                       "contract_id": job.contract_id, "runtime_id": runtime, "fingerprint": job.fingerprint,
                       "created_at": now, "status": "queued", "stage": "received", "progress": 0}
            put = {"TableName": self.tables.jobs_table, "Item": row,
                   "ConditionExpression": "attribute_not_exists(job_id)"}
            if previous:
                put.update(ConditionExpression="active_job_id=:previous AND (#s IN (:done,:failed) OR lease_until<:now)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":previous": previous["active_job_id"], ":done": "completed",
                                               ":failed": "failed", ":now": now})
            try:
                self.tables.transact([{"Put": put}, self.tables.event_put(receipt)])
                return self.client.logical(row)
            except ClientError as exc:
                if not conflict(exc):
                    raise
        raise ContractBusy("Contract registration changed concurrently; retry the queue message")

    def claim(self, job_id, runtime_id, owner, lease_seconds):
        row = super().claim(job_id, runtime_id, owner, lease_seconds)
        if row:
            # Fence late monitoring from a previous retry of this same run.
            return self.client.update(job_id, expression="SET attempt_started_ns=:start",
                condition="#o=:owner AND #r=:runtime", names={"#o": "owner", "#r": "runtime_id"},
                values={":start": int(self.clock() * 1_000_000_000), ":owner": owner, ":runtime": runtime_id})
        return None

    def check_event(self, event):
        if self.tables.event(event["job_id"], "worker:" + event["event_id"]):
            return False
        return super().check_event(event)

    def bind_trace(self, job_id, owner, trace_id):
        super().bind_trace(job_id, owner, trace_id)
        row = self.client.get(job_id)
        record = {"job_id": job_id, "event_id": "trace", "record_type": "trace_bound",
                  "contract_id": row["contract_id"], "runtime_id": row["runtime_id"], "trace_id": trace_id,
                  "input_sha256": row["input_sha256"], "created_at": int(self.clock())}
        try:
            self.tables.transact([self.tables.event_put(record)])
        except ClientError as exc:
            if not conflict(exc) or self.tables.event(job_id, "trace").get("trace_id") != trace_id:
                raise

    def apply(self, event, output=None):
        for attempt in range(4):
            try:
                return self._apply_once(event, output)
            except LeaseLost:
                if attempt == 3:
                    raise
                self.assert_owned(event)

    def _apply_once(self, event, output):
        if not self.check_event(event):
            return
        current = self.assert_owned(event)
        percentage = max(event["progress"], current["progress"])
        stage = current["stage"] if event["status"] == "running" and event["progress"] < current["progress"] else event["stage"]
        fields = {"status": event["status"], "stage": stage, "progress": percentage,
                  "sequence": event["sequence"], "last_event_id": event["event_id"],
                  "details": event.get("details", {}), "updated_at": int(self.clock())}
        if output is not None:
            fields["output"] = output
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":f{i}": value for i, value in enumerate(fields.values())}
        names.update({"#o": "owner", "#r": "runtime_id", "#q": "sequence", "#s": "status", "#l": "lease_until"})
        values.update({":owner": event["owner"], ":runtime": event["runtime_id"], ":seq": event["sequence"],
                       ":done": "completed", ":now": int(self.clock()), ":percentage": percentage})
        update = self.client.operation(event["job_id"], expression="SET " + ", ".join(f"#f{i}=:f{i}" for i in range(len(fields))),
            condition="#o=:owner AND #r=:runtime AND #q<:seq AND #s<>:done AND #l>=:now AND progress<=:percentage", names=names, values=values)
        log = {key: event[key] for key in ("job_id", "contract_id", "runtime_id", "stage", "status", "progress", "sequence")}
        log.update(event_id="worker:" + event["event_id"], record_type="worker_progress",
                   owner=event["owner"], created_at=int(self.clock()), details=event.get("details", {}))
        if output is not None:
            log["output"] = output
        operations = [update, self.tables.event_put(log)]
        if event["status"] == "completed":
            operations.append(self.tables.event_put({**log, "event_id": "completed", "record_type": "completion_receipt"}))
        try:
            self.tables.transact(operations)
        except ClientError as exc:
            if conflict(exc):
                if self.tables.event(event["job_id"], log["event_id"]):
                    return
                raise LeaseLost("Progress transaction lost ownership or ordering") from exc
            raise

    def observe(self, event):
        """Append monitoring once; project only the active run, never finalize a job."""
        receipt = self.tables.event(event["job_id"], "registered")
        if not receipt or any(receipt.get(k) != event[k] for k in ("contract_id", "runtime_id")):
            raise ValueError("Monitoring event is not bound to a registered run")
        bound = self.tables.event(event["job_id"], "trace")
        if not bound or bound["trace_id"] != event["trace_id"]:
            raise ValueError("Monitoring trace is not bound to this run")
        event_id = "monitoring:" + event["event_id"]
        log = {**event, "event_id": event_id, "observation_id": event["event_id"], "record_type": "monitoring_progress"}
        for _ in range(4):
            if self.tables.event(event["job_id"], event_id):
                return
            row = self.client.get(event["job_id"])
            operations = [self.tables.event_put(log)]
            projection = progress_view(event)
            if (row and projection and row.get("status") == "running" and row.get("trace_id") == event["trace_id"]
                    and row.get("runtime_id") == event["runtime_id"] and row.get("lease_until", 0) >= int(self.clock())
                    and event["timestamp_ns"] >= row.get("attempt_started_ns", row["run_started_ns"])
                    and event["timestamp_ns"] > row.get("monitoring_timestamp_ns", 0)
                    and projection[1] >= row.get("progress", 0)):
                stage, percentage = projection
                operations.append(self.client.operation(event["job_id"],
                    expression="SET stage=:stage, progress=:progress, monitoring_timestamp_ns=:ts, last_observation_id=:id, observed_status=:observed, updated_at=:now",
                    condition="#r=:runtime AND #o=:owner AND #s=:running AND #l>=:now AND progress<=:progress AND (attribute_not_exists(monitoring_timestamp_ns) OR monitoring_timestamp_ns<:ts)",
                    names={"#r": "runtime_id", "#o": "owner", "#s": "status", "#l": "lease_until"},
                    values={":runtime": event["runtime_id"], ":owner": row["owner"], ":running": "running",
                            ":now": int(self.clock()), ":stage": stage, ":progress": percentage,
                            ":ts": event["timestamp_ns"], ":id": event["event_id"], ":observed": event["status"]}))
            try:
                self.tables.transact(operations)
                return
            except ClientError as exc:
                if not conflict(exc):
                    raise
        raise RuntimeError("Concurrent progress updates; retry monitoring delivery")


def progress_view(event):
    service, stage, done = event["service"], event["stage"], event["status"] == "completed"
    if service == "input-processing":
        return "processing", 20 if done else 10
    for index, start, end in ((1, 30, 50), (2, 60, 75), (3, 85, 90)):
        if service == f"agent-{index}" or (service == "langgraph" and stage.startswith(f"agent_{index}")):
            return f"agent_{index}", end if done else start
    if service == "worker" and "progress" in event:
        return stage, min(95, int(event["progress"]))
    return None
