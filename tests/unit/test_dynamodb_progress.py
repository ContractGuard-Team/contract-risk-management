"""jobs = where the contract is now; events = only completed / failed for good / cancelled."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from common.exceptions import JobCancelled
from workers.connectors.dynamodb_tables import DynamoTables, write
from workers.contracts import SourceError
from workers.runtime.contract_job_store import ContractJobStore, ContractBusy
from workers.runtime.job_store import LeaseLost
from workers.runtime.progress_worker import ProgressWorker
from workers.schemas import QueueJob

REGION = "ap-southeast-1"


def make_tables():
    client = boto3.client("dynamodb", region_name=REGION)
    for table, keys in (("contract-risk-jobs", ["job_id"]), ("contract-risk-job-events", ["job_id", "event_id"])):
        client.create_table(TableName=table, KeySchema=[{"AttributeName": key, "KeyType": "HASH" if i == 0 else "RANGE"} for i, key in enumerate(keys)],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in keys], BillingMode="PAY_PER_REQUEST")
    return DynamoTables("contract-risk-jobs", "contract-risk-job-events", REGION, client=client)


def all_events(tables):
    return [tables.decode(item) for item in tables.client.scan(TableName=tables.events_table)["Items"]]


class DynamoProgressTests(unittest.TestCase):
    def setUp(self):
        mock = mock_aws(); mock.start(); self.addCleanup(mock.stop)
        env = patch.dict(os.environ, {"MONITORING_URL": ""}); env.start(); self.addCleanup(env.stop)
        self.tables = make_tables()
        self.clock = [1000.0]
        self.store = ContractJobStore(self.tables, clock=lambda: self.clock[0])

    def job(self, name="job-a", contract="contract-a", key="a.txt"):
        return QueueJob.from_body({"job_id": name, "contract_id": contract, "bucket": "input", "source_file_key": key})

    def start(self, name="job-a", contract="contract-a"):
        row = self.store.reserve(self.job(name, contract))
        self.store.claim(name, row["runtime_id"], "owner-" + name, 180)
        self.store.bind_input(name, "owner-" + name, "a" * 64)
        self.store.bind_trace(name, "owner-" + name, "b" * 32)
        return self.store.client.get(name)

    def event(self, row, sequence=1, **updates):
        return {"job_id": row["job_id"], "contract_id": row["contract_id"], "runtime_id": row["runtime_id"],
                "owner": row["owner"], "sequence": sequence, "event_id": uuid4().hex, "stage": "processing",
                "status": "running", "progress": 20, "details": {}, **updates}

    def finish(self, row, sequence=90, status="completed", output=None, **updates):
        self.store.apply(self.event(row, sequence, status=status, **{"stage": "completed", "progress": 100, **updates}), output)

    def test_row_follows_the_contract_and_replaces_the_previous_log(self):
        self.assertTrue(self.tables.validate_schema()["schema_valid"])
        row = self.start()
        seen = [row["location"]]
        for sequence, (stage, details) in enumerate((("downloading", {"old": "log"}), ("processing", {}), ("langgraph", {}),
                ("agent_1", {}), ("agent_2", {}), ("agent_3", {}), ("saving_result", {})), 1):
            self.store.apply(self.event(row, sequence, stage=stage, details=details))
            current = self.store.client.get("job-a")
            self.assertEqual((current["status"], current["stage"], current["details"]), ("running", stage, details))
            seen.append(current["location"])
        self.assertEqual(seen, ["worker", "worker", "input_processing", "langgraph", "agent_1", "agent_2", "agent_3", "worker"])
        self.assertEqual(all_events(self.tables), [])  # nothing is logged while the contract is being processed
        self.finish(row, output={"bucket": "o", "key": "k"})
        done = self.store.client.get("job-a")
        self.assertEqual((done["location"], done["status"], done["progress"]), ("worker", "completed", 100))
        [log] = all_events(self.tables)
        self.assertEqual((log["event_id"], log["status"], log["contract_id"], log["output"]),
                         ("outcome", "completed", "contract-a", {"bucket": "o", "key": "k"}))
        self.store.release("job-a", "owner-job-a")
        self.clock[0] += 10_000  # the completed row stays until another job of the same contract arrives
        self.assertEqual(self.store.client.physical("contract-a")["status"], "completed")

    def test_next_job_of_the_contract_resets_the_row_and_keeps_the_old_log(self):
        first = self.start()
        self.finish(first, output={"bucket": "o", "key": "old.json"})
        new = self.store.reserve(self.job("job-b"))
        current = self.store.client.physical("contract-a")
        self.assertEqual((current["active_job_id"], current["status"], current["location"], current["progress"]),
                         ("job-b", "queued", "worker", 0))
        self.assertFalse({"output", "details", "owner", "trace_id", "input_sha256"} & current.keys())
        self.assertNotEqual(new["runtime_id"], first["runtime_id"])
        self.assertEqual(self.tables.client.scan(TableName=self.tables.jobs_table)["Count"], 1)
        old = self.store.reserve(self.job())  # redelivered old job: answered from its log, row untouched
        self.assertEqual((old["status"], old["output"]["key"]), ("completed", "old.json"))
        self.assertEqual(self.store.client.physical("contract-a")["active_job_id"], "job-b")

    def test_duplicate_and_active_contract_do_not_reset(self):
        row = self.start()
        self.store.apply(self.event(row))
        self.assertEqual(self.store.reserve(self.job())["progress"], 20)
        with self.assertRaises(ContractBusy):
            self.store.reserve(self.job("job-b"))
        with self.assertRaises(SourceError):
            self.store.reserve(self.job(key="other.txt"))
        self.clock[0] += 181
        self.store.reserve(self.job("job-b"))
        with self.assertRaises(LeaseLost):
            self.store.apply(self.event(row, 2))
        self.assertEqual(all_events(self.tables), [])

    def test_retry_is_not_logged_and_a_new_attempt_starts_clean(self):
        row = self.start()
        self.store.apply(self.event(row, stage="agent_2", status="retrying", details={"error_type": "X"}))
        current = self.store.client.get("job-a")
        self.assertEqual((current["status"], current["location"], current["details"]["error_type"]), ("retrying", "agent_2", "X"))
        self.assertEqual(all_events(self.tables), [])
        self.store.release("job-a", "owner-job-a")
        self.clock[0] += 1
        again = self.store.reserve(self.job())
        self.assertEqual(again["runtime_id"], row["runtime_id"])
        claimed = self.store.claim("job-a", again["runtime_id"], "second", 180)
        self.assertEqual((claimed["status"], claimed["location"]), ("running", "worker"))
        self.assertNotIn("details", claimed)

    def test_final_failure_logs_where_and_why_once(self):
        row = self.start()
        details = {"error_type": "RemoteServiceError", "reason": "Remote job failed: ModelResponseError", "attempts": 3}
        self.finish(row, status="failed", stage="agent_2", progress=60, details=details, output={"bucket": "o", "key": "err"})
        [log] = all_events(self.tables)
        self.assertEqual((log["status"], log["location"], log["reason"], int(log["attempts"]), log["output"]["key"]),
                         ("failed", "agent_2", details["reason"], 3, "err"))
        self.store.record_failure("job-a", "contract-a", {"error_type": "Later"})
        self.assertEqual(all_events(self.tables), [log])
        # The same job sent again may still succeed: its log becomes the latest outcome.
        self.store.release("job-a", "owner-job-a")
        self.clock[0] += 1
        self.store.claim("job-a", row["runtime_id"], "owner-job-a", 180)
        self.finish(row)
        self.assertEqual([e["status"] for e in all_events(self.tables)], ["completed"])

    def test_failure_before_any_run_is_logged_without_touching_another_job(self):
        self.start("job-b")
        self.store.record_failure("job-a", "contract-a", {"error_type": "SourceError", "reason": "bad request"})
        self.store.record_failure("job-c", None, {"error_type": "SourceError", "reason": "bad request"})
        self.assertEqual({e["job_id"]: e["status"] for e in all_events(self.tables)}, {"job-a": "failed", "job-c": "failed"})
        self.assertEqual(self.store.client.physical("contract-a")["status"], "running")

    def test_cancel_running_job_wins_over_completion_and_adds_the_location(self):
        row = self.start()
        self.assertFalse(self.store.cancelled("job-a"))
        self.assertEqual(self.store.cancel("job-a"), "cancelled")
        self.assertEqual(self.store.cancel("job-a"), "cancelled")
        self.assertTrue(self.store.cancelled("job-a"))
        with self.assertRaises(JobCancelled):
            self.finish(row)
        self.finish(row, status="cancelled", stage="agent_1", progress=30, details={"reason": "Cancelled by user"})
        [log] = all_events(self.tables)
        self.assertEqual((log["status"], log["location"], log["contract_id"]), ("cancelled", "agent_1", "contract-a"))
        self.assertEqual(self.store.client.get("job-a")["status"], "cancelled")
        self.assertEqual(self.store.reserve(self.job())["status"], "cancelled")  # redelivery never runs it
        self.assertEqual(self.store.reserve(self.job("job-b"))["status"], "queued")  # the next job of the contract does

    def test_cancel_before_start_after_crash_and_after_the_end(self):
        self.assertEqual(self.store.cancel("job-later"), "cancelled")
        self.assertEqual(self.store.reserve(self.job("job-later"))["status"], "cancelled")
        self.assertIsNone(self.store.client.physical("contract-a"))
        self.start("job-crash", "contract-crash")
        self.store.cancel("job-crash")
        self.store.mark_cancelled("job-crash")  # still leased by a live worker: that worker writes it
        self.assertEqual(self.store.client.get("job-crash")["status"], "running")
        self.clock[0] += 181
        self.store.mark_cancelled("job-crash")
        self.assertEqual(self.store.client.get("job-crash")["status"], "cancelled")
        self.finish(self.start("job-done", "contract-done"))
        self.assertEqual(self.store.cancel("job-done"), "completed")
        self.finish(self.start("job-bad", "contract-bad"), status="failed", stage="agent_1")
        self.assertEqual(self.store.cancel("job-bad"), "failed")
        for bad in ("", "  ", "x" * 257, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.cancel(bad)

    def test_completion_receipts_of_the_previous_format_still_count(self):
        self.tables.client.put_item(TableName=self.tables.events_table, Item=self.tables.encode(
            {"job_id": "job-a", "event_id": "completed", "record_type": "completion_receipt", "status": "completed",
             "contract_id": "contract-a", "runtime_id": "old", "output": {"bucket": "o", "key": "old"}}))
        self.assertEqual(self.store.reserve(self.job())["status"], "completed")
        self.assertEqual(self.store.cancel("job-a"), "completed")
        self.assertTrue(self.store.completed("job-a"))

    def test_s3_failure_never_completes_or_logs_completion(self):
        row = self.start()
        class BrokenS3:
            def write_json(self, *args):
                raise RuntimeError("S3 unavailable")
        writer = ProgressWorker(self.store, BrokenS3(), "output")
        result = {"schema_version": "2.0.0", "job_id": row["job_id"], "document": {}}
        with self.assertRaises(RuntimeError):
            writer.handle(self.event(row, status="completed", stage="completed", progress=100, result=result))
        self.assertEqual((all_events(self.tables), self.store.client.get("job-a")["status"]), ([], "running"))
        # ... but a failure is logged even when its error output cannot be written.
        with self.assertLogs("workers.runtime.progress_worker", "ERROR"):
            writer.handle(self.event(row, status="failed", stage="agent_1", result=result))
        self.assertEqual([e["status"] for e in all_events(self.tables)], ["failed"])

    def test_write_repeats_only_row_conflicts(self):
        def error(code, *reasons):
            return ClientError({"Error": {"Code": code}, "CancellationReasons": [{"Code": r} for r in reasons]}, "x")
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise error("TransactionCanceledException", "None", "TransactionConflict")
            return "ok"
        self.assertEqual((write(flaky), len(calls)), ("ok", 3))
        with self.assertRaises(ClientError), patch("workers.connectors.dynamodb_tables.time.sleep") as sleep:
            write(lambda: (_ for _ in ()).throw(error("TransactionCanceledException", "ConditionalCheckFailed")))
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
