"""ContractGuard protocol (07-worker-aws-integration.md §1-4) on the existing worker, against Moto S3/SQS/DynamoDB."""
import json
import os
import unittest
from unittest.mock import patch

import boto3
from moto import mock_aws

from workers.connectors.s3_client import S3Client
from workers.connectors.sqs_client import SQSClient
from workers.partner import ERR_INPUT, ERR_PROCESSING, KEEPALIVE_SECONDS, PartnerProtocol, check_outbound
from workers.runtime.intake_worker import IntakeWorker
from workers.runtime.job_store import JobStore
from workers.runtime.progress_worker import ProgressWorker
from workers.settings import WorkerSettings
from test_workers import REGION, table_client

BUCKET = "contractguard-dev-documents-699061129385"
JOB = "5f0c2c1e-0000-4000-8000-000000000001"
REQUEST = {"source_file_key": "quarantine/user-1/upload-1.txt", "clean_file_key": "clean/ws-1/contract-9/v2/source.txt",
           "result_key": f"results/{JOB}/extraction.json", "output_language": "vi", "context": {"party": "A"},
           "limits": {"max_pages": 50}}
CONTRACT = b"Dieu 1. Thanh toan trong 30 ngay."


class FakeProcessor:
    def __init__(self, page_count=None):
        self.calls, self.contexts, self.page_count = 0, [], page_count

    def process(self, job, file, identity, guard):
        self.calls += 1
        self.contexts.append(job.context)
        return {"contract_id": job.contract_id, "chunks": [file.data.decode()],
                "context": {**job.context, "preprocessing": {"document": {"page_count": self.page_count}}}}


class FakeWorkflow:
    def __init__(self, error=None):
        self.error = error

    def execute(self, contract, identity, guard, on_progress):
        if self.error:
            raise self.error
        on_progress({"status": "running", "stage": "agent_1", "progress": 40})
        on_progress({"status": "running", "stage": "agent_3", "progress": 85})
        return {"agent_id": "agent_3_recommendation", "text": "đề xuất thử"}


class PartnerWorkerTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION,
                                      "MONITORING_URL": ""})
        env.start(); self.addCleanup(env.stop)
        aws = mock_aws(); aws.start(); self.addCleanup(aws.stop)
        self.s3 = boto3.client("s3", region_name=REGION)
        self.s3.create_bucket(Bucket=BUCKET)
        self.s3.put_object(Bucket=BUCKET, Key=REQUEST["source_file_key"], Body=CONTRACT, ContentType="text/plain")
        self.s3.put_object(Bucket=BUCKET, Key=f"results/{JOB}/request.json", Body=json.dumps(REQUEST).encode())
        self.sqs = boto3.client("sqs", region_name=REGION)
        self.requests_url = self.sqs.create_queue(QueueName="contractguard-dev-analysis-requests")["QueueUrl"]
        self.results_url = self.sqs.create_queue(QueueName="contractguard-dev-analysis-results")["QueueUrl"]
        storage = S3Client(client=self.s3)
        self.store = JobStore(table_client())
        self.settings = WorkerSettings(self.requests_url, "jobs", BUCKET, input_bucket=BUCKET,
                                       results_queue_url=self.results_url)
        self.partner = PartnerProtocol(SQSClient(self.results_url, client=self.sqs), storage, BUCKET)
        self.processor = FakeProcessor()
        self.queue = SQSClient(self.requests_url, client=self.sqs)
        self.writer = ProgressWorker(self.store, storage, BUCKET)

    def worker(self, workflow=None):
        return IntakeWorker(self.queue, self.store, S3Client(client=self.s3), self.writer.handle, self.settings,
                            processor=self.processor, workflow=workflow or FakeWorkflow(), partner=self.partner)

    def send(self, body):
        self.sqs.send_message(QueueUrl=self.requests_url, MessageBody=json.dumps({"message_version": "1", **body}))
        return self.queue.receive()[0]

    def results(self):
        out = []
        while True:
            batch = self.sqs.receive_message(QueueUrl=self.results_url, MaxNumberOfMessages=10).get("Messages", [])
            if not batch:
                return out
            for m in batch:
                out.append(json.loads(m["Body"]))
                self.sqs.delete_message(QueueUrl=self.results_url, ReceiptHandle=m["ReceiptHandle"])

    def requests_left(self):
        return int(self.sqs.get_queue_attributes(QueueUrl=self.requests_url, AttributeNames=["All"])["Attributes"]
                   ["ApproximateNumberOfMessagesNotVisible"]) + int(
            self.sqs.get_queue_attributes(QueueUrl=self.requests_url, AttributeNames=["All"])["Attributes"]
            ["ApproximateNumberOfMessages"])

    def keys(self):
        return {o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])}

    def test_ping_answers_pong_and_deletes(self):
        self.assertEqual(self.worker().handle(self.send({"event_type": "ping", "job_id": JOB})), "pong")
        self.assertEqual(self.results(), [{"message_version": "1", "event_type": "pong", "job_id": JOB}])
        self.assertEqual(self.requests_left(), 0)

    def test_analysis_request_full_cycle_then_redelivery_answers_without_recompute(self):
        message = {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        self.assertEqual(self.worker().handle(self.send(message)), "completed")
        sent = self.results()
        self.assertEqual({m["message_version"] for m in sent}, {"1"})
        steps = [m["step"] for m in sent if m["event_type"] == "progress"]
        self.assertEqual(steps[:3], ["downloading", "dispatching", "processing"])
        self.assertIn("agent_3", steps)
        self.assertEqual(sent[-1], {"message_version": "1", "event_type": "completed", "job_id": JOB,
                                    "result_key": REQUEST["result_key"]})
        self.assertEqual(self.s3.get_object(Bucket=BUCKET, Key=REQUEST["clean_file_key"])["Body"].read(), CONTRACT)
        result = json.loads(self.s3.get_object(Bucket=BUCKET, Key=REQUEST["result_key"])["Body"].read())
        self.assertEqual(result["document"]["clean_file_key"], REQUEST["clean_file_key"])
        self.assertEqual(result["result"]["text"], "đề xuất thử")
        self.assertNotIn(REQUEST["source_file_key"], self.keys())  # quarantine removed after completed
        self.assertEqual(self.requests_left(), 0)
        row = self.store.client.get(JOB)
        self.assertEqual((row["job_info"]["contract_id"], row["job_info"]["contract_version"], row["status"]),
                         ("contract-9", "2", "completed"))
        # SQS at-least-once: same message again -> completed re-sent, no second analysis.
        self.assertEqual(self.worker().handle(self.send(message)), "duplicate_completed")
        self.assertEqual([m["event_type"] for m in self.results()], ["completed"])
        self.assertEqual(self.processor.calls, 1)

    def test_bad_request_reports_failed_and_drops_message(self):
        bad = {**REQUEST, "source_file_key": "clean/elsewhere.txt"}
        self.s3.put_object(Bucket=BUCKET, Key=f"results/{JOB}/request.json", Body=json.dumps(bad).encode())
        message = {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        self.assertEqual(self.worker().handle(self.send(message)), "failed")
        [failed] = self.results()
        self.assertEqual((failed["event_type"], failed["error_code"]), ("failed", ERR_INPUT))
        self.assertEqual(self.requests_left(), 0)
        self.assertIn(REQUEST["source_file_key"], self.keys())  # nothing deleted on failure

    def test_transient_error_retries_then_fails_on_last_receive(self):
        message = {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        received = self.send(message)
        with self.assertRaises(RuntimeError):
            self.worker(FakeWorkflow(RuntimeError("model outage"))).handle(received)
        self.assertNotIn("failed", [m["event_type"] for m in self.results()])  # retried by SQS, not reported
        last = {**received, "Attributes": {"ApproximateReceiveCount": "3"}}
        self.assertEqual(self.worker(FakeWorkflow(RuntimeError("model outage"))).handle(last), "failed")
        failed = [m for m in self.results() if m["event_type"] == "failed"]
        self.assertEqual((failed[0]["error_code"], failed[0]["failure_reason"]), (ERR_PROCESSING, "RuntimeError"))

    def test_clean_copy_keeps_the_uploaded_content_type(self):
        message = {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        self.worker().handle(self.send(message))
        head = self.s3.head_object(Bucket=BUCKET, Key=REQUEST["clean_file_key"])
        self.assertEqual(head["ContentType"], "text/plain")

    def test_internal_value_error_is_retried_not_reported_as_bad_input(self):
        message = {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        received = self.send(message)
        broken = FakeWorkflow(ValueError("Workflow returned no valid Agent 3 output"))
        with self.assertRaises(ValueError):
            self.worker(broken).handle(received)
        self.assertEqual([m for m in self.results() if m["event_type"] == "failed"], [])
        self.assertEqual(self.worker(broken).handle({**received, "Attributes": {"ApproximateReceiveCount": "3"}}), "failed")
        [failed] = [m for m in self.results() if m["event_type"] == "failed"]
        self.assertEqual(failed["error_code"], ERR_PROCESSING)

    def test_completed_job_is_never_reported_failed(self):
        message = {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        received = self.send(message)
        # Quarantine cleanup fails: the job is still acknowledged as completed.
        with patch.object(self.partner.storage, "delete", side_effect=RuntimeError("AccessDenied")):
            self.assertEqual(self.worker().handle(received), "completed")
        self.assertEqual([m["event_type"] for m in self.results()][-1], "completed")
        # Redelivery whose re-send of `completed` keeps failing, up to the last receive: no `failed`, message kept.
        last = {**self.send(message), "Attributes": {"ApproximateReceiveCount": "3"}}
        with patch.object(self.partner, "send", side_effect=RuntimeError("SQS down")), self.assertRaises(RuntimeError):
            self.worker().handle(last)
        self.assertEqual(self.results(), [])
        self.assertEqual(self.requests_left(), 1)

    def rejected(self, request=None, message=None):
        if request is not None:
            self.s3.put_object(Bucket=BUCKET, Key=f"results/{JOB}/request.json", Body=json.dumps(request).encode())
        body = message or {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        self.assertEqual(self.worker().handle(self.send(body)), "failed")
        [failed] = [m for m in self.results() if m["event_type"] == "failed"]
        self.assertEqual(failed["error_code"], ERR_INPUT)
        return failed["failure_reason"]

    def test_keys_must_belong_to_this_job(self):
        other = "results/another-job/request.json"
        self.s3.put_object(Bucket=BUCKET, Key=other, Body=json.dumps(REQUEST).encode())
        self.assertIn("of this job", self.rejected(message={"event_type": "analysis_requested", "job_id": JOB, "request_key": other}))
        self.assertIn("another job_id", self.rejected({**REQUEST, "job_id": "another-job"}))
        self.assertIn("result_key", self.rejected({**REQUEST, "result_key": "results/another-job/extraction.json"}))
        self.assertIn("result_key", self.rejected({**REQUEST, "result_key": f"results/{JOB}/other.json"}))
        self.assertEqual(self.processor.calls, 0)  # rejected before any processing or model call

    def test_output_language_and_limits_reach_the_pipeline_and_are_enforced(self):
        message = {"event_type": "analysis_requested", "job_id": JOB, "request_key": f"results/{JOB}/request.json"}
        self.assertEqual(self.worker().handle(self.send(message)), "completed")
        self.assertEqual({k: self.processor.contexts[0][k] for k in ("output_language", "limits", "party")},
                         {"output_language": "vi", "limits": {"max_pages": 50}, "party": "A"})
        # Agent 3 reads the original through MCP partner_document with the keys from request.json.
        self.assertEqual(self.processor.contexts[0]["source_document"],
                         {"bucket": BUCKET, "source_file_key": REQUEST["source_file_key"],
                          "clean_file_key": REQUEST["clean_file_key"]})
        self.results()

    def test_limits_reject_too_large_or_too_long_documents(self):
        self.assertIn("max_file_bytes", self.rejected({**REQUEST, "limits": {"max_file_bytes": 5}}))
        self.assertIn("limits.max_pages", self.rejected({**REQUEST, "limits": {"max_pages": 0}}))
        self.assertIn("output_language", self.rejected({**REQUEST, "output_language": "Vietnamese please"}))

    def test_too_many_pages_is_an_input_error(self):
        self.processor = FakeProcessor(page_count=99)
        self.assertIn("99 pages", self.rejected())

    def test_outbound_messages_are_checked_before_sending(self):
        base = {"message_version": "1", "job_id": JOB}
        for good in ({"event_type": "pong"}, {"event_type": "progress", "step": "agent_1"},
                     {"event_type": "completed", "result_key": f"results/{JOB}/extraction.json"},
                     {"event_type": "failed", "failure_reason": "x", "error_code": ERR_INPUT}):
            check_outbound({**base, **good})
        for bad in ({"event_type": "done"}, {"event_type": "pong", "extra": 1}, {"event_type": "progress", "step": ""},
                    {"event_type": "completed", "result_key": "clean/x.json"},
                    {"event_type": "failed", "failure_reason": "x" * 301, "error_code": ERR_INPUT}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_outbound({**base, **bad})

    def test_keepalive_repeats_step_before_backend_timeout(self):
        clock = [0.0]
        sent = []
        partner = PartnerProtocol(type("Q", (), {"send": lambda self, body: sent.append(json.loads(body))})(), None,
                                  BUCKET, clock=lambda: clock[0])
        partner.progress(JOB, "agent_1")
        partner.progress(JOB, "agent_1")  # same step: not repeated
        partner.keepalive(JOB)
        clock[0] = KEEPALIVE_SECONDS
        partner.keepalive(JOB)
        self.assertEqual([m["step"] for m in sent], ["agent_1", "agent_1"])
        with self.assertRaises(ValueError):
            WorkerSettings(self.requests_url, "jobs", "other-bucket", input_bucket=BUCKET, results_queue_url=self.results_url)


if __name__ == "__main__":
    unittest.main()
