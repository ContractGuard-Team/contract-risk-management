"""Job identity, lease ownership and ordered overwrites of the current progress row."""
import time
from uuid import uuid4
from botocore.exceptions import ClientError


def conditional_failure(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class LeaseLost(RuntimeError):
    pass


class JobStore:
    def __init__(self, client, *, clock=time.time):
        self.client, self.clock = client, clock

    def reserve(self, job):
        item = {"job_id": job.job_id, "runtime_id": job.runtime_id or uuid4().hex,
                "fingerprint": job.fingerprint, "job_info": job.info, "status": "queued",
                "stage": "received", "progress": 0, "sequence": 0, "created_at": int(self.clock())}
        try:
            self.client.put(item)
            return item
        except ClientError as exc:
            if not conditional_failure(exc):
                raise
        existing = self.client.get(job.job_id)
        if not existing or existing["fingerprint"] != job.fingerprint:
            raise ValueError("job_id already exists with different input; submit a new job_id")
        if job.runtime_id is not None and existing["runtime_id"] != job.runtime_id:
            raise ValueError("job_id already belongs to another runtime_id")
        return existing

    def claim(self, job_id, runtime_id, owner, lease_seconds):
        now = int(self.clock())
        try:
            return self.client.update(job_id,
                expression="SET #o=:owner, #l=:lease, #s=:running, #q=:zero, stage=:stage, progress=:zero, updated_at=:now ADD attempts :one",
                condition="#r=:runtime AND #s<>:done AND (attribute_not_exists(#o) OR #l<:now)",
                names={"#o": "owner", "#l": "lease_until", "#s": "status", "#q": "sequence", "#r": "runtime_id"},
                values={":owner": owner, ":lease": now + lease_seconds, ":running": "running", ":zero": 0,
                        ":runtime": runtime_id, ":done": "completed", ":stage": "received", ":now": now, ":one": 1})
        except ClientError as exc:
            if conditional_failure(exc):
                return None
            raise

    def renew(self, job_id, runtime_id, owner, lease_seconds):
        now = int(self.clock())
        try:
            self.client.update(job_id, expression="SET #l=:lease",
                condition="#r=:runtime AND #o=:owner AND #l>=:now",
                names={"#r": "runtime_id", "#o": "owner", "#l": "lease_until"},
                values={":runtime": runtime_id, ":owner": owner, ":lease": now + lease_seconds, ":now": now})
        except ClientError as exc:
            if conditional_failure(exc):
                raise LeaseLost("Job lease is no longer owned") from exc
            raise

    def release(self, job_id, owner):
        try:
            self.client.update(job_id, expression="SET #l=:zero", condition="#o=:owner",
                               names={"#l": "lease_until", "#o": "owner"}, values={":zero": 0, ":owner": owner})
        except ClientError as exc:
            if not conditional_failure(exc):
                raise

    def bind_input(self, job_id, owner, digest):
        try:
            self.client.update(job_id, expression="SET input_sha256=:hash", condition="#o=:owner AND (attribute_not_exists(input_sha256) OR input_sha256=:hash)",
                               names={"#o": "owner"}, values={":owner": owner, ":hash": digest})
        except ClientError as exc:
            if conditional_failure(exc):
                raise ValueError("Source object changed or job ownership was lost") from exc
            raise

    def bind_trace(self, job_id, owner, trace_id):
        self.client.update(job_id, expression="SET trace_id=:trace",
            condition="#o=:owner AND (attribute_not_exists(trace_id) OR trace_id=:trace)",
            names={"#o": "owner"}, values={":owner": owner, ":trace": trace_id})

    def assert_owned(self, identity):
        row = self.client.get(identity["job_id"])
        if (not row or row.get("runtime_id") != identity["runtime_id"]
                or row.get("owner") != identity["owner"]
                or row.get("lease_until", 0) < int(self.clock())):
            raise LeaseLost("Job lease is no longer owned")
        return row

    def check_event(self, event):
        row = self.client.get(event["job_id"])
        if (not row or row.get("runtime_id") != event["runtime_id"] or row.get("owner") != event["owner"]):
            raise LeaseLost("Stale event owner/runtime")
        if row.get("last_event_id") == event["event_id"]:
            return False
        if row.get("status") == "completed" or row.get("lease_until", 0) < int(self.clock()):
            raise LeaseLost("Job is completed or lease has expired")
        if row.get("sequence", 0) >= event["sequence"]:
            raise LeaseLost("Out-of-order event")
        return True

    def apply(self, event, output=None):
        if not self.check_event(event):
            return
        fields = {"status": event["status"], "stage": event["stage"], "progress": event["progress"],
                  "sequence": event["sequence"], "last_event_id": event["event_id"],
                  "details": event.get("details", {}), "updated_at": int(self.clock())}
        if output is not None:
            fields["output"] = output
        names = {f"#f{i}": key for i, key in enumerate(fields)}
        values = {f":f{i}": value for i, value in enumerate(fields.values())}
        names.update({"#o": "owner", "#r": "runtime_id", "#q": "sequence", "#s": "status", "#l": "lease_until"})
        values.update({":owner": event["owner"], ":runtime": event["runtime_id"], ":seq": event["sequence"],
                       ":done": "completed", ":now": int(self.clock())})
        try:
            self.client.update(event["job_id"], expression="SET " + ", ".join(f"#f{i}=:f{i}" for i in range(len(fields))),
                condition="#o=:owner AND #r=:runtime AND #q<:seq AND #s<>:done AND #l>=:now", names=names, values=values)
        except ClientError as exc:
            if conditional_failure(exc):
                raise LeaseLost("Progress update lost ownership or ordering") from exc
            raise
