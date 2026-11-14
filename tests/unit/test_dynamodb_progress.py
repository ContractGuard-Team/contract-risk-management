from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import boto3
from fastapi.testclient import TestClient
from moto import mock_aws

from common.service_http import base_app
from monitoring.service import EventStore
from monitoring.worker_exporter import WorkerExporter
from workers.connectors.dynamodb_tables import DynamoTables
from workers.runtime.contract_job_store import ContractJobStore, ContractBusy
from workers.runtime.job_store import LeaseLost
from workers.runtime.monitoring_inbox import MonitoringInbox, attach_ingress
from workers.runtime.progress_worker import ProgressWorker
from workers.schemas import QueueJob


REGION = "ap-southeast-1"
TOKEN = "progress-test-internal-token-" * 2


class DynamoProgressTests(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.env = patch.dict(os.environ, {"INTERNAL_API_TOKEN": TOKEN, "MONITORING_URL": "", "MONITORING_WORKER_ENABLED": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        client = boto3.client("dynamodb", region_name=REGION)
        for table, keys in (("contract-risk-jobs", ["job_id"]), ("contract-risk-job-events", ["job_id", "event_id"])):
            client.create_table(TableName=table, KeySchema=[{"AttributeName": key, "KeyType": "HASH" if i == 0 else "RANGE"} for i, key in enumerate(keys)],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in keys], BillingMode="PAY_PER_REQUEST")
        self.tables = DynamoTables("contract-risk-jobs", "contract-risk-job-events", REGION, client=client)
        self.clock = [1000.0]
        self.store = ContractJobStore(self.tables, clock=lambda: self.clock[0])

    def job(self, name="job-a", contract="contract-a"):
        return QueueJob.from_body({"job_id": name, "contract_id": contract, "s3_bucket": "input", "s3_key": "a.txt"})

    def start(self, name="job-a", contract="contract-a"):
        job = self.job(name, contract)
        row = self.store.reserve(job)
        self.store.claim(name, row["runtime_id"], "owner-" + name, 180)
        self.store.bind_input(name, "owner-" + name, "a" * 64)
        self.store.bind_trace(name, "owner-" + name, "b" * 32)
        return self.store.client.get(name)

    def event(self, row, **updates):
        return {"job_id": row["job_id"], "contract_id": row["contract_id"], "runtime_id": row["runtime_id"],
                "owner": row["owner"], "sequence": 1, "event_id": uuid4().hex, "stage": "processing",
                "status": "running", "progress": 20, "details": {}, **updates}

    def observation(self, row, **updates):
        return {"job_id": row["job_id"], "contract_id": row["contract_id"], "runtime_id": row["runtime_id"],
                "trace_id": "b" * 32, "request_id": "b" * 32, "event_id": uuid4().hex,
                "timestamp_ns": int((self.clock[0] + 1) * 1_000_000_000), "service": "agent-2",
                "stage": "agent_2_risk_scoring", "status": "completed", **updates}

    def test_schema_and_same_contract_reset_keep_append_only_history(self):
        self.assertTrue(self.tables.validate_schema()["schema_valid"])
        first = self.start()
        output = {"bucket": "output", "key": "old.json"}
        self.store.apply(self.event(first, status="completed", stage="completed", progress=100), output)
        history = self.tables.client.scan(TableName=self.tables.events_table)["Items"]
        new = self.store.reserve(self.job("job-b"))
        current = self.store.client.physical("contract-a")
        self.assertEqual(current["job_id"], "contract:contract-a")
        self.assertEqual(current["active_job_id"], "job-b")
        self.assertEqual(current["progress"], 0)
        self.assertNotIn("output", current)
        self.assertNotEqual(new["runtime_id"], first["runtime_id"])
        self.assertEqual(self.tables.client.scan(TableName=self.tables.jobs_table)["Count"], 1)
        after = self.tables.client.scan(TableName=self.tables.events_table)["Items"]
        self.assertTrue(all(row in after for row in history))
        old = self.store.reserve(self.job())
        self.assertEqual(old["status"], "completed")
        self.assertEqual(old["output"], output)
        self.assertEqual(self.store.client.physical("contract-a")["active_job_id"], "job-b")

    def test_duplicate_and_active_contract_do_not_reset(self):
        row = self.start()
        self.store.apply(self.event(row))
        same = self.store.reserve(self.job())
        self.assertEqual(same["runtime_id"], row["runtime_id"])
        self.assertEqual(same["progress"], 20)
        with self.assertRaises(ContractBusy):
            self.store.reserve(self.job("job-b"))
        self.assertIsNone(self.tables.event("job-b", "registered"))
        self.clock[0] += 181
        self.store.reserve(self.job("job-b"))
        self.assertEqual(self.store.reserve(self.job())["status"], "superseded")
        with self.assertRaises(LeaseLost):
            self.store.apply(self.event(row, sequence=2))

    def test_monitoring_append_dedupe_order_and_terminal_guard(self):
        row = self.start()
        observation = self.observation(row)
        self.store.observe(observation)
        self.store.observe(observation)
        self.assertEqual(self.store.client.get(row["job_id"])["progress"], 75)
        self.assertEqual(self.store.client.get(row["job_id"])["status"], "running")
        self.store.apply(self.event(row, progress=30, stage="agent_1"))
        current = self.store.client.get(row["job_id"])
        self.assertEqual((current["stage"], current["progress"]), ("agent_2", 75))
        self.store.observe(self.observation(row, service="agent-1", timestamp_ns=observation["timestamp_ns"]-1))
        self.assertEqual(self.store.client.get(row["job_id"])["progress"], 75)
        with self.assertRaises(ValueError):
            self.store.observe(self.observation(row, trace_id="c"*32))
        self.store.apply(self.event(row, sequence=2, status="completed", stage="completed", progress=100), {"bucket":"out","key":"result"})
        self.store.observe(self.observation(row, service="agent-3"))
        self.assertEqual(self.store.client.get(row["job_id"])["status"], "completed")
        logs = self.tables.client.query(TableName=self.tables.events_table, KeyConditionExpression="job_id=:job",
            ExpressionAttributeValues={":job":{"S":row["job_id"]}})["Items"]
        self.assertEqual(sum(item["event_id"]["S"] == "monitoring:"+observation["event_id"] for item in logs), 1)

    def test_old_run_monitoring_logs_without_overwriting_new_run(self):
        old = self.start()
        self.store.apply(self.event(old, status="completed", stage="completed", progress=100), {"bucket":"out","key":"old"})
        new = self.start("job-b")
        late = self.observation(old, service="agent-3")
        self.store.observe(late)
        self.assertIsNotNone(self.tables.event(old["job_id"], "monitoring:" + late["event_id"]))
        self.assertEqual(self.store.client.get(new["job_id"])["progress"], 0)

    def test_s3_failure_never_completes_or_appends_completion(self):
        row = self.start()
        class BrokenS3:
            def write_json(self, *args):
                raise RuntimeError("S3 unavailable")
        writer = ProgressWorker(self.store, BrokenS3(), "output")
        with self.assertRaises(RuntimeError):
            writer.handle(self.event(row, status="completed", stage="completed", progress=100,
                          result={"agent_id":"agent_3_recommendation","text":"test"}))
        self.assertIsNone(self.tables.event(row["job_id"], "completed"))
        self.assertEqual(self.store.client.get(row["job_id"])["status"], "running")

    def test_monitoring_http_to_durable_inbox_to_two_tables(self):
        row = self.start()
        observation = self.observation(row)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.db"
            inbox = MonitoringInbox(path)
            app = base_app("worker")
            attach_ingress(app, inbox)
            with TestClient(app) as http:
                self.assertEqual(http.post("/v1/monitoring-events", json=observation).status_code, 401)
                class ClientAdapter:
                    def request(self, method, url, *, json, request_id):
                        response = http.request(method, url, json=json, headers={"Authorization":"Bearer "+TOKEN})
                        response.raise_for_status()
                        return response.json()
                exporter = WorkerExporter(client=ClientAdapter())
                exporter.export(observation)
                exporter.export(observation)
            inbox.close()
            inbox = MonitoringInbox(path)
            try:
                writer = ProgressWorker(self.store, None, "output", inbox=inbox)
                writer.drain_monitoring()
                self.assertIsNone(inbox.next())
                self.assertEqual(self.store.client.get(row["job_id"])["progress"], 75)
                self.assertIsNotNone(self.tables.event(row["job_id"], "monitoring:" + observation["event_id"]))
            finally:
                inbox.close()

    def test_event_store_upgrade_skips_historical_rows_and_tracks_new_outbox(self):
        import sqlite3
        row = self.start()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, service TEXT, trace_id TEXT, payload TEXT, exported INTEGER DEFAULT 0)")
                db.execute("INSERT INTO events VALUES('old','worker','old','{}',0)")
            db.close()
            store = EventStore(path)
            try:
                self.assertEqual(store.metrics()["pending_worker_export"], 0)
                store.add(self.observation(row))
                self.assertEqual(store.metrics()["pending_worker_export"], 1)
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
