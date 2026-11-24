"""Cancel and final failure: worker -> services over real HTTP, the two DynamoDB tables and the results queue."""
import json
import os
import socket
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

import boto3
from fastapi.testclient import TestClient
from moto import mock_aws

from common.exceptions import JobCancelled
from common.schemas.business.outbound import AnalysisResult
from common.service_client import RemoteServiceError, ServiceClient
from common.service_http import base_app
from common.service_jobs import job_app
from workers import cancel
from workers.connectors.s3_client import S3Client
from workers.connectors.sqs_client import SQSClient
from workers.partner import ERR_CANCELLED, ERR_INPUT, ERR_PROCESSING, PartnerProtocol
from workers.runtime.contract_job_store import ContractJobStore
from workers.runtime.intake_worker import IntakeWorker
from workers.runtime.progress_worker import ProgressWorker
from workers.settings import WorkerSettings
from test_dynamodb_progress import REGION, all_events, make_tables
from test_worker_partner import BUCKET, CONTRACT_ID, JOB, REQUEST, FakeProcessor, FakeWorkflow, outbound_errors

TOKEN = "cancel-test-internal-token-" * 2
IDENTITY = {"job_id": "j", "runtime_id": "r", "contract_id": "c", "trace_id": "a" * 32, "request_id": "a" * 32}


class Served:
    """A job service on a real localhost port."""
    def __init__(self, case, name, handler):
        import uvicorn
        with patch.dict(os.environ, {"DATA_DIR": case.dir.name}):
            self.app = job_app(name, handler)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_config=None))
        thread = Thread(target=self.server.run, daemon=True)
        thread.start()
        while not self.server.started:
            time.sleep(0.01)
        def stop():
            self.server.should_exit = True
            thread.join(10)
        case.addCleanup(stop)

    def statuses(self):
        return {row[0] for row in self.app.state.store.db.execute("SELECT status FROM jobs")}


class ServiceCancelTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.dir.cleanup)
        env = patch.dict(os.environ, {"INTERNAL_API_TOKEN": TOKEN, "MONITORING_URL": ""})
        env.start(); self.addCleanup(env.stop)

    def test_cancel_cascades_frees_the_runner_and_is_final(self):
        calling_model, release, stopped = Event(), Event(), []
        self.addCleanup(release.set)

        def agent(payload, progress, guard):  # like an agent stuck inside one model call
            if payload.get("quick"):
                return {"ok": True}
            calling_model.set()
            release.wait(20)
            try:
                guard()
            except JobCancelled:
                stopped.append("agent")
                raise
            return {"ok": True}
        inner = Served(self, "agent-1", agent)

        def graph(payload, progress, guard):
            client = ServiceClient(inner.url)
            try:
                return client.run_job({"identity": payload["identity"]}, guard=guard)
            finally:
                client.close()
        outer = Served(self, "langgraph", graph)
        client = ServiceClient(outer.url); self.addCleanup(client.close)

        def guard():  # the worker's guard once the user pressed cancel
            if calling_model.is_set():
                raise JobCancelled("Cancelled by user")
        started = time.monotonic()
        with self.assertRaises(JobCancelled):
            client.run_job({"identity": IDENTITY}, guard=guard)
        while inner.statuses() != {"cancelled"} and time.monotonic() < started + 5:
            time.sleep(0.05)
        self.assertEqual((outer.statuses(), inner.statuses()), ({"cancelled"}, {"cancelled"}))
        # The stuck call is still running, yet the agent takes the next contract at once.
        direct = ServiceClient(inner.url); self.addCleanup(direct.close)
        self.assertEqual(direct.run_job({"identity": IDENTITY, "quick": True}), {"ok": True})
        self.assertLess(time.monotonic() - started, 5)
        release.set()
        while not stopped and time.monotonic() < started + 10:
            time.sleep(0.05)
        self.assertEqual(stopped, ["agent"])  # and stops at its next guard instead of calling the model again
        with self.assertRaises(JobCancelled):
            client.run_job({"identity": IDENTITY})  # resubmitting never restarts it

    def test_cancel_endpoint_leaves_finished_and_unknown_jobs_alone(self):
        service = Served(self, "agent-2", lambda payload, progress, guard: {"ok": True})
        client = ServiceClient(service.url); self.addCleanup(client.close)
        client.run_job({"identity": IDENTITY})
        [key] = [row[0] for row in service.app.state.store.db.execute("SELECT key FROM jobs")]
        self.assertEqual(client.request("POST", f"/v1/jobs/{key}/cancel")["status"], "completed")
        with self.assertRaises(RemoteServiceError):
            client.request("POST", "/v1/jobs/" + "0" * 64 + "/cancel")

    def test_agent_stops_before_the_next_model_call(self):
        from agents.hermes.agent_factory import create_agent
        from common.schemas.agent_messages import AgentRequest
        calls = []
        class Provider:
            def generate(self, *args, **kwargs):
                calls.append(1)
        agent = create_agent("agent_1_classification", provider=Provider())
        def guard():
            raise JobCancelled("Cancelled by user")
        with self.assertRaises(JobCancelled):
            agent.run(AgentRequest("contract", {}), guard=guard)
        self.assertEqual(calls, [])


class WorkerOutcomeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": REGION,
                                      "MONITORING_URL": "", "INTERNAL_API_TOKEN": TOKEN})
        env.start(); self.addCleanup(env.stop)
        aws = mock_aws(); aws.start(); self.addCleanup(aws.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=BUCKET)
        self.s3.put_object(Bucket=BUCKET, Key=REQUEST["source_file_key"], Body=b"%PDF-1.7 contract")
        self.sqs = boto3.client("sqs", region_name=REGION)
        self.urls = {name: self.sqs.create_queue(QueueName=name)["QueueUrl"] for name in ("requests", "results", "cancels")}
        self.tables = make_tables()
        self.store = ContractJobStore(self.tables)
        storage = S3Client(client=self.s3)
        self.queue = SQSClient(self.urls["requests"], client=self.sqs)
        self.partner = PartnerProtocol(SQSClient(self.urls["results"], client=self.sqs), storage, BUCKET)
        self.settings = WorkerSettings(self.urls["requests"], "jobs", BUCKET, input_bucket=BUCKET,
                                       results_queue_url=self.urls["results"], events_table_name="events")
        # Like production: the progress process has its own store and learns contracts from the events.
        self.writer = ProgressWorker(ContractJobStore(self.tables), storage, BUCKET)

    def worker(self, workflow=None, processor=None, partner=True):
        return IntakeWorker(self.queue, self.store, S3Client(client=self.s3), self.writer.handle, self.settings,
                            processor=processor or FakeProcessor(), workflow=workflow or FakeWorkflow(),
                            partner=self.partner if partner else None)

    def message(self, job=JOB, attempts=1, request=None):
        request = request or {**REQUEST, "job_id": job, "result_key": f"results/{job}/extraction.json"}
        self.s3.put_object(Bucket=BUCKET, Key=f"results/{job}/request.json", Body=json.dumps(request).encode())
        self.sqs.send_message(QueueUrl=self.urls["requests"], MessageBody=json.dumps(
            {"message_version": "1", "event_type": "analysis_requested", "job_id": job,
             "request_key": f"results/{job}/request.json"}))
        return {**self.queue.receive()[0], "Attributes": {"ApproximateReceiveCount": str(attempts)}}

    def answers(self, *types):
        found = self.sqs.receive_message(QueueUrl=self.urls["results"], MaxNumberOfMessages=10).get("Messages", [])
        for m in found:
            self.sqs.delete_message(QueueUrl=self.urls["results"], ReceiptHandle=m["ReceiptHandle"])
        sent = [json.loads(m["Body"]) for m in found]
        self.assertEqual([outbound_errors(m) for m in sent], [[]] * len(sent))
        return [m for m in sent if m["event_type"] in types]

    def requests_left(self):
        values = self.sqs.get_queue_attributes(QueueUrl=self.urls["requests"], AttributeNames=["All"])["Attributes"]
        return int(values["ApproximateNumberOfMessages"]) + int(values["ApproximateNumberOfMessagesNotVisible"])

    def row(self):
        return self.store.client.physical(CONTRACT_ID)

    def test_success_leaves_one_log_and_the_completed_row(self):
        self.assertEqual(self.worker().handle(self.message()), "completed")
        self.assertEqual([(e["job_id"], e["status"]) for e in all_events(self.tables)], [(JOB, "completed")])
        row = self.row()
        self.assertEqual((row["location"], row["status"], row["active_job_id"]), ("worker", "completed", JOB))
        self.assertEqual(row["output"]["key"], REQUEST["result_key"])

    def test_cancel_stops_the_running_contract_and_the_next_one_is_processed(self):
        seen = []
        class Stuck:  # LangGraph that would run for an hour; guard is the worker's real heartbeat guard
            def execute(inner, contract, identity, guard, on_progress):
                on_progress({"status": "running", "stage": "agent_2", "progress": 60})
                seen.append(self.store.client.get(JOB)["location"])
                self.assertEqual(self.store.cancel(JOB), "cancelled")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    guard()
                    time.sleep(0.05)
                raise AssertionError("not cancelled")
        started = time.monotonic()
        self.assertEqual(self.worker(Stuck()).handle(self.message()), "cancelled")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(seen, ["agent_2"])
        [answer] = self.answers("failed", "completed")
        self.assertEqual((answer["error_code"], answer["failure_reason"]), (ERR_CANCELLED, "Cancelled by user"))
        [log] = all_events(self.tables)
        self.assertEqual((log["status"], log["location"], log["contract_id"]), ("cancelled", "agent_2", CONTRACT_ID))
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertEqual(self.requests_left(), 0)
        self.assertNotIn(REQUEST["result_key"], {o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET)["Contents"]})
        # Same contract, next job: the row is reset and processed normally.
        self.assertEqual(self.worker().handle(self.message("job-next")), "completed")
        self.assertEqual((self.row()["active_job_id"], self.row()["status"]), ("job-next", "completed"))

    def test_cancel_before_the_message_arrives(self):
        self.store.cancel(JOB)
        processor = FakeProcessor()
        self.assertEqual(self.worker(processor=processor).handle(self.message()), "cancelled")
        self.assertEqual((processor.calls, self.requests_left()), (0, 0))
        self.assertEqual([m["error_code"] for m in self.answers("failed")], [ERR_CANCELLED])

    def test_cancel_between_the_result_and_its_log_is_still_a_cancel(self):
        class Late(FakeWorkflow):
            def execute(inner, *args, **kwargs):
                result = super().execute(*args, **kwargs)
                self.store.cancel(JOB)
                return result
        with patch("workers.runtime.heartbeat.CANCEL_CHECK_SECONDS", 3600):  # the guard does not notice in time
            self.assertEqual(self.worker(Late()).handle(self.message()), "cancelled")
        self.assertEqual([e["status"] for e in all_events(self.tables)], ["cancelled"])
        self.assertEqual(self.row()["status"], "cancelled")
        self.assertEqual([m["event_type"] for m in self.answers("failed", "completed")], ["failed"])

    def test_retries_then_error_output_log_and_answer(self):
        broken = FakeWorkflow(RemoteServiceError("Remote job failed: ModelResponseError"))
        first = self.message()
        with self.assertRaises(RemoteServiceError):
            self.worker(broken).handle(first)
        self.assertEqual((self.row()["status"], all_events(self.tables)), ("retrying", []))
        self.assertEqual(self.answers("failed"), [])
        time.sleep(1.1)  # the released lease is compared in whole seconds
        last = {**first, "Attributes": {"ApproximateReceiveCount": "3"}}
        self.assertEqual(self.worker(broken).handle(last), "failed")
        [answer] = self.answers("failed")
        self.assertEqual(answer, {"message_version": "1", "event_type": "failed", "job_id": JOB, "error_code": ERR_PROCESSING,
                                  "failure_reason": "Remote job failed: ModelResponseError"})
        [log] = all_events(self.tables)
        self.assertEqual((log["status"], log["location"], int(log["attempts"])), ("failed", "langgraph", 3))
        self.assertEqual((self.row()["status"], self.requests_left()), ("failed", 0))
        # The error output: every section of the result schema, null when missing.
        output = json.loads(self.s3.get_object(Bucket=BUCKET, Key=log["output"]["key"])["Body"].read())
        self.assertEqual(log["output"]["key"], REQUEST["result_key"])
        self.assertEqual(output.keys(), AnalysisResult.model_fields.keys())
        self.assertEqual({k: v for k, v in output.items() if v is not None},
                         {"schema_version": "2.0.0", "job_id": JOB, "contract_id": CONTRACT_ID, "contract_version": "2",
                          "document_complete": False})

    def test_bad_request_is_logged_and_answered_without_a_contract_row(self):
        self.assertEqual(self.worker().handle(self.message(request={**REQUEST, "tenant_id": ""})), "failed")
        [log] = all_events(self.tables)
        self.assertEqual((log["job_id"], log["status"]), (JOB, "failed"))
        self.assertIsNone(self.row())
        self.assertEqual([m["error_code"] for m in self.answers("failed")], [ERR_INPUT])

    def test_internal_message_format_stops_after_the_last_attempt_too(self):
        self.sqs.send_message(QueueUrl=self.urls["requests"], MessageBody=json.dumps(
            {"job_id": "plain", "contract_id": "c-plain", "bucket": BUCKET, "source_file_key": REQUEST["source_file_key"]}))
        message = {**self.queue.receive()[0], "Attributes": {"ApproximateReceiveCount": "3"}}
        self.assertEqual(self.worker(FakeWorkflow(RuntimeError("x")), partner=False).handle(message), "failed")
        [log] = all_events(self.tables)
        output = json.loads(self.s3.get_object(Bucket=BUCKET, Key=log["output"]["key"])["Body"].read())
        self.assertEqual((output["result"]["job_id"], output["result"]["clauses"], self.requests_left()), ("plain", None, 0))

    def test_monitoring_hears_every_outcome(self):
        with patch("workers.runtime.intake_worker.emit") as emit:
            self.worker().handle(self.message())
            self.store.cancel("job-c")
            self.worker().handle(self.message("job-c"))
            self.worker(FakeWorkflow(RuntimeError("x"))).handle(self.message("job-f", attempts=3))
            self.worker().handle(self.message("job-bad", request={**REQUEST, "tenant_id": ""}))
        told = [(c.kwargs["job_id"], c.args[2]) for c in emit.call_args_list if c.args[1] in ("pipeline", "completed")]
        self.assertEqual(told, [(JOB, "completed"), ("job-f", "failed"), ("job-bad", "failed")])
        self.assertTrue(all(len(c.kwargs["trace_id"]) == 32 for c in emit.call_args_list))

    def test_http_and_queue_call_the_same_function(self):
        app = base_app("worker")
        cancel.attach(app, self.store)
        with TestClient(app) as http:
            self.assertEqual(http.post("/v1/jobs/job-http/cancel").status_code, 401)
            auth = {"Authorization": "Bearer " + TOKEN}
            self.assertEqual(http.post("/v1/jobs/job-http/cancel", headers=auth).json(),
                             {"job_id": "job-http", "status": "cancelled"})
            self.assertEqual(http.post("/v1/jobs/%20/cancel", headers=auth).status_code, 400)
        queue, stop = SQSClient(self.urls["cancels"], client=self.sqs), Event()
        for body in ("not json", json.dumps({"message_version": "1", "event_type": "cancel_requested", "job_id": "job-sqs"})):
            self.sqs.send_message(QueueUrl=self.urls["cancels"], MessageBody=body)
            for message in queue.receive():
                cancel.handle(queue, self.store, message)
        self.assertEqual({e["job_id"] for e in all_events(self.tables)}, {"job-http", "job-sqs"})
        self.assertEqual(queue._connect().receive_message(QueueUrl=self.urls["cancels"]).get("Messages", []), [])
        with patch.object(queue, "receive", side_effect=[RuntimeError("SQS down"), []]) as receive, \
                patch.object(stop, "wait", side_effect=lambda _: stop.set()), self.assertLogs("workers.cancel", "ERROR"):
            cancel.serve_queue(queue, self.store, stop)
        receive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
