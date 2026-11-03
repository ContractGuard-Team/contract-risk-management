"""SQLite cache for one process/worker; share this instance to serialize duplicate job submissions."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock

from common.schemas.contract import canonical


class ContractCache:
    def __init__(self, path=":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = RLock()
        self._jobs = {}
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS entries (
                run_key TEXT NOT NULL, stage TEXT NOT NULL, item_key TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(run_key, stage, item_key))""")

    @contextmanager
    def job_lock(self, key):
        with self._lock:
            lock = self._jobs.setdefault(key, RLock())
        with lock:
            yield

    def begin(self, key, fingerprint):
        with self._lock, self._db:
            existing = self._db.execute("SELECT fingerprint FROM runs WHERE key=?", (key,)).fetchone()
            if existing and existing[0] != fingerprint:
                raise ValueError("Job already exists with different input/configuration; use a new job_id")
            self._db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?)", (key, fingerprint))

    def get(self, key, stage, item="result"):
        with self._lock:
            row = self._db.execute("SELECT value FROM entries WHERE run_key=? AND stage=? AND item_key=?",
                                   (key, stage, item)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, stage, value, item="result"):
        serialized = canonical(value)
        with self._lock, self._db:
            self._db.execute("INSERT INTO entries VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                             (key, stage, item, serialized))
            row = self._db.execute("SELECT value FROM entries WHERE run_key=? AND stage=? AND item_key=?",
                                   (key, stage, item)).fetchone()
            if row[0] != serialized:
                raise ValueError("Attempted to overwrite an immutable cached result")

    def close(self):
        self._db.close()
