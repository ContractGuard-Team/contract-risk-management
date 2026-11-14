"""Durable local handoff from worker HTTP ingress to progress process."""
import json
from pathlib import Path
import sqlite3
from threading import RLock
import time

from monitoring.service import sanitize
from common.service_http import identity


class MonitoringInbox:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self.lock = RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS monitoring_inbox (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0)""")

    def add(self, event):
        clean = sanitize(event)
        identity({key: clean.get(key) for key in ("job_id", "runtime_id", "contract_id", "trace_id", "request_id")})
        encoded = json.dumps(clean, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO monitoring_inbox(id,payload) VALUES(?,?)", (clean["event_id"], encoded))
            saved = self.db.execute("SELECT payload FROM monitoring_inbox WHERE id=?", (clean["event_id"],)).fetchone()
            if saved[0] != encoded:
                raise ValueError("Monitoring event_id is bound to a different payload")
        return clean["event_id"]

    def next(self):
        with self.lock:
            row = self.db.execute("SELECT id,payload,attempts FROM monitoring_inbox WHERE status='pending' AND retry_at<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def finish(self, event_id, status="delivered"):
        with self.lock, self.db:
            self.db.execute("UPDATE monitoring_inbox SET status=? WHERE id=?", (status, event_id))

    def retry(self, event_id, attempts):
        with self.lock, self.db:
            self.db.execute("UPDATE monitoring_inbox SET attempts=attempts+1,retry_at=? WHERE id=?",
                            (time.time() + min(60, 2 ** min(attempts, 6)), event_id))

    def close(self):
        self.db.close()


def attach_ingress(app, inbox):
    from fastapi import Request, HTTPException
    from common.service_http import read_json
    from starlette.concurrency import run_in_threadpool

    @app.post("/v1/monitoring-events", status_code=202)
    async def receive(request: Request):
        event = await read_json(request, maximum=16 * 1024)
        try:
            event_id = await run_in_threadpool(inbox.add, event)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Invalid job monitoring event") from None
        return {"accepted": True, "event_id": event_id, "persistence": "worker_inbox"}
