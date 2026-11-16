import contextlib
import io
import json
import logging
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import httpx

from monitoring import client as telemetry
from monitoring.alerts import WebhookNotifier
from monitoring.langfuse_exporter import otlp
from monitoring.logging import JsonFormatter
from monitoring.metrics import summarize
from monitoring.service import EventStore, create_app
from monitoring.tracing import timeline

TOKEN = "t" * 40
TRACE = "a" * 32


def event(service, stage, status, ts, **fields):
    return {"event_id": os.urandom(16).hex(), "timestamp_ns": ts, "service": service, "stage": stage,
            "status": status, "job_id": "job-1", "contract_id": "c-1", "trace_id": TRACE, **fields}


class ClientTests(unittest.TestCase):
    def test_emit_never_waits_for_a_hung_collector(self):
        with socket.socket() as hung:  # accepts TCP connections, never answers HTTP
            hung.bind(("127.0.0.1", 0))
            hung.listen(5)
            out = io.StringIO()
            with patch.dict(os.environ, {"MONITORING_URL": f"http://127.0.0.1:{hung.getsockname()[1]}",
                                         "INTERNAL_API_TOKEN": TOKEN}), contextlib.redirect_stdout(out):
                start = time.monotonic()
                telemetry.emit("agent-2", "job", "started", job_id="j")
                self.assertLess(time.monotonic() - start, 0.3)
                self.assertTrue(telemetry.flush(5))
            self.assertIn("collector_unavailable", out.getvalue())


class AggregationTests(unittest.TestCase):
    def test_metrics_latency_errors_tokens_failed_jobs(self):
        now = time.time_ns()
        events = [event("agent-2", "job", "completed", now - i, duration_ms=d) for i, d in enumerate((100, 200, 300, 400, 1000))]
        events += [event("agent-2", "job", "failed", now, error_type="TransientModelError", duration_ms=5),
                   event("agent-1", "agent_1_classification", "completed", now, input_tokens=20, output_tokens=10,
                         total_tokens=30, model="m"),
                   event("worker", "completed", "completed", now),
                   event("agent-2", "job", "completed", now - 2 * 3600 * 10**9, duration_ms=99999)]  # outside window
        m = summarize(events, now_ns=now, window_minutes=60)
        stage = m["stages"]["agent-2.job"]
        self.assertEqual((stage["completed"], stage["failed"], stage["max_ms"], stage["p95_ms"]), (5, 1, 1000, 1000))
        self.assertAlmostEqual(stage["error_rate"], 1 / 6, places=3)
        self.assertEqual(m["tokens"]["agent-1/m"], {"calls": 1, "input_tokens": 20, "output_tokens": 10, "total_tokens": 30})
        self.assertEqual(m["failed_jobs"][0]["error_type"], "TransientModelError")
        self.assertEqual((m["jobs"]["completed"], m["errors_by_type"]), (1, {"TransientModelError": 1}))

    def test_timeline_orders_services_and_finds_first_failure(self):
        base = 10**18
        view = timeline([event("worker", "processing", "running", base),
                         event("agent-2", "job", "failed", base + 3 * 10**9, error_type="ValueError", duration_ms=1000),
                         event("langgraph", "job", "failed", base + 4 * 10**9, error_type="RemoteServiceError")])
        self.assertEqual((view["status"], view["services"]), ("failed", ["worker", "agent-2", "langgraph"]))
        self.assertEqual((view["first_failure"]["service"], view["wall_ms"]), ("agent-2", 4000.0))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.store = EventStore(Path(self.dir.name) / "events.sqlite3")

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def test_alerts_dedupe_throttle_slow_and_replay(self):
        now = time.time_ns()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            first = event("agent-2", "job", "failed", now, error_type="ModelResponseError")
            self.store.add(first)
            self.store.add(first)  # replay
            self.store.add(event("langgraph", "job", "failed", now + 1, error_type="RemoteServiceError"))
            for i in range(3):
                self.store.add(event("agent-3", "agent_3_recommendation", "failed", now + 10 + i, error_type="TransientModelError"))
            self.store.add(event("mcp", "tool.qdrant_search", "completed", now + 20, duration_ms=2_000_000))
        rules = sorted(a["rule"] for a in self.store.alerts())
        self.assertEqual(rules, ["job_failed", "model_throttled", "slow_stage"])
        self.assertIn("agent-2", next(a for a in self.store.alerts() if a["rule"] == "job_failed")["summary"])
        self.assertEqual(out.getvalue().count('"alert"'), 3)

    def test_retention_keeps_rows_waiting_for_export(self):
        old = time.time_ns() - 40 * 86400 * 10**9
        stale = event("worker", "processing", "running", old)
        del stale["job_id"]  # not job-scoped: nothing waits on worker forwarding
        self.store.add(stale)
        self.assertEqual(self.store.purge(30, langfuse_active=True, webhook_active=False), 0)  # Langfuse not sent yet
        self.assertEqual(self.store.purge(30, langfuse_active=False, webhook_active=False), 1)

    def test_http_endpoints(self):
        from fastapi.testclient import TestClient
        env = {"DATA_DIR": self.dir.name + "/svc", "INTERNAL_API_TOKEN": TOKEN, "LANGFUSE_ENABLED": "false",
               "MONITORING_WORKER_ENABLED": "false", "MONITORING_URL": "", "ALERT_WEBHOOK_URL": ""}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), TestClient(create_app()) as http:
            auth = {"Authorization": "Bearer " + TOKEN}
            now = time.time_ns()
            for e in (event("worker", "processing", "running", now), event("agent-1", "job", "failed", now + 1, error_type="X")):
                self.assertEqual(http.post("/v1/events", json=e, headers=auth).status_code, 200)
            self.assertEqual(http.get(f"/v1/traces/{TRACE}", headers=auth).json()["status"], "failed")
            self.assertEqual(http.get("/v1/alerts", headers=auth).json()["alerts"][0]["rule"], "job_failed")
            metrics = http.get("/v1/metrics?window_minutes=5", headers=auth).json()
            self.assertEqual(metrics["failed_jobs"][0]["service"], "agent-1")
            self.assertEqual(http.get("/v1/metrics?window_minutes=0", headers=auth).status_code, 400)
            self.assertEqual(http.get(f"/v1/traces/{TRACE}").status_code, 401)
            self.assertEqual(http.get("/readyz").json()["retention_days"], 30.0)


class OutputFormatTests(unittest.TestCase):
    def test_webhook_https_only_and_text_payload(self):
        with self.assertRaises(ValueError):
            WebhookNotifier("http://hooks.example.test/x")
        sent = []
        transport = httpx.MockTransport(lambda r: sent.append(json.loads(r.content)) or httpx.Response(200))
        notifier = WebhookNotifier("https://hooks.example.test/x", client=httpx.Client(transport=transport))
        notifier.send({"severity": "error", "summary": "Job j failed", "rule": "job_failed", "key": "k"})
        self.assertEqual(sent[0]["text"], "[ERROR] Job j failed")

    def test_json_logs_hide_exception_text(self):
        try:
            raise ValueError("secret contract clause")
        except ValueError:
            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed %s", ("step",), __import__("sys").exc_info())
        line = json.loads(JsonFormatter("agent-1").format(record))
        self.assertEqual((line["service"], line["message"], line["error_type"]), ("agent-1", "failed step", "ValueError"))
        self.assertNotIn("secret contract clause", json.dumps(line))

    def test_langfuse_progress_is_event_and_failure_is_error(self):
        def attrs(e):
            span = otlp(e)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            return {a["key"]: a["value"]["stringValue"] for a in span["attributes"]}
        now = time.time_ns()
        self.assertEqual(attrs(event("worker", "processing", "running", now))["langfuse.observation.type"], "event")
        failed = attrs(event("agent-2", "job", "failed", now, error_type="ValueError", duration_ms=3))
        self.assertEqual((failed["langfuse.observation.type"], failed["langfuse.observation.level"],
                          failed["langfuse.observation.status_message"]), ("span", "ERROR", "ValueError"))


class ServiceObserverProgressTests(unittest.TestCase):
    def test_reflect_spans_do_not_move_stage_backwards(self):
        from monitoring.service_observer import ServiceObserver
        seen = []
        observer = ServiceObserver("langgraph", {"job_id": "j"}, lambda stage, pct: seen.append(stage))
        with patch("monitoring.service_observer.emit"):
            for name in ("agent_1_classification.chunk", "agent_3_recommendation.run", "agent_1_classification.reflect"):
                with observer.span(name):
                    pass
        self.assertEqual(seen, ["agent_1", "agent_3"])


if __name__ == "__main__":
    unittest.main()
