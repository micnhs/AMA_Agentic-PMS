"""Durable analysis-job queue on the SQLite store (offline implementation of :class:`pms.interfaces.JobQueue`).

One job per received signal (and per human re-analysis request). Jobs survive restarts; a job left
RUNNING by a crashed worker is re-queued by :meth:`SqliteJobQueue.recover_stale` (T25). Claiming
is atomic (``UPDATE ... WHERE status = 'QUEUED'``), so an in-process worker thread and a separate
worker process on the same database never run a job twice. Instantiated only by :mod:`pms.runtime`.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from .interfaces import Job
from .models import iso_utc, parse_utc
from .store import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, signal_id TEXT NOT NULL, status TEXT NOT NULL,
    reason TEXT NOT NULL, enqueued_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, progress TEXT NOT NULL DEFAULT '{}',
    rounds INTEGER NOT NULL DEFAULT 0, worker TEXT
);
CREATE INDEX IF NOT EXISTS analysis_jobs_status ON analysis_jobs(status, enqueued_at);
CREATE INDEX IF NOT EXISTS analysis_jobs_case ON analysis_jobs(case_id);
"""
_COLS = ("job_id", "case_id", "signal_id", "status", "reason", "enqueued_at", "started_at", "finished_at", "attempts",
         "error", "progress", "rounds", "worker")


class SqliteJobQueue:
    def __init__(self, store: Store) -> None:
        self.store = store
        with store.lock:
            store.conn.executescript(_SCHEMA)

    def _row(self, row: tuple) -> Job:
        data = dict(zip(_COLS, row))
        data["progress"] = json.loads(data["progress"] or "{}")
        return Job(**data)

    def _now(self) -> str:
        return iso_utc(self.store.clock.now(), ms=True)

    def enqueue(self, case_id: str, signal_id: str, *, reason: str) -> Job:
        with self.store.transaction():
            open_job = self.store.conn.execute(
                f"SELECT {','.join(_COLS)} FROM analysis_jobs WHERE signal_id = ? AND status IN ('QUEUED','RUNNING')"
                " ORDER BY job_id LIMIT 1", (signal_id,)).fetchone()
            if open_job is not None:
                return self._row(open_job)
            job_id = self.store.next_id("JOB")
            self.store.conn.execute(
                "INSERT INTO analysis_jobs (job_id, case_id, signal_id, status, reason, enqueued_at, progress)"
                " VALUES (?,?,?,?,?,?,?)", (job_id, case_id, signal_id, "QUEUED", reason, self._now(), "{}"))
        return self.get(job_id)

    def claim_next(self, worker_id: str) -> Job | None:
        with self.store.transaction():
            row = self.store.conn.execute(
                "SELECT job_id FROM analysis_jobs WHERE status = 'QUEUED' ORDER BY enqueued_at, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = self.store.conn.execute(
                "UPDATE analysis_jobs SET status = 'RUNNING', started_at = ?, attempts = attempts + 1, worker = ?"
                " WHERE job_id = ? AND status = 'QUEUED'", (self._now(), worker_id, row[0]))
            if cur.rowcount != 1:
                return None
        return self.get(row[0])

    def update_progress(self, job_id: str, progress: dict[str, Any], *, rounds: int | None = None) -> None:
        with self.store.transaction():
            if rounds is None:
                self.store.conn.execute("UPDATE analysis_jobs SET progress = ? WHERE job_id = ?",
                                        (json.dumps(progress, ensure_ascii=False, default=str), job_id))
            else:
                self.store.conn.execute("UPDATE analysis_jobs SET progress = ?, rounds = ? WHERE job_id = ?",
                                        (json.dumps(progress, ensure_ascii=False, default=str), rounds, job_id))

    def finish(self, job_id: str, status: str, *, error: str | None = None) -> Job:
        if status not in ("DONE", "FAILED", "SKIPPED"):
            raise ValueError(f"invalid final job status {status}")
        with self.store.transaction():
            self.store.conn.execute("UPDATE analysis_jobs SET status = ?, finished_at = ?, error = ? WHERE job_id = ?",
                                    (status, self._now(), error, job_id))
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        with self.store.lock:
            row = self.store.conn.execute(f"SELECT {','.join(_COLS)} FROM analysis_jobs WHERE job_id = ?",
                                          (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"job {job_id} not found")
        return self._row(row)

    def list(self, *, case_id: str | None = None, status: str | None = None) -> list[Job]:
        sql = f"SELECT {','.join(_COLS)} FROM analysis_jobs WHERE 1=1"
        params: list[Any] = []
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        with self.store.lock:
            rows = self.store.conn.execute(sql + " ORDER BY enqueued_at, job_id", params).fetchall()
        return [self._row(r) for r in rows]

    def pending_count(self) -> int:
        with self.store.lock:
            return int(self.store.conn.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0])

    def recover_stale(self, older_than: _dt.timedelta) -> int:
        """Re-queue RUNNING jobs whose worker vanished (restart without lost work)."""
        limit = self.store.clock.now() - older_than
        recovered = 0
        with self.store.transaction():
            for job in [j for j in self.list(status="RUNNING") if j.started_at and parse_utc(j.started_at) <= limit]:
                self.store.conn.execute(
                    "UPDATE analysis_jobs SET status = 'QUEUED', error = ? WHERE job_id = ? AND status = 'RUNNING'",
                    (f"re-queued after worker interruption (attempt {job.attempts})", job.job_id))
                recovered += 1
        return recovered
