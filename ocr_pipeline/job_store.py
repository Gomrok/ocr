"""Persist OCR job ownership and progress separately from user files."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

ACTIVE_STATES = ("queued", "running")
RETENTION_SECONDS = 24 * 60 * 60


class JobStore:
    def __init__(self, root: Path):
        self.path = root / "jobs.sqlite3"

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS jobs ("
                    "id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,"
                    "updated REAL NOT NULL, payload TEXT NOT NULL)"
                )
                connection.execute("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, updated)")
                connection.execute("CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, updated)")
                yield connection
        finally:
            connection.close()

    def save(self, job_id, job):
        payload = {k: v for k, v in job.items() if not k.endswith("_monotonic")}
        payload["log"] = payload.get("log", [])[-120:]
        with self._connection() as db:
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated=excluded.updated, payload=excluded.payload",
                (job_id, job["owner_id"], job["state"], time.time(),
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )

    def get(self, job_id, owner_id):
        if not owner_id:
            return None
        with self._connection() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=? AND owner=?", (job_id, owner_id)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self, owner_id):
        if not owner_id:
            return []
        with self._connection() as db:
            rows = db.execute(
                "SELECT id, payload FROM jobs WHERE owner=? ORDER BY updated DESC, id DESC LIMIT 40",
                (owner_id,),
            ).fetchall()
        return [(job_id, json.loads(payload)) for job_id, payload in rows]

    def pending(self):
        with self._connection() as db:
            rows = db.execute(
                "SELECT id, payload FROM jobs WHERE state IN (?, ?) ORDER BY updated, id",
                ACTIVE_STATES,
            ).fetchall()
        return [(job_id, json.loads(payload)) for job_id, payload in rows]

    def forget(self, job_id):
        with self._connection() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def expire(self):
        with self._connection() as db:
            db.execute("DELETE FROM jobs WHERE state NOT IN (?, ?) AND updated<?",
                       (*ACTIVE_STATES, time.time() - RETENTION_SECONDS))
