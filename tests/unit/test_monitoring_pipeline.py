"""Monitoring of the legal data pipeline + terminal logging. Worker forwarding stays contract-only."""
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
import warnings
from unittest.mock import patch

from monitoring import alerts
from monitoring.logging import configure
from monitoring.service import EventStore, sanitize


def event(**fields):
    return {"event_id": os.urandom(16).hex(), "timestamp_ns": time.time_ns(), "service": "data-collector",
            "stage": "run", "status": "completed", **fields}


class PipelineEventsTests(unittest.TestCase):
    def test_pipeline_events_are_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.sqlite3")
            saved = store.add(event(stage="process", status="done", document="61/2020/QH14", count=3,
                                    trace_id="a" * 32, duration_ms=1200.5))
            self.assertEqual((saved["document"], saved["count"]), ("61/2020/QH14", 3))
            self.assertIn("data-collector.process", store.metrics()["stages"])
            store.db.close()
        with self.assertRaises(ValueError):
            sanitize(event(service="data-unknown"))
        with self.assertRaises(ValueError):
            sanitize(event(count=-1))

    def test_alert_rules(self):
        def rules(e):
            return [a["rule"] for a in alerts.evaluate(e, count_recent_throttles=lambda _: 0)]
        self.assertEqual(rules(event(status="incomplete", trace_id="b" * 32)), ["data_run_incomplete"])
        self.assertEqual(rules(event(status="failed", error_type="ConnectError")), ["data_run_failed"])
        self.assertEqual(rules(event(stage="process", status="failed", document="X")), ["data_document_failed"])
        self.assertEqual(rules(event(stage="sqs_message", status="kept")), ["data_sqs_retry"])
        self.assertEqual(rules(event(duration_ms=5 * 3600 * 1000)), [])  # a full crawl is long by design
        self.assertEqual(rules(event(stage="process", status="completed", duration_ms=3600 * 1000)), ["slow_stage"])


class TerminalLoggingTests(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self.saved = (root.handlers[:], root.level, {n: logging.getLogger(n).level for n in ("httpx", "mcp", "boto3")})

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:], level, quiet = self.saved
        root.setLevel(level)
        for name, value in quiet.items():
            logging.getLogger(name).setLevel(value)

    def log_error(self, mode, details):
        stream = io.StringIO()
        with patch.dict(os.environ, {"LOG_FORMAT": mode}):
            configure("data-collector", error_details=details, stream=stream)
        try:
            raise ValueError("Điều 5 không đọc được")
        except ValueError:
            logging.getLogger("data_pipeline").exception("document failed")
        return stream.getvalue()

    def test_text_format_shows_where_and_traceback(self):
        text = self.log_error("text", True)
        self.assertIn("ERROR   data-collector data_pipeline: document failed", text)
        self.assertIn("ValueError at ", text)
        self.assertIn("test_monitoring_pipeline.py", text)
        self.assertIn("Điều 5 không đọc được", text)  # public data: message + traceback shown

    def test_json_for_contract_services_keeps_location_but_not_message(self):
        entry = json.loads(self.log_error("json", False))
        self.assertEqual(entry["error_type"], "ValueError")
        self.assertIn("test_monitoring_pipeline.py", entry["error_at"])
        self.assertTrue(entry["stack"])
        self.assertNotIn("Điều 5", json.dumps(entry, ensure_ascii=False))


class PipelineReportTests(unittest.TestCase):
    def test_a_crawl_run_reports_one_trace_with_document_and_process_events(self):
        import asyncio
        from functools import partial
        import boto3
        from moto import mock_aws
        from qdrant_client import QdrantClient
        from types import SimpleNamespace
        from data_pipeline.collector import collect
        from data_pipeline.ingestion.qdrant_writer import QdrantWriter
        from data_pipeline.ingestion.s3_archiver import S3Archiver
        from data_pipeline.processor import process_files
        from test_data_pipeline import BASE_LAW, BUCKET, FakeEmbedder, FakeSource, MemoryStore, doc

        sent = []
        with mock_aws(), patch("data_pipeline.telemetry.emit", side_effect=lambda *a, **k: sent.append((a, k))), \
                patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "SERVICE_NAME": "data-collector"}), \
                self.assertLogs("data_pipeline", level="INFO") as logs:
            s3 = boto3.client("s3", region_name="ap-southeast-1")
            s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"})
            writer = QdrantWriter(client=QdrantClient(":memory:"), collection="legal_documents", dimensions=8)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # local Qdrant ignores payload indexes
                writer.ensure_indexes()
            ctx = SimpleNamespace(store=MemoryStore(), embedder=FakeEmbedder(), qdrant=writer, today=lambda: "2026-01-01",
                                  extract=lambda files: {"pages": [{"page": 1, "method": "pdf_text",
                                                                    "text": files[0][1].decode()[9:]}],
                                                         "method": "text", "warnings": []})
            base = doc("1", "61/2020/QH14", "Luật Đầu tư", "Luật", "2021-01-01", BASE_LAW)
            asyncio.run(collect("full", source=FakeSource(dict([base])), store=ctx.store,
                                archiver=S3Archiver(BUCKET, client=s3), today="2026-01-01",
                                process=partial(process_files, ctx)))
        stages = [(a[1], a[2]) for a, _ in sent]
        self.assertEqual(stages, [("run", "started"), ("listing", "completed"), ("document", "collected"),
                                  ("process", "done"), ("run", "completed")])
        self.assertEqual(len({k["trace_id"] for _, k in sent}), 1)  # whole run = one trace in monitoring
        self.assertTrue(all(a[0] == "data-collector" and k["echo"] is False for a, k in sent))
        self.assertEqual(sent[3][1]["document"], "61/2020/QH14")
        self.assertTrue(any("[process done] 61/2020/QH14 v1 -> Supabase + Qdrant" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
