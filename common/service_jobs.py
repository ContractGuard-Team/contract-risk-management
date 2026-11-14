"""Durable HTTP jobs with atomic ownership, replay, progress and restart recovery.

SQLite volumes are single-host storage. Claims are fenced across replicas sharing
the same local volume; no claim of exactly-once remote/model execution is made.
"""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Event, RLock, Thread
import time
from uuid import uuid4
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from common.service_client import call_key
from common.service_http import base_app, read_json, identity
from monitoring.client import emit


# Failures worth running again when the caller resubmits the same idempotent job.
RETRYABLE_JOB_ERRORS = ("RemoteServiceError", "TimeoutError", "TransientModelError")


class JobConflict(ValueError):
    pass


class JobStore:
    def __init__(self, path, *, clock=time.time, lease_seconds=60):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.lock, self.clock, self.lease_seconds = RLock(), clock, lease_seconds
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
              key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL, stage TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
              result TEXT, error_type TEXT, owner TEXT, lease REAL NOT NULL DEFAULT 0,
              attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL)""")

    def submit(self, key, payload):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        fingerprint = call_key(payload)
        now = self.clock()
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(key,fingerprint,payload,status,stage,created,updated) VALUES(?,?,?,'queued','queued',?,?)",
                            (key, fingerprint, encoded, now, now))
            row = self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
            if row["fingerprint"] != fingerprint:
                raise JobConflict("Idempotency key is bound to another payload")
            # A retryable service failure can be resubmitted by the queue owner.
            if row["status"] == "failed" and row["error_type"] in RETRYABLE_JOB_ERRORS and row["attempts"] < 3:
                self.db.execute("UPDATE jobs SET status='queued',stage='retry',error_type=NULL,updated=? WHERE key=?", (now, key))
        return self.get(key)

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return {"key": key, "identity": payload["identity"], "status": row["status"], "stage": row["stage"],
                "progress": row["progress"], "attempts": row["attempts"], "error_type": row["error_type"],
                "result": json.loads(row["result"]) if row["result"] is not None else None}

    def claim(self):
        now, owner = self.clock(), uuid4().hex
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute("SELECT * FROM jobs WHERE status='queued' OR (status='running' AND lease<?) ORDER BY created LIMIT 1", (now,)).fetchone()
                if row:
                    self.db.execute("UPDATE jobs SET status='running',owner=?,lease=?,attempts=attempts+1,updated=? WHERE key=?",
                                    (owner, now + self.lease_seconds, now, row["key"]))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return (row["key"], owner, json.loads(row["payload"])) if row else None

    def renew(self, key, owner):
        now = self.clock()
        with self.lock, self.db:
            cursor = self.db.execute("UPDATE jobs SET lease=? WHERE key=? AND owner=? AND status='running' AND lease>=?",
                                     (now + self.lease_seconds, key, owner, now))
        if cursor.rowcount != 1:
            raise RuntimeError("Internal job lease lost")

    def update(self, key, owner, *, stage, progress, status="running", result=None, error_type=None):
        now = self.clock()
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
        with self.lock, self.db:
            cursor = self.db.execute("""UPDATE jobs SET stage=?, progress=max(progress,?), status=?, result=?,
                 error_type=?, updated=? WHERE key=? AND owner=? AND status='running' AND lease>=?""",
                 (stage, progress, status, encoded, error_type, now, key, owner, now))
        if cursor.rowcount != 1:
            raise RuntimeError("Stale internal job update")

    def close(self):
        self.db.close()


class JobRunner:
    def __init__(self, store, handler, service):
        self.store, self.handler, self.service = store, handler, service
        self.stop = Event()
        self.thread = Thread(target=self._loop, name=service + "-jobs", daemon=True)

    def _run(self, key, owner, payload):
        done, errors = Event(), []
        started = time.monotonic()
        maximum = float(os.getenv("JOB_TIMEOUT_SECONDS", "3600"))
        def renew():
            while not done.wait(max(0.1, self.store.lease_seconds / 3)):
                try:
                    self.store.renew(key, owner)
                except Exception as exc:
                    errors.append(exc)
                    return
        def guard():
            if errors:
                raise RuntimeError("Internal lease renewal failed")
            if time.monotonic() - started > maximum:
                raise TimeoutError("Internal execution deadline exceeded")
        def progress(stage, percentage):
            guard()
            if not isinstance(stage, str) or len(stage) > 128 or type(percentage) is not int or not 0 <= percentage <= 95:
                raise ValueError("Invalid progress update")
            self.store.update(key, owner, stage=stage, progress=percentage)
            emit(self.service, stage, "running", progress=percentage, **payload["identity"])
        heartbeat = Thread(target=renew, daemon=True)
        heartbeat.start()
        emit(self.service, "job", "started", **payload["identity"])
        try:
            result = self.handler(payload, progress, guard)
            guard()
            if not isinstance(result, dict):
                raise ValueError("Job result must be a JSON object")
            self.store.update(key, owner, stage="completed", progress=100, status="completed", result=result)
            emit(self.service, "job", "completed", duration_ms=(time.monotonic()-started)*1000, **payload["identity"])
        except Exception as exc:
            try:
                self.store.update(key, owner, stage="failed", progress=0, status="failed", error_type=type(exc).__name__)
            except Exception:
                pass
            emit(self.service, "job", "failed", error_type=type(exc).__name__, duration_ms=(time.monotonic()-started)*1000, **payload["identity"])
        finally:
            done.set()
            heartbeat.join(timeout=2)

    def _loop(self):
        while not self.stop.is_set():
            try:
                claimed = self.store.claim()
                if claimed:
                    self._run(*claimed)
                    continue
            except Exception as exc:
                emit(self.service, "job_runner", "failed", error_type=type(exc).__name__)
            self.stop.wait(0.2)


def job_app(service, handler, *, validate=lambda _: None, ready=lambda: {}, describe=None):
    store = JobStore(Path(os.getenv("DATA_DIR", "/data")) / (service + "-jobs.sqlite3"))
    runner = JobRunner(store, handler, service)
    @asynccontextmanager
    async def lifespan(app):
        runner.thread.start()
        emit(service, "service", "started")
        yield
        runner.stop.set()
        runner.thread.join(timeout=5)
        if not runner.thread.is_alive():
            store.close()
    def readiness():
        if not runner.thread.is_alive():
            raise RuntimeError("Job runner unavailable")
        return ready()
    app = base_app(service, lifespan=lifespan, ready=readiness)
    app.state.store = store

    @app.post("/v1/jobs", status_code=202)
    async def submit(request: Request):
        payload = await read_json(request)
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise HTTPException(400, "Idempotency-Key must be a SHA-256 hex string")
        try:
            payload["identity"] = identity(payload.get("identity"))
            validate(payload)
            ready()
            return store.submit(key, payload)
        except JobConflict:
            raise HTTPException(409, "Idempotency conflict") from None
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "Invalid job schema or configuration") from None

    @app.get("/v1/jobs/{key}")
    def status(key: str):
        row = store.get(key)
        if row is None:
            raise HTTPException(404, "Job not found")
        return row

    if describe:
        app.add_api_route("/v1/describe", describe, methods=["GET"])
    return app
