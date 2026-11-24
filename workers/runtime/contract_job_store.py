"""Where each contract is now (jobs table) and how each job ended (events table).

jobs   PK job_id = contract:<contract_id>. One row per contract, overwritten at every hop: `location` (worker,
       input_processing, langgraph, agent_1..3, then worker again) and `status` (queued, running, retrying, completed,
       failed, cancelled). The last state stays until another job of the same contract replaces the whole row.
events PK job_id (the SQS job_id), SK event_id = "outcome". Written only when a job completed, failed for good or was
       cancelled: at most one log per job, nothing while it runs. A failed log is replaced if the same job_id runs again.
The outcome row decides races: completed and cancelled each refuse to overwrite the other.
"""
import time
from uuid import uuid4

from botocore.exceptions import ClientError
from common.exceptions import JobCancelled
from workers.connectors.dynamodb_tables import write
from workers.contracts import SourceError

from workers.runtime.job_store import JobStore, LeaseLost

OUTCOME = "outcome"
FINAL = ("completed", "failed", "cancelled")
WORKER_STAGES = ("received", "downloading", "dispatching", "saving_result", "completed")


def location(stage):
    if stage in WORKER_STAGES:
        return "worker"
    if stage == "processing":
        return "input_processing"
    return stage if stage.startswith("agent_") else "langgraph"


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
    """Adapt the existing lease protocol to the contract-keyed current row."""
    def __init__(self, tables):
        self.tables = tables
        self.contracts = {}  # job_id -> contract_id of the jobs this process handles

    @staticmethod
    def key(contract_id):
        return "contract:" + contract_id

    def locate(self, job_id):
        if job_id not in self.contracts:
            raise LeaseLost("Unknown job registration")
        return {"job_id": self.key(self.contracts[job_id])}

    def physical(self, contract_id):
        return self.tables.get(self.tables.jobs_table, {"job_id": self.key(contract_id)})

    @staticmethod
    def logical(row):
        return {**row, "job_id": row["active_job_id"]}

    def get(self, job_id):
        if job_id not in self.contracts:
            return None  # never registered (e.g. rejected before reserve): no row, like any unknown job
        row = self.physical(self.contracts[job_id])
        return self.logical(row) if row and row.get("active_job_id") == job_id else None

    def operation(self, job_id, *, expression, condition, names, values):
        return {"Update": {"TableName": self.tables.jobs_table, "Key": self.locate(job_id),
            "UpdateExpression": expression, "ConditionExpression": "(" + condition + ") AND active_job_id=:active_job",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": {**values, ":active_job": job_id}}}

    def update(self, job_id, *, expression, condition, names, values):
        wire = self.operation(job_id, expression=expression, condition=condition, names=names, values=values)["Update"]
        for field in ("Key", "ExpressionAttributeValues"):
            wire[field] = self.tables.encode(wire[field])
        response = write(lambda: self.tables.client.update_item(**wire, ReturnValues="ALL_NEW"))
        return self.logical(self.tables.decode(response["Attributes"]))


class ContractJobStore(JobStore):
    def __init__(self, tables, *, clock=time.time, lease_seconds=180):
        super().__init__(CurrentClient(tables), clock=clock)
        self.tables, self.lease_seconds = tables, lease_seconds

    # --- outcome log -----------------------------------------------------------------------------------
    def outcome(self, job_id):
        row = self.tables.event(job_id, OUTCOME)
        if row is None:  # receipts written before 2026-11-24
            row = self.tables.event(job_id, "completed")
        return row

    def outcome_put(self, record):
        replaceable = (":failed", ":cancelled") if record["status"] == "cancelled" else (":failed",)
        return {"Put": {"TableName": self.tables.events_table, "Item": {**record, "event_id": OUTCOME},
                        "ConditionExpression": f"attribute_not_exists(event_id) OR #s IN ({','.join(replaceable)})",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {name: name[1:] for name in replaceable}}}

    def completed(self, job_id):
        return (self.outcome(job_id) or {}).get("status") == "completed"

    def cancelled(self, job_id):
        return (self.tables.event(job_id, OUTCOME) or {}).get("status") == "cancelled"

    def cancel(self, job_id, reason="Cancelled by user"):
        """Cancel a job, running or not yet received. Returns what it is now: cancelled | completed | failed.
        The running worker sees the log within a second, stops, and adds where the contract was."""
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 256:
            raise ValueError("job_id is required (1..256 characters)")
        for _ in range(2):
            done = self.outcome(job_id)
            if done and done["status"] == "failed":  # only a job that is running again can still be cancelled
                row = self.client.physical(done["contract_id"]) if done.get("contract_id") else None
                if not row or row.get("active_job_id") != job_id or row["status"] in FINAL:
                    return "failed"
            elif done:
                return done["status"]
            try:
                self.tables.transact([self.outcome_put({"job_id": job_id, "status": "cancelled", "reason": reason,
                                                        "created_at": int(self.clock())})])
                return "cancelled"
            except ClientError as exc:
                if not conflict(exc):
                    raise
        return self.outcome(job_id)["status"]

    def mark_cancelled(self, job_id):
        """The contract row of a cancelled job that no worker is running (cancelled before start, or after a crash)."""
        try:
            self._close_row(job_id, "cancelled", {"reason": "Cancelled by user"})
        except ClientError as exc:
            if not conflict(exc):
                raise
        except LeaseLost:
            pass

    def _close_row(self, job_id, status, details):
        now = int(self.clock())
        self.client.update(job_id, expression="SET #s=:status, details=:details, updated_at=:now",
            condition="lease_until<:now AND NOT #s IN (:completed,:failed,:cancelled)", names={"#s": "status"},
            values={":status": status, ":details": details, ":now": now, ":completed": "completed",
                    ":failed": "failed", ":cancelled": "cancelled"})

    def record_failure(self, job_id, contract_id, details):
        """Final failure outside a lease (request rejected before the run, or the in-run log could not be written)."""
        if self.outcome(job_id):
            return  # already logged
        row = self.client.physical(contract_id) if contract_id else None
        if row and row.get("active_job_id") == job_id:
            if row.get("lease_until", 0) >= int(self.clock()):
                return  # another worker is running this job and will log its own outcome
            try:
                self._close_row(job_id, "failed", details)
            except ClientError as exc:
                if not conflict(exc):
                    raise
        record = {"job_id": job_id, "status": "failed", "created_at": int(self.clock()), **details}
        if contract_id:
            record["contract_id"] = contract_id
        try:
            self.tables.transact([self.outcome_put(record)])
        except ClientError as exc:
            if not conflict(exc):  # completed or cancelled meanwhile: that log stands
                raise

    # --- current row -----------------------------------------------------------------------------------
    def reserve(self, job):
        self.client.contracts[job.job_id] = job.contract_id
        for _ in range(4):
            done = self.outcome(job.job_id)
            previous = self.client.physical(job.contract_id)
            current = previous if previous and previous.get("active_job_id") == job.job_id else None
            for known in (current, done):
                if known and (known.get("contract_id", job.contract_id) != job.contract_id
                              or known.get("fingerprint", job.fingerprint) != job.fingerprint):
                    raise SourceError("job_id already exists with different input; submit a new job_id")
                if known and job.runtime_id is not None and known.get("runtime_id", job.runtime_id) != job.runtime_id:
                    raise SourceError("job_id already belongs to another runtime_id")
            if done and done["status"] in ("completed", "cancelled"):
                return {"runtime_id": "", **done}
            if current:
                return self.client.logical(current)
            now = int(self.clock())
            if previous and previous.get("status") not in FINAL and previous.get("lease_until", 0) >= now:
                raise ContractBusy("Another run of this contract still owns its lease")
            row = {"job_id": self.client.key(job.contract_id), "active_job_id": job.job_id,
                   "contract_id": job.contract_id, "contract_version": job.contract_version,
                   "runtime_id": job.runtime_id or uuid4().hex, "fingerprint": job.fingerprint,
                   "status": "queued", "location": "worker", "stage": "received", "progress": 0,
                   "sequence": 0, "created_at": now, "updated_at": now, "lease_until": now + self.lease_seconds}
            put = {"TableName": self.tables.jobs_table, "Item": row,
                   "ConditionExpression": "attribute_not_exists(job_id)"}
            if previous:  # a new job of the same contract resets the whole row
                put.update(ConditionExpression="active_job_id=:previous AND (#s IN (:done,:failed,:cancelled) OR lease_until<:now)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":previous": previous["active_job_id"], ":done": "completed",
                                               ":failed": "failed", ":cancelled": "cancelled", ":now": now})
            try:
                self.tables.transact([{"Put": put}])
                return self.client.logical(row)
            except ClientError as exc:
                if not conflict(exc):
                    raise
        raise ContractBusy("Contract registration changed concurrently; retry the queue message")

    def claim(self, job_id, runtime_id, owner, lease_seconds):
        if not super().claim(job_id, runtime_id, owner, lease_seconds):
            return None
        # Nothing of an earlier attempt stays visible on the row.
        return self.client.update(job_id, expression="SET #where=:worker REMOVE details, #out, last_event_id",
            condition="#o=:owner", names={"#where": "location", "#out": "output", "#o": "owner"},
            values={":worker": "worker", ":owner": owner})

    def assert_owned(self, identity):
        if identity.get("contract_id"):  # the progress process learns the contract from its events
            self.client.contracts[identity["job_id"]] = identity["contract_id"]
        return super().assert_owned(identity)

    def check_event(self, event):
        self.client.contracts[event["job_id"]] = event["contract_id"]
        return super().check_event(event)

    def apply(self, event, output=None):
        if not self.check_event(event):
            return
        now, where = int(self.clock()), location(event["stage"])
        fields = {"status": event["status"], "location": where, "stage": event["stage"], "progress": event["progress"],
                  "sequence": event["sequence"], "last_event_id": event["event_id"],
                  "details": event.get("details", {}), "updated_at": now}
        if output is not None:
            fields["output"] = output
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":f{i}": value for i, value in enumerate(fields.values())}
        names.update({"#o": "owner", "#r": "runtime_id", "#q": "sequence", "#s": "status", "#l": "lease_until"})
        values.update({":owner": event["owner"], ":runtime": event["runtime_id"], ":seq": event["sequence"],
                       ":done": "completed", ":now": now})
        update = {"expression": "SET " + ", ".join(f"#f{i}=:f{i}" for i in range(len(fields))),
                  "condition": "#o=:owner AND #r=:runtime AND #q<:seq AND #s<>:done AND #l>=:now",
                  "names": names, "values": values}
        try:
            if event["status"] not in FINAL:
                self.client.update(event["job_id"], **update)
                return
            row = self.client.get(event["job_id"])
            record = {key: row[key] for key in ("contract_id", "contract_version", "runtime_id", "fingerprint", "trace_id")
                      if key in row}
            record.update(job_id=event["job_id"], status=event["status"], location=where, stage=event["stage"],
                          progress=event["progress"], created_at=now, **event.get("details", {}))
            if output is not None:
                record["output"] = output
            self.tables.transact([self.client.operation(event["job_id"], **update), self.outcome_put(record)])
        except ClientError as exc:
            if not conflict(exc):
                raise
            if event["status"] != "cancelled" and self.cancelled(event["job_id"]):
                raise JobCancelled("Cancelled by user") from exc
            raise LeaseLost("Progress update lost ownership or ordering") from exc
