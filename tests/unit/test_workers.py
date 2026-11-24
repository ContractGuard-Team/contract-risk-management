"""Isolated intake/progress tests with emulated AWS; no preprocessing or agents."""
import json
import multiprocessing
import os
from queue import Queue
import unittest
from unittest.mock import patch

import boto3
import httpx
from threading import Event
from email.parser import BytesParser
from email.policy import default
from workers.connectors.input_processing import InputProcessingClient
from workers.connectors.langfuse import LangfuseClient
from moto import mock_aws

from workers.connectors.dynamodb_client import DynamoDBClient
from workers.connectors.s3_client import S3Client
from workers.connectors.sqs_client import SQSClient
from workers.runtime.heartbeat import Heartbeat
from workers.runtime.intake_worker import IntakeWorker
from workers.runtime.job_store import JobStore, LeaseLost
from workers.runtime.progress_worker import JobEvents, ProgressChannel, ProgressWorker
from workers.schemas import QueueJob
from workers.settings import WorkerSettings


REGION = "us-east-1"
BODY = {"job_id": "job-test", "contract_id": "contract-test", "bucket": "input-contracts",
        "source_file_key": "contracts/test.txt", "metadata": {"tenant": "demo", "score": 1.5}}


def fake_extraction(note, job_id=None, **document):
    """Smallest stand-in for the LangGraph extraction.v2 the worker persists (the worker never builds it)."""
    return {"schema_version": "2.0.0", "job_id": job_id or BODY["job_id"], "document": document,
            "clauses": [{"clause_id": "c1"}], "overall_recommendation": note}


def table_client():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="jobs", KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    return DynamoDBClient("jobs", region=REGION)


def event(sequence=1, **updates):
    return {"job_id": BODY["job_id"], "contract_id": BODY["contract_id"],
            "runtime_id": "fixed-runtime", "owner": "owner", "event_id": f"event-{sequence}",
            "sequence": sequence, "status": "running", "stage": "agent_1", "progress": 40,
            "details": {}, **updates}


def final_event(sequence=2):
    return event(sequence, stage="completed", status="completed", progress=100,
                 result=fake_extraction("fixture recommendation"))


def progress_child(requests, responses, stop):
    # Moto lives inside the spawned process; no inherited mock/global state is assumed.
    with mock_aws():
        store = JobStore(table_client())
        job = QueueJob.from_body({**BODY, "runtime_id": "fixed-runtime"})
        store.reserve(job)
        store.claim(job.job_id, job.runtime_id, "owner", 180)
        boto3.client("s3", region_name=REGION).create_bucket(Bucket="output-contracts")
        ProgressWorker(store, S3Client(region=REGION), "output-contracts").serve(requests, responses, stop)


class WorkerAWSTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                                           "AWS_DEFAULT_REGION": REGION})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.aws = mock_aws()
        self.aws.start()
        self.addCleanup(self.aws.stop)
        self.ddb = table_client()
        self.clock = [1000]
        self.store = JobStore(self.ddb, clock=lambda: self.clock[0])
        self.s3 = boto3.client("s3", region_name=REGION)
        for bucket in ("input-contracts", "output-contracts"):
            self.s3.create_bucket(Bucket=bucket)
        self.s3.put_object(Bucket="input-contracts", Key=BODY["source_file_key"], Body=b"Article 1. Payment\nPay within thirty days.")
        self.storage = S3Client(client=self.s3)
        self.sqs = boto3.client("sqs", region_name=REGION)
        url = self.sqs.create_queue(QueueName="contracts")["QueueUrl"]
        self.queue = SQSClient(url, client=self.sqs)
        self.settings = WorkerSettings(url, "jobs", "output-contracts")
        self.submissions = []
        self.remote_failure = False
        def remote(request):
            if request.method == "POST":
                mail = BytesParser(policy=default).parsebytes(
                    ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode() + request.read())
                parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                         for part in mail.iter_parts()}
                payload = json.loads(parts["job"])
                self.submissions.append((payload, request.headers["idempotency-key"], parts["file"]))
                return httpx.Response(202, json={"status": "accepted", **{k: payload[k] for k in
                    ("job_id", "runtime_id", "contract_id", "trace_id")}})
            row = self.ddb.get(BODY["job_id"])
            observations = []
            for index, stage in enumerate(("processing", "agent_1", "agent_2", "agent_3"), 1):
                identity = {"job_id": row["job_id"], "runtime_id": row["runtime_id"], "contract_id": BODY["contract_id"]}
                observations.append({"id": f"obs-{index}", "traceId": row["trace_id"], "endTime": "2026-11-07T00:00:00Z",
                    "metadata": {**identity, "worker_event": {"sequence": index, "stage": stage,
                        "state": "failed" if self.remote_failure else "completed", "progress": index * 20}},
                    "output": json.dumps(fake_extraction("synthetic result"))})
            return httpx.Response(200, json={"data": observations, "meta": {}})
        client = httpx.Client(transport=httpx.MockTransport(remote))
        self.addCleanup(client.close)
        self.processor = InputProcessingClient("http://processing/jobs", client=client)
        self.reader = LangfuseClient("http://tracing", "public-test", "secret-test", client=client)
        self.writer = ProgressWorker(self.store, self.storage, "output-contracts", langfuse=self.reader, poll_seconds=0.01)

    def message(self, body=None):
        self.sqs.send_message(QueueUrl=self.settings.queue_url, MessageBody=json.dumps(body or BODY))
        return self.queue.receive()[0]

    def claimed(self):
        job = QueueJob.from_body({**BODY, "runtime_id": "fixed-runtime"})
        self.store.reserve(job)
        self.store.claim(job.job_id, job.runtime_id, "owner", 180)
        return job

    def worker(self, *, sender=None):
        return IntakeWorker(self.queue, self.store, self.storage,
                            sender or self.writer.handle, self.settings, processor=self.processor)

    def test_http_dispatch_trace_poll_and_persist_before_sqs_delete(self):
        original_delete = self.queue.delete
        def delete(receipt):
            row = self.ddb.get(BODY["job_id"])
            self.assertEqual(row["status"], "completed")
            output = self.s3.get_object(Bucket=row["output"]["bucket"], Key=row["output"]["key"])
            self.assertEqual(json.loads(output["Body"].read())["result"]["overall_recommendation"], "synthetic result")
            original_delete(receipt)
        with patch.object(self.queue, "delete", side_effect=delete) as deleted:
            self.assertEqual(self.worker().handle(self.message()), "completed")
            self.assertEqual(self.worker().handle(self.message()), "duplicate_completed")
        self.assertEqual(deleted.call_count, 2)
        self.assertEqual(len(self.submissions), 1)
        payload, key, data = self.submissions[0]
        self.assertEqual(data, b"Article 1. Payment\nPay within thirty days.")
        row = self.ddb.get(BODY["job_id"])
        self.assertEqual(row["trace_id"], payload["trace_id"])
        self.assertEqual(row["runtime_id"], payload["runtime_id"])
        self.assertEqual(len(row["runtime_id"]), 32)
        self.assertEqual(row["progress"], 100)
        self.assertNotIn("synthetic result", json.dumps(row, default=str))

    def test_reservation_reuses_runtime_and_rejects_conflicting_input(self):
        job = QueueJob.from_body(BODY)
        first = self.store.reserve(job)
        self.assertEqual(self.store.reserve(job)["runtime_id"], first["runtime_id"])
        for changes in ({"runtime_id": "different"}, {"source_file_key": "different.txt"}, {"contract_id": "changed"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.reserve(QueueJob.from_body({**BODY, **changes}))
        supplied = QueueJob.from_body({**BODY, "job_id": "provided", "runtime-id": "user-runtime"})
        self.assertEqual(self.store.reserve(supplied)["runtime_id"], "user-runtime")

    def test_lease_blocks_concurrent_owner_and_fences_old_events(self):
        job = self.claimed()
        self.assertIsNone(self.store.claim(job.job_id, job.runtime_id, "other", 180))
        self.clock[0] += 181
        with self.assertRaises(LeaseLost):
            self.store.renew(job.job_id, job.runtime_id, "owner", 180)
        self.assertIsNotNone(self.store.claim(job.job_id, job.runtime_id, "other", 180))
        with self.assertRaises(LeaseLost):
            self.writer.handle(event())

    def test_ordered_overwrites_duplicate_final_and_no_regression(self):
        self.claimed()
        self.writer.handle(event())
        self.writer.handle(event(3, stage="agent_2", progress=80))
        with self.assertRaises(LeaseLost):
            self.writer.handle(event(2))
        final = final_event(4)
        with patch.object(self.storage, "write_json", wraps=self.storage.write_json) as write:
            self.writer.handle(final)
            self.writer.handle(final)
            self.assertEqual(write.call_count, 1)
        with self.assertRaises(LeaseLost):
            self.writer.handle(event(5))
        self.assertEqual(self.ddb.get(BODY["job_id"])["status"], "completed")

    def test_s3_output_failure_does_not_mark_complete(self):
        self.claimed()
        with patch.object(self.storage, "write_json", side_effect=IOError("simulated")), self.assertRaises(IOError):
            self.writer.handle(final_event())
        row = self.ddb.get(BODY["job_id"])
        self.assertNotEqual(row["status"], "completed")
        self.assertNotIn("output", row)
        self.writer.handle(final_event())
        self.assertEqual(self.ddb.get(BODY["job_id"])["status"], "completed")

    def test_dynamodb_failure_after_s3_does_not_ack_completion(self):
        self.claimed()
        with patch.object(self.store, "apply", side_effect=IOError("simulated")), self.assertRaises(IOError):
            self.writer.handle(final_event())
        self.assertNotEqual(self.ddb.get(BODY["job_id"])["status"], "completed")
        self.writer.handle(final_event())
        self.assertEqual(self.ddb.get(BODY["job_id"])["status"], "completed")

    def test_changed_s3_content_rejected_on_retry(self):
        worker, message = self.worker(), self.message()
        with patch.object(self.storage, "write_json", side_effect=IOError("simulated")), self.assertRaises(IOError):
            worker.handle(message)
        self.s3.put_object(Bucket="input-contracts", Key=BODY["source_file_key"], Body=b"Changed contract")
        with patch.object(self.queue, "delete") as deleted, self.assertRaises(ValueError):
            worker.handle(message)
        deleted.assert_not_called()
        self.assertEqual(len(self.submissions), 1)

    def test_empty_document_fails_at_once_and_is_removed(self):
        self.s3.put_object(Bucket="input-contracts", Key=BODY["source_file_key"], Body=b"")
        with patch.object(self.queue, "delete") as deleted:
            self.assertEqual(self.worker().handle(self.message()), "failed")
        deleted.assert_called_once()
        self.assertEqual(self.ddb.get(BODY["job_id"])["status"], "failed")

    def test_heartbeat_extends_visibility_and_lease(self):
        job = self.claimed()
        message = self.message()
        self.clock[0] += 10
        beat = Heartbeat(self.queue, self.store, message["ReceiptHandle"], job.job_id,
                         job.runtime_id, "owner", self.settings)
        beat.tick()
        self.assertEqual(self.ddb.get(job.job_id)["lease_until"], 1190)
        self.clock[0] += 181
        with self.assertRaises(LeaseLost):
            beat.tick()

    def test_busy_duplicate_never_downloads_or_deletes_message(self):
        self.claimed()
        with patch.object(self.storage, "read") as read, patch.object(self.queue, "delete") as deleted:
            outcome = self.worker().handle(self.message())
        self.assertEqual(outcome, "busy")
        read.assert_not_called()
        deleted.assert_not_called()

    def test_polling_dispatches_messages_and_observes_stop(self):
        stop = Event()
        worker = self.worker()
        message = self.message()
        with patch.object(self.queue, "receive", return_value=[message]) as receive, \
             patch.object(worker, "handle", side_effect=lambda _: stop.set()) as handle:
            worker.serve(stop)
        handle.assert_called_once_with(message)
        receive.assert_called_once_with(visibility=self.settings.visibility_seconds)

    def test_retry_reuses_trace_and_idempotency_key_after_output_failure(self):
        worker, message = self.worker(), self.message()
        with patch.object(self.queue, "delete") as deleted, \
             patch.object(self.storage, "write_json", side_effect=IOError("simulated")), self.assertRaises(IOError):
            worker.handle(message)
        deleted.assert_not_called()
        self.assertEqual(self.ddb.get(BODY["job_id"])["progress"], 95)
        self.assertEqual(worker.handle(message), "completed")
        self.assertEqual(self.submissions[0][:2], self.submissions[1][:2])

    def test_failed_remote_stage_retains_sqs_and_does_not_store_result(self):
        self.remote_failure = True
        with patch.object(self.queue, "delete") as deleted, self.assertRaises(RuntimeError):
            self.worker().handle(self.message())
        deleted.assert_not_called()
        row = self.ddb.get(BODY["job_id"])
        self.assertEqual(row["status"], "retrying")
        self.assertNotIn("output", row)

    def test_trace_timeout_never_marks_complete(self):
        self.claimed()
        self.store.bind_trace(BODY["job_id"], "owner", "trace")
        command = {**event(), "operation": "watch", "trace_id": "trace",
                   "from_start_time": "2026-11-07T00:00:00Z", "timeout_seconds": 0.01}
        with patch.object(self.reader, "observations", return_value=[]), self.assertRaises(TimeoutError):
            self.writer.handle(command)
        self.assertNotEqual(self.ddb.get(BODY["job_id"])["status"], "completed")

    def test_invalid_message_is_not_deleted_or_registered(self):
        message = self.message({"contract_id": "missing-job"})
        with patch.object(self.queue, "delete") as deleted, self.assertRaises(ValueError):
            self.worker().handle(message)
        deleted.assert_not_called()
        self.assertIsNone(self.ddb.get(BODY["job_id"]))


class WorkerBoundaryTests(unittest.TestCase):
    def test_payload_validation_and_bucket_default(self):
        job = QueueJob.from_body({"job_id": "a", "contract_id": "b", "source_file_key": "x.pdf"}, default_bucket="uploads")
        self.assertEqual(job.source["bucket"], "uploads")
        for changes in ({"runtime_id": ""}, {"runtime_id": "a", "runtime-id": "b"},
                        {"context": {"worker": {}}}, {"job_id": ""}, {"source_file_key": "no.exe"},
                        {"metadata": "x" * 33000}, {"bucket": ""}, {"source_file_key": None},
                        {"contract_version": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                QueueJob.from_body({**BODY, **changes})

    def test_progress_channel_timeout_and_negative_ack(self):
        requests, responses = Queue(), Queue()
        with self.assertRaises(TimeoutError):
            ProgressChannel(requests, responses, timeout=0.01).send(event())
        responses.put({"event_id": "event-1", "ok": False, "error_type": "IOError"})
        with self.assertRaises(RuntimeError):
            ProgressChannel(requests, responses, timeout=1).send(event())

    def test_entrypoint_loads_environment_without_empty_aws_profile(self):
        from workers.main import main
        env = {"WORKER_SQS_QUEUE_URL": "environment-queue", "AWS_PROFILE": "",
               "INTERNAL_API_TOKEN": "unit-test-token-" * 3, "WORKER_ENABLED": "true"}
        file_values = {"WORKER_SQS_QUEUE_URL": "file-queue", "WORKER_DYNAMODB_TABLE": "jobs",
                       "WORKER_OUTPUT_BUCKET": "outputs", "AWS_PROFILE": "", "AWS_REGION": "us-east-1"}
        with patch.dict(os.environ, env, clear=True), patch("dotenv.dotenv_values", return_value=file_values), \
             patch("workers.main.run", return_value=0) as run:
            self.assertEqual(main([]), 0)
            settings = run.call_args.args[0]
            self.assertEqual(settings.queue_url, "environment-queue")
            self.assertEqual(settings.region, "us-east-1")
            self.assertNotIn("AWS_PROFILE", os.environ)

    def test_spawned_progress_process_acks_success_and_rejects_stale_event(self):
        context = multiprocessing.get_context("spawn")
        requests, responses, stop = context.Queue(), context.Queue(), context.Event()
        child = context.Process(target=progress_child, args=(requests, responses, stop))
        child.start()
        try:
            channel = ProgressChannel(requests, responses, timeout=20)
            channel.send(event())
            channel.send(final_event())
            with self.assertRaises(RuntimeError):
                channel.send(event(3))
        finally:
            stop.set()
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join()
            requests.close()
            responses.close()
        self.assertEqual(child.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
