"""Langfuse delivery: one bad event never blocks the rest, every reason for silence is visible."""
import contextlib
import io
import json
import os
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from monitoring.langfuse_exporter import LangfuseExporter
from monitoring.service import create_app

TOKEN = "t" * 40
KEYS = {"LANGFUSE_ENABLED": "true", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}


def event(stage):
    return {"event_id": os.urandom(16).hex(), "timestamp_ns": time.time_ns(), "service": "worker", "stage": stage,
            "status": "running", "job_id": "job-1", "trace_id": "a" * 32}


class LangfuseDeliveryTests(unittest.TestCase):
    def run_collector(self, reply, stages, env=KEYS, until=lambda sent, ready: len(sent) >= 2):
        """Post `stages` to the real collector while Langfuse answers with reply(stage); -> (sent, readyz, output)."""
        sent, out = [], io.StringIO()

        def handler(request):
            name = json.loads(request.content)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"]
            response = reply(name)
            if response.status_code < 300:
                sent.append(name)
            return response
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with TemporaryDirectory() as data, patch.dict(os.environ, {"DATA_DIR": data, "INTERNAL_API_TOKEN": TOKEN,
                "MONITORING_URL": "", "ALERT_WEBHOOK_URL": "", **env}), contextlib.redirect_stdout(out), \
                patch("monitoring.service.LangfuseExporter", lambda: LangfuseExporter(client=client)), \
                patch("monitoring.service.EXPORT_INTERVAL", 0.02), patch("monitoring.service.EXPORT_RETRY", 0.02), \
                TestClient(create_app()) as http:
            for stage in stages:
                http.post("/v1/events", json=event(stage), headers={"Authorization": "Bearer " + TOKEN})
            deadline, ready = time.monotonic() + 5, {}
            while time.monotonic() < deadline:
                ready = http.get("/readyz").json()
                if until(sent, ready):
                    break
                time.sleep(0.02)
        return sent, ready, out.getvalue()

    def test_a_rejected_event_does_not_block_the_events_behind_it(self):
        sent, _, out = self.run_collector(
            lambda name: httpx.Response(400 if name == "worker.poison" else 200), ["poison", "downloading", "agent_1"],
            until=lambda sent, ready: "worker.agent_1" in sent)
        self.assertTrue({"worker.downloading", "worker.agent_1"} <= set(sent))
        self.assertIn("HTTP 400", out)

    def test_partial_success_counts_as_rejected_and_any_2xx_as_accepted(self):
        def reply(name):
            return httpx.Response(207, json={"partialSuccess": {"rejectedSpans": 1 if name == "worker.poison" else 0}})
        sent, _, _ = self.run_collector(reply, ["poison", "agent_2"], until=lambda sent, ready: "worker.agent_2" in sent)
        self.assertIn("worker.agent_2", sent)

    def test_wrong_keys_or_region_are_reported_and_events_are_kept(self):
        sent, ready, out = self.run_collector(lambda name: httpx.Response(401), ["downloading"],
                                              until=lambda sent, ready: ready.get("langfuse_error"))
        self.assertEqual(sent, [])
        self.assertIn("HTTP 401", ready["langfuse_error"])
        self.assertIn("LANGFUSE_BASE_URL", ready["langfuse_error"])
        self.assertGreaterEqual(ready["langfuse_pending"], 1)  # nothing lost: sent once the keys are right
        self.assertEqual(out.count('"HTTP 401'), 1)  # one line per distinct problem, not one per retry

    def test_disabled_or_missing_keys_say_why(self):
        for env, reason in (({**KEYS, "LANGFUSE_ENABLED": "false"}, "LANGFUSE_ENABLED"),
                            ({**KEYS, "LANGFUSE_SECRET_KEY": ""}, "LANGFUSE_SECRET_KEY")):
            with self.subTest(reason=reason):
                _, ready, out = self.run_collector(lambda name: httpx.Response(200), [], env=env,
                                                   until=lambda sent, ready: True)
                self.assertFalse(ready["langfuse_active"])
                self.assertIn(reason, ready["langfuse_error"])
                self.assertIn(reason, out)


if __name__ == "__main__":
    unittest.main()
