import json
import unittest
import httpx
from workers.connectors.http import request_json
from workers.connectors.langfuse import LangfuseClient, progress_events
from workers.connectors.input_processing import InputProcessingClient
from workers.contracts import DownloadedFile, TransientRemoteError
from workers.schemas import QueueJob


IDENTITY = {"job_id": "j", "runtime_id": "r", "contract_id": "c", "trace_id": "t"}


def observation(**changes):
    return {"id": "obs", "traceId": "t", "endTime": "2026-11-07T01:00:00Z",
            "metadata": {"job_id": "j", "runtime_id": "r", "contract_id": "c",
                         "worker_event": {"sequence": 1, "stage": "agent_3", "state": "completed", "progress": 95}},
            "output": json.dumps({"agent_id": "agent_3_recommendation", "text": "result"}), **changes}


class ConnectorTests(unittest.TestCase):
    def test_v2_pagination_including_empty_page_and_explicit_auth(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json={"data": [], "meta": {"cursor": "next"}})
            return httpx.Response(200, json={"data": [observation()], "meta": {"cursor": None}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = LangfuseClient("https://traces.test", "pk", "sk", client=http)
            rows = client.observations("t", "2026-11-07T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(calls[0].url.path, "/api/public/v2/observations")
        self.assertEqual(calls[0].url.params["traceId"], "t")
        self.assertEqual(calls[1].url.params["cursor"], "next")
        self.assertIn("toStartTime", calls[0].url.params)
        self.assertIn("worker_event", calls[0].url.params["expandMetadata"])
        self.assertEqual(calls[0].headers["authorization"], "Basic cGs6c2s=")

    def test_repeated_cursor_rejected(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [], "meta": {"cursor": "again"}}))) as http:
            with self.assertRaises(ValueError):
                LangfuseClient("https://traces.test", "pk", "sk", client=http).observations("t", "2026-11-07T00:00:00Z")

    def test_only_bound_explicit_final_event_completes(self):
        self.assertEqual(progress_events([observation(traceId="other")], IDENTITY), [])
        self.assertEqual(progress_events([observation(metadata={})], IDENTITY), [])
        self.assertEqual(progress_events([observation(endTime=None)], IDENTITY), [])
        self.assertEqual(progress_events([observation()], IDENTITY)[0]["result"]["text"], "result")
        bad = observation()
        bad["metadata"]["runtime_id"] = "wrong"
        with self.assertRaises(ValueError):
            progress_events([bad], IDENTITY)
        for output in (None, "plain model text", json.dumps({"agent_id": "agent_2_risk_scoring", "text": "x"})):
            with self.subTest(output=output), self.assertRaises((ValueError, TypeError)):
                progress_events([observation(output=output)], IDENTITY)

    def test_duplicate_source_sequences_must_agree(self):
        changed = observation(id="different")
        with self.assertRaises(ValueError):
            progress_events([observation(), changed], IDENTITY)

    def test_http_failures_bounded_response_and_redirect_do_not_leak_bodies(self):
        for status in (401, 302, 429, 503):
            with self.subTest(status=status), httpx.Client(transport=httpx.MockTransport(
                    lambda _, status=status: httpx.Response(status, text="secret-payload"))) as client:
                with self.assertRaises(ValueError if status < 400 or status == 401 else TransientRemoteError) as caught:
                    request_json(client, "GET", "https://example.test")
                self.assertNotIn("secret-payload", str(caught.exception))
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 100))) as client:
            with self.assertRaises(ValueError):
                request_json(client, "GET", "https://example.test", max_bytes=10)

    def test_processing_ack_must_match_identity(self):
        job = QueueJob.from_body({"job_id": "j", "contract_id": "c", "s3_bucket": "input", "s3_key": "x.txt"})
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(202, json={"status": "accepted", "job_id": "other"}))) as http:
            client = InputProcessingClient("http://processing/jobs", token="secret", client=http)
            with self.assertRaises(ValueError):
                client.submit(job, "r", DownloadedFile(b"text", "x.txt"))
