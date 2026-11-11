"""SQLite persistence (offline mode; ADR-004). Repository API without business logic.

The store only reads and writes rows. Rules live in :mod:`pms.rules`, the state machine
and permission checks in :mod:`pms.workflow`, and the audit chain in :mod:`pms.audit`
(which shares this connection, so a state change and its audit record commit in one
transaction). Callers that change state should go through the workflow or the rule
engine - they write the matching audit record.

IDs are readable and sequential per kind (``SIG-00001``, ``TASK-00001`` ...). JSON
columns are decoded into Python objects; boolean columns into ``bool``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterator, Sequence

from .audit import AuditLog
from .models import (
    AgentRun, Alert, Approval, Case, Clock, CorrelationRecord, CostRecord, DeadlineRow, Decision, Fact, Finding,
    MetadataEdit, Recommendation, Signal, Submission, SystemClock, Task, iso_utc,
)

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY, case_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
    critical INTEGER NOT NULL DEFAULT 0, severity TEXT NOT NULL, urgency TEXT NOT NULL DEFAULT '{}',
    rule_sets TEXT NOT NULL DEFAULT '[]', authority_labels TEXT NOT NULL DEFAULT '[]',
    route_unverified INTEGER NOT NULL DEFAULT 0, rs_notes TEXT NOT NULL DEFAULT '[]',
    incident_states TEXT NOT NULL DEFAULT '[]', rs_basis TEXT, awareness_anchor TEXT, anchor_signal_id TEXT,
    anchor_ref TEXT, product_family TEXT, metadata TEXT NOT NULL DEFAULT '{}',
    analysis_state TEXT NOT NULL DEFAULT 'PENDING', deadline_waiver TEXT, content_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY, case_id TEXT, ref TEXT NOT NULL, source TEXT NOT NULL, filename TEXT,
    received_at TEXT NOT NULL, text TEXT NOT NULL, text_sha256 TEXT NOT NULL, readable INTEGER NOT NULL,
    language TEXT, metadata TEXT NOT NULL DEFAULT '{}', relation TEXT, link TEXT NOT NULL DEFAULT '{}',
    registry TEXT NOT NULL DEFAULT '{}', injection TEXT NOT NULL DEFAULT '{}', crit TEXT NOT NULL DEFAULT '{}',
    extraction TEXT NOT NULL DEFAULT '{}', rule_sets TEXT NOT NULL DEFAULT '[]',
    deadline_not_reset INTEGER NOT NULL DEFAULT 0, completeness TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signals_case ON signals(case_id);
CREATE TABLE IF NOT EXISTS deadlines (
    deadline_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, position INTEGER NOT NULL, rule_set TEXT NOT NULL,
    basis TEXT NOT NULL, days INTEGER NOT NULL, anchor_utc TEXT NOT NULL, awareness_local_date TEXT NOT NULL,
    start_date TEXT NOT NULL, due_date TEXT NOT NULL, due_end_local TEXT NOT NULL, due_end_utc TEXT NOT NULL,
    origin_signal_id TEXT, sources TEXT NOT NULL DEFAULT '[]', stopped_by_submission_id TEXT, stopped_at TEXT,
    superseded TEXT, created_at TEXT NOT NULL, UNIQUE (case_id, rule_set, basis)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, case_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, created_by TEXT NOT NULL, closed_at TEXT,
    closed_by TEXT, close_reason TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, sent_at TEXT, channel TEXT, detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS metadata_edits (
    edit_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, field TEXT NOT NULL, old_value TEXT, new_value TEXT,
    reason TEXT NOT NULL, user_id TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, content_hash TEXT NOT NULL, content_version INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL, reason TEXT NOT NULL, ts TEXT NOT NULL, valid INTEGER NOT NULL,
    voided_at TEXT, voided_reason TEXT
);
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, approval_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    channel_label TEXT NOT NULL, reference TEXT NOT NULL, submitted_on TEXT NOT NULL, receipt_status TEXT NOT NULL,
    recorded_by TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, reason_code TEXT,
    actor_id TEXT NOT NULL, status_before TEXT NOT NULL, status_after TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT, case_id TEXT, status TEXT NOT NULL,
    summary TEXT NOT NULL, content_hash TEXT, reviewer_id TEXT, reason TEXT, data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS costs (
    cost_id TEXT PRIMARY KEY, case_id TEXT, run_id TEXT, agent_id TEXT NOT NULL, model TEXT,
    input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, price_table_version TEXT, simulated INTEGER NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, signal_id TEXT, agent_id TEXT NOT NULL, status TEXT NOT NULL,
    model TEXT, model_version TEXT, prompt_version TEXT, agent_version TEXT, started_at TEXT, finished_at TEXT,
    output TEXT, error TEXT, usage TEXT, draft INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS recommendations (
    rec_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL,
    content_hash TEXT NOT NULL, draft INTEGER NOT NULL DEFAULT 1, run_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, signal_id TEXT, name TEXT NOT NULL, value TEXT,
    quote TEXT, line INTEGER, verified INTEGER NOT NULL, origin TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS correlations (
    corr_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, signal_id TEXT, result TEXT NOT NULL,
    corpus_version TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class _Table:
    name: str
    pk: str
    prefix: str
    record: type
    json_cols: frozenset[str] = frozenset()
    bool_cols: frozenset[str] = frozenset()
    order_by: str = ""


_TABLES: dict[str, _Table] = {
    t.name: t for t in (
        _Table("cases", "case_id", "CASE", Case,
               frozenset({"urgency", "rule_sets", "authority_labels", "rs_notes", "incident_states", "metadata",
                          "deadline_waiver"}), frozenset({"critical", "route_unverified"}), "created_at, case_id"),
        _Table("signals", "signal_id", "SIG", Signal,
               frozenset({"metadata", "link", "registry", "injection", "crit", "extraction", "rule_sets",
                          "completeness"}), frozenset({"readable", "deadline_not_reset"}), "received_at, signal_id"),
        _Table("deadlines", "deadline_id", "DL", DeadlineRow, frozenset({"sources", "superseded"}), frozenset(),
               "case_id, position"),
        _Table("tasks", "task_id", "TASK", Task, frozenset({"detail"}), frozenset(), "created_at, task_id"),
        _Table("alerts", "alert_id", "ALR", Alert, frozenset({"detail"}), frozenset(), "created_at, alert_id"),
        _Table("metadata_edits", "edit_id", "EDIT", MetadataEdit, frozenset({"old_value", "new_value"}), frozenset(),
               "ts, edit_id"),
        _Table("approvals", "approval_id", "APR", Approval, frozenset(), frozenset({"valid"}), "ts, approval_id"),
        _Table("submissions", "submission_id", "SUB", Submission, frozenset(), frozenset(), "ts, submission_id"),
        _Table("decisions", "decision_id", "DEC", Decision, frozenset(), frozenset(), "ts, decision_id"),
        _Table("findings", "finding_id", "FND", Finding, frozenset({"data"}), frozenset(), "created_at, finding_id"),
        _Table("costs", "cost_id", "COST", CostRecord, frozenset(), frozenset({"simulated"}), "ts, cost_id"),
        _Table("agent_runs", "run_id", "RUN", AgentRun, frozenset({"output", "usage"}), frozenset({"draft"}),
               "run_id"),
        _Table("recommendations", "rec_id", "REC", Recommendation, frozenset({"content"}), frozenset({"draft"}),
               "case_id, version"),
        _Table("facts", "fact_id", "FACT", Fact, frozenset({"value"}), frozenset({"verified"}), "fact_id"),
        _Table("correlations", "corr_id", "CORR", CorrelationRecord, frozenset({"result"}), frozenset(),
               "created_at, corr_id"),
    )
}


class NotFound(KeyError):
    """A requested row does not exist."""


class Store:
    """SQLite store. ``path=":memory:"`` gives a fresh in-memory database (golden run, tests)."""

    def __init__(self, path: str | Path = ":memory:", *, clock: Clock | None = None) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0
        self.audit = AuditLog(self.conn, clock=self.clock, lock=self.lock)
        with self.lock:
            self.conn.executescript(_SCHEMA)
            self.audit.init_schema()
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """Re-entrant write transaction (``BEGIN IMMEDIATE`` ... ``COMMIT``/``ROLLBACK``)."""
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")

    def now(self) -> str:
        return iso_utc(self.clock.now(), ms=True)

    def next_id(self, prefix: str) -> str:
        with self.transaction():
            row = self.conn.execute("SELECT value FROM counters WHERE name = ?", (prefix,)).fetchone()
            value = (row[0] if row else 0) + 1
            self.conn.execute("INSERT OR REPLACE INTO counters VALUES (?, ?)", (prefix, value))
        return f"{prefix}-{value:05d}"

    @staticmethod
    def _encode(table: _Table, col: str, value: Any) -> Any:
        if col in table.json_cols:
            return None if value is None and col in ("deadline_waiver", "superseded", "output", "usage") else json.dumps(
                value, ensure_ascii=False, default=str)
        if col in table.bool_cols:
            return 1 if value else 0
        return value

    @staticmethod
    def _decode(table: _Table, row: sqlite3.Row | tuple, cols: Sequence[str]) -> Any:
        data: dict[str, Any] = {}
        for col, value in zip(cols, row):
            if col in table.json_cols and value is not None:
                value = json.loads(value)
            elif col in table.bool_cols:
                value = bool(value)
            data[col] = value
        return table.record(**data)

    def _cols(self, table: _Table) -> list[str]:
        return [f.name for f in fields(table.record)]

    def _insert(self, table_name: str, values: dict[str, Any]) -> Any:
        table = _TABLES[table_name]
        cols = self._cols(table)
        with self.transaction():
            if not values.get(table.pk):
                values[table.pk] = self.next_id(table.prefix)
            unknown = set(values) - set(cols)
            if unknown:
                raise ValueError(f"unknown columns for {table_name}: {sorted(unknown)}")
            present = [c for c in cols if c in values]
            self.conn.execute(
                f"INSERT INTO {table.name} ({','.join(present)}) VALUES ({','.join('?' * len(present))})",
                [self._encode(table, c, values[c]) for c in present],
            )
        return self._get(table_name, values[table.pk])

    def _get(self, table_name: str, pk: str) -> Any:
        table = _TABLES[table_name]
        cols = self._cols(table)
        with self.lock:
            row = self.conn.execute(f"SELECT {','.join(cols)} FROM {table.name} WHERE {table.pk} = ?", (pk,)).fetchone()
        if row is None:
            raise NotFound(f"{table_name} {pk} not found")
        return self._decode(table, row, cols)

    def _update(self, table_name: str, pk: str, values: dict[str, Any]) -> Any:
        table = _TABLES[table_name]
        cols = self._cols(table)
        unknown = set(values) - set(cols) | ({table.pk} & set(values))
        if unknown:
            raise ValueError(f"cannot update columns {sorted(unknown)} of {table_name}")
        if values:
            with self.transaction():
                cur = self.conn.execute(
                    f"UPDATE {table.name} SET {','.join(f'{c} = ?' for c in values)} WHERE {table.pk} = ?",
                    [self._encode(table, c, v) for c, v in values.items()] + [pk],
                )
                if cur.rowcount == 0:
                    raise NotFound(f"{table_name} {pk} not found")
        return self._get(table_name, pk)

    def _list(self, table_name: str, where: dict[str, Any] | None = None) -> list[Any]:
        table = _TABLES[table_name]
        cols = self._cols(table)
        clauses, params = [], []
        for col, value in (where or {}).items():
            if col not in cols:
                raise ValueError(f"unknown filter column {col} for {table_name}")
            if value is None:
                continue
            clauses.append(f"{col} = ?")
            params.append(self._encode(table, col, value))
        sql = f"SELECT {','.join(cols)} FROM {table.name}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if table.order_by:
            sql += f" ORDER BY {table.order_by}"
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._decode(table, r, cols) for r in rows]

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction():
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------------ cases
    def insert_case(self, **values: Any) -> Case:
        now = self.now()
        values.setdefault("created_at", now)
        values.setdefault("updated_at", now)
        return self._insert("cases", values)

    def get_case(self, case_id: str) -> Case:
        return self._get("cases", case_id)

    def find_case_by_key(self, case_key: str) -> Case | None:
        found = self._list("cases", {"case_key": case_key})
        return found[0] if found else None

    def list_cases(self, *, status: str | None = None, critical: bool | None = None) -> list[Case]:
        return self._list("cases", {"status": status, "critical": critical})

    def update_case(self, case_id: str, **values: Any) -> Case:
        values.setdefault("updated_at", self.now())
        return self._update("cases", case_id, values)

    def case_key_available(self, case_key: str) -> bool:
        return self.find_case_by_key(case_key) is None and not self._list("cases", {"case_id": case_key})

    # ------------------------------------------------------------------ signals
    def insert_signal(self, **values: Any) -> Signal:
        values.setdefault("created_at", self.now())
        return self._insert("signals", values)

    def get_signal(self, signal_id: str) -> Signal:
        return self._get("signals", signal_id)

    def update_signal(self, signal_id: str, **values: Any) -> Signal:
        return self._update("signals", signal_id, values)

    def list_signals(self, case_id: str | None = None) -> list[Signal]:
        return self._list("signals", {"case_id": case_id})

    # ------------------------------------------------------------------ deadlines
    def upsert_deadline(self, case_id: str, position: int, values: dict[str, Any]) -> DeadlineRow:
        """Insert a candidate or refresh its computed fields; keeps stop/supersede state."""
        with self.transaction():
            existing = [d for d in self._list("deadlines", {"case_id": case_id})
                        if d.rule_set == values["rule_set"] and d.basis == values["basis"]]
            if existing:
                keep = {k: v for k, v in values.items() if k not in ("origin_signal_id", "rule_set", "basis")}
                return self._update("deadlines", existing[0].deadline_id, keep)
            return self._insert("deadlines", {**values, "case_id": case_id, "position": position,
                                              "created_at": values.get("created_at") or self.now()})

    def list_deadlines(self, case_id: str) -> list[DeadlineRow]:
        return self._list("deadlines", {"case_id": case_id})

    def update_deadline(self, deadline_id: str, **values: Any) -> DeadlineRow:
        return self._update("deadlines", deadline_id, values)

    # ------------------------------------------------------------------ tasks and alerts
    def insert_task(self, **values: Any) -> Task:
        values.setdefault("created_at", self.now())
        values.setdefault("status", "OPEN")
        values.setdefault("detail", {})
        return self._insert("tasks", values)

    def get_task(self, task_id: str) -> Task:
        return self._get("tasks", task_id)

    def update_task(self, task_id: str, **values: Any) -> Task:
        return self._update("tasks", task_id, values)

    def list_tasks(self, *, case_id: str | None = None, status: str | None = None, kind: str | None = None) -> list[Task]:
        return self._list("tasks", {"case_id": case_id, "status": status, "kind": kind})

    def insert_alert(self, **values: Any) -> Alert:
        values.setdefault("created_at", self.now())
        values.setdefault("status", "PENDING")
        values.setdefault("detail", {})
        return self._insert("alerts", values)

    def update_alert(self, alert_id: str, **values: Any) -> Alert:
        return self._update("alerts", alert_id, values)

    def list_alerts(self, *, case_id: str | None = None, status: str | None = None) -> list[Alert]:
        return self._list("alerts", {"case_id": case_id, "status": status})

    # ------------------------------------------------------------------ human records
    def insert_metadata_edit(self, **values: Any) -> MetadataEdit:
        return self._insert("metadata_edits", values)

    def list_metadata_edits(self, case_id: str | None = None) -> list[MetadataEdit]:
        return self._list("metadata_edits", {"case_id": case_id})

    def insert_approval(self, **values: Any) -> Approval:
        return self._insert("approvals", values)

    def update_approval(self, approval_id: str, **values: Any) -> Approval:
        return self._update("approvals", approval_id, values)

    def list_approvals(self, case_id: str | None = None) -> list[Approval]:
        return self._list("approvals", {"case_id": case_id})

    def valid_approval(self, case_id: str) -> Approval | None:
        valid = [a for a in self._list("approvals", {"case_id": case_id}) if a.valid]
        return valid[-1] if valid else None

    def insert_submission(self, **values: Any) -> Submission:
        return self._insert("submissions", values)

    def list_submissions(self, case_id: str | None = None) -> list[Submission]:
        return self._list("submissions", {"case_id": case_id})

    def insert_decision(self, **values: Any) -> Decision:
        return self._insert("decisions", values)

    def list_decisions(self, *, case_id: str | None = None, kind: str | None = None) -> list[Decision]:
        return self._list("decisions", {"case_id": case_id, "kind": kind})

    # ------------------------------------------------------------------ findings
    def insert_finding(self, **values: Any) -> Finding:
        now = self.now()
        values.setdefault("created_at", now)
        values.setdefault("updated_at", now)
        values.setdefault("data", {})
        return self._insert("findings", values)

    def get_finding(self, finding_id: str) -> Finding:
        return self._get("findings", finding_id)

    def update_finding(self, finding_id: str, **values: Any) -> Finding:
        values.setdefault("updated_at", self.now())
        return self._update("findings", finding_id, values)

    def list_findings(self, *, kind: str | None = None, status: str | None = None, case_id: str | None = None) -> list[Finding]:
        return self._list("findings", {"kind": kind, "status": status, "case_id": case_id})

    # ------------------------------------------------------------------ agents, costs, recommendations
    def insert_agent_run(self, **values: Any) -> AgentRun:
        return self._insert("agent_runs", values)

    def update_agent_run(self, run_id: str, **values: Any) -> AgentRun:
        return self._update("agent_runs", run_id, values)

    def get_agent_run(self, run_id: str) -> AgentRun:
        return self._get("agent_runs", run_id)

    def list_agent_runs(self, case_id: str | None = None) -> list[AgentRun]:
        return self._list("agent_runs", {"case_id": case_id})

    def insert_cost(self, **values: Any) -> CostRecord:
        values.setdefault("ts", self.now())
        return self._insert("costs", values)

    def list_costs(self, case_id: str | None = None) -> list[CostRecord]:
        return self._list("costs", {"case_id": case_id})

    def insert_recommendation(self, **values: Any) -> Recommendation:
        values.setdefault("created_at", self.now())
        return self._insert("recommendations", values)

    def list_recommendations(self, case_id: str) -> list[Recommendation]:
        return self._list("recommendations", {"case_id": case_id})

    def latest_recommendation(self, case_id: str) -> Recommendation | None:
        recs = self.list_recommendations(case_id)
        return recs[-1] if recs else None

    # ------------------------------------------------------------------ facts and correlations
    def insert_fact(self, **values: Any) -> Fact:
        values.setdefault("created_at", self.now())
        return self._insert("facts", values)

    def list_facts(self, case_id: str) -> list[Fact]:
        return self._list("facts", {"case_id": case_id})

    def insert_correlation(self, **values: Any) -> CorrelationRecord:
        values.setdefault("created_at", self.now())
        return self._insert("correlations", values)

    def list_correlations(self, case_id: str | None = None, signal_id: str | None = None) -> list[CorrelationRecord]:
        return self._list("correlations", {"case_id": case_id, "signal_id": signal_id})

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        with self.lock:
            self.conn.close()
