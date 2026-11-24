"""Durable sanitized event collector, metrics, and optional Langfuse outbox."""
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from threading import Event, RLock, Thread
from fastapi import Request, HTTPException
from common.service_http import base_app, serve, read_json
from monitoring.client import FIELDS
from monitoring.langfuse_exporter import LangfuseExporter, LangfuseRejected, LangfuseUnavailable
from monitoring import alerts as alert_rules
from monitoring.metrics import summarize
from monitoring.tracing import timeline
import time


SERVICES = {"worker", "input-processing", "agent-1", "agent-2", "agent-3", "langgraph", "monitoring", "config", "mcp",
            "data-collector", "data-processor"}  # data pipeline runs on the host (systemd), posts via 127.0.0.1


EXPORT_INTERVAL, EXPORT_RETRY = 2, 5  # seconds between export rounds / after a failed one


def sanitize(event):
    if event.get("service") not in SERVICES or not re.fullmatch(r"[a-f0-9]{32}", event.get("event_id", "")):
        raise ValueError("Invalid service/event_id")
    for field in ("stage", "status"):
        if not isinstance(event.get(field), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", event[field]):
            raise ValueError("Invalid stage/status")
    if type(event.get("timestamp_ns")) is not int or event["timestamp_ns"] <= 0:
        raise ValueError("Invalid timestamp")
    clean = {key: event[key] for key in ("event_id", "timestamp_ns", "service", "stage", "status")}
    for key in FIELDS:
        if key not in event:
            continue
        value = event[key]
        if key in {"duration_ms", "progress", "retry", "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
                   "reasoning_tokens", "count"}:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid metric")
        elif not isinstance(value, str) or len(value) > 256:
            raise ValueError("Invalid correlation field")
        clean[key] = value
    if "trace_id" in clean and not re.fullmatch(r"[0-9a-f]{32}", clean["trace_id"]):
        raise ValueError("Invalid trace_id")
    return clean


class EventStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self.lock = RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, service TEXT, trace_id TEXT, payload TEXT, exported INTEGER DEFAULT 0)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                created_ns INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)""")

    def add(self, event):
        event = sanitize(event)
        with self.lock, self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO events(id,service,trace_id,payload) VALUES(?,?,?,?)",
                (event["event_id"], event["service"], event.get("trace_id"), json.dumps(event))).rowcount
            if inserted:  # replayed events never re-alert
                for alert in alert_rules.evaluate(event, count_recent_throttles=self.count_throttles):
                    if self.db.execute("INSERT OR IGNORE INTO alerts(key,payload,created_ns) VALUES(?,?,?)",
                                       (alert["key"], json.dumps(alert), event["timestamp_ns"])).rowcount:
                        print(alert_rules.log_line(alert), flush=True)
        return event

    def count_throttles(self, since_ns):
        return self.db.execute("SELECT count(*) FROM events WHERE json_extract(payload,'$.error_type')='TransientModelError' "
                               "AND json_extract(payload,'$.timestamp_ns')>=?", (since_ns,)).fetchone()[0]

    def alerts(self, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT payload, delivered FROM alerts ORDER BY created_ns DESC LIMIT ?", (limit,)).fetchall()
        return [{**json.loads(payload), "delivered": bool(delivered)} for payload, delivered in rows]

    def undelivered_alerts(self):
        with self.lock:
            return [(key, json.loads(payload)) for key, payload in
                    self.db.execute("SELECT key, payload FROM alerts WHERE delivered=0 ORDER BY created_ns LIMIT 20")]

    def mark_delivered(self, key):
        with self.lock, self.db:
            self.db.execute("UPDATE alerts SET delivered=1 WHERE key=?", (key,))

    def purge(self, retention_days, *, langfuse_active, webhook_active):
        """Drop telemetry older than the retention window, never rows still waiting for an exporter."""
        cutoff = time.time_ns() - int(retention_days * 86400 * 1_000_000_000)
        with self.lock, self.db:
            events = self.db.execute("DELETE FROM events WHERE json_extract(payload,'$.timestamp_ns')<? "
                                     "AND (exported<>0 OR ?=0)", (cutoff, int(langfuse_active))).rowcount
            self.db.execute("DELETE FROM alerts WHERE created_ns<? AND (delivered=1 OR ?=0)", (cutoff, int(webhook_active)))
        return events

    def query(self, trace_id=None):
        with self.lock:
            if trace_id:
                rows = self.db.execute("SELECT payload FROM events WHERE trace_id=? ORDER BY rowid DESC LIMIT 1000", (trace_id,)).fetchall()
            else:
                rows = self.db.execute("SELECT payload FROM events ORDER BY rowid DESC LIMIT 1000").fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM events WHERE exported=0").fetchone()[0]

    def metrics(self, window_minutes=60):
        pending = self.pending()
        with self.lock:
            counts = dict(self.db.execute("SELECT service,count(*) FROM events GROUP BY service"))
            # shortcut: aggregates the newest 50k events in Python; add indexed columns if volume outgrows this.
            recent = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM events ORDER BY rowid DESC LIMIT 50000")]
        return {"events_by_service": counts, "pending_export": pending,
                **summarize(recent, now_ns=time.time_ns(), window_minutes=window_minutes)}


def create_app():
    store = EventStore(Path(os.getenv("DATA_DIR", "/data")) / "events.sqlite3")
    exporter, stop = LangfuseExporter(), Event()
    notifier = alert_rules.WebhookNotifier()
    retention_days = float(os.getenv("MONITORING_RETENTION_DAYS", "30"))
    def maintenance_loop():
        last_purge = 0.0
        while not stop.wait(2):
            for key, alert in (store.undelivered_alerts() if notifier.active else []):
                try:
                    notifier.send(alert)
                    store.mark_delivered(key)
                except Exception:
                    print('{"service":"monitoring","stage":"alert_webhook","status":"retry"}', flush=True)
                    break
            if time.monotonic() - last_purge >= 3600:
                last_purge = time.monotonic()
                removed = store.purge(retention_days, langfuse_active=exporter.active, webhook_active=notifier.active)
                print(json.dumps({"service": "monitoring", "stage": "retention", "status": "completed", "removed": removed}), flush=True)
    def report(error):
        """One log line per distinct Langfuse problem (and one when it clears), never one per retry."""
        if error != exporter.error:
            exporter.error = error
            print(json.dumps({"service": "monitoring", "stage": "langfuse_export",
                              "status": "retry" if error else "recovered", "error": error}), flush=True)
    def mark(event_id, exported):  # 1 = accepted by Langfuse, 2 = rejected for good
        with store.lock, store.db:
            store.db.execute("UPDATE events SET exported=? WHERE id=?", (exported, event_id))
    if not exporter.active:
        print(json.dumps({"service": "monitoring", "stage": "langfuse_export", "status": "inactive", "error": exporter.error}), flush=True)
    def export_loop():
        while not stop.wait(EXPORT_INTERVAL):
            if not exporter.active:
                continue
            with store.lock:
                rows = store.db.execute("SELECT id,payload FROM events WHERE exported=0 ORDER BY rowid LIMIT 100").fetchall()
            for event_id, payload in rows:
                if stop.is_set():
                    break
                try:
                    exporter.export(json.loads(payload))
                    mark(event_id, 1)
                    report(None)
                except (LangfuseRejected, KeyError, ValueError, TypeError) as exc:  # this event only: go on with the next
                    reason = str(exc) if isinstance(exc, LangfuseRejected) else type(exc).__name__
                    print(json.dumps({"service": "monitoring", "stage": "langfuse_export", "status": "rejected",
                                      "event_id": event_id, "error": reason}), flush=True)
                    mark(event_id, 2)
                except Exception as exc:  # outage/keys: keep the outbox for later; remote bodies are never printed
                    report(str(exc) if isinstance(exc, LangfuseUnavailable) else type(exc).__name__)
                    stop.wait(EXPORT_RETRY)
                    break
    thread = Thread(target=export_loop, daemon=True)
    maintenance_thread = Thread(target=maintenance_loop, daemon=True)
    @asynccontextmanager
    async def lifespan(app):
        from monitoring.client import emit
        store.add(emit("monitoring", "service", "started"))
        thread.start()
        maintenance_thread.start()
        yield
        stop.set()
        thread.join(timeout=15)
        maintenance_thread.join(timeout=15)
        if not any(t.is_alive() for t in (thread, maintenance_thread)):
            exporter.close()
            notifier.close()
            store.db.close()
    app = base_app("monitoring", lifespan=lifespan, ready=lambda: {
        "langfuse_active": exporter.active, "langfuse_error": exporter.error, "langfuse_pending": store.pending(),
        "alert_webhook": notifier.active, "retention_days": retention_days})
    @app.post("/v1/events")
    async def event(request: Request):
        value = await read_json(request, maximum=16 * 1024)
        try:
            saved = store.add(value)
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid telemetry event") from None
        print(json.dumps(saved), flush=True)
        return {"accepted": True, "event_id": saved["event_id"]}
    @app.get("/v1/events")
    def events(trace_id: str | None = None):
        return {"events": store.query(trace_id)}
    @app.get("/v1/metrics")
    def metrics(window_minutes: float = 60):
        if not 0 < window_minutes <= 10080:
            raise HTTPException(400, "window_minutes must be in (0, 10080]")
        return store.metrics(window_minutes)
    @app.get("/v1/traces/{trace_id}")
    def trace(trace_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
            raise HTTPException(400, "Invalid trace_id")
        view = timeline(store.query(trace_id))
        if view is None:
            raise HTTPException(404, "Unknown trace_id")
        return view
    @app.get("/v1/alerts")
    def alerts(limit: int = 100):
        return {"alerts": store.alerts(max(1, min(limit, 500)))}
    return app


if __name__ == "__main__":
    serve(create_app, "monitoring")
