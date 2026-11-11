"""Append-only audit trail with a SHA-256 hash chain (concept section 8; design D-14).

Every status change, agent run, rule evaluation, human decision and metadata edit
writes exactly one record. Records are only appended: there is no update or delete API,
and SQLite triggers abort any ``UPDATE`` or ``DELETE`` on the audit table (ADR-004).

Each record contains all fields of concept section 8 (``None`` where not applicable):
record_id, seq, ts_utc (ISO 8601 with milliseconds), source, actor_type, actor_id,
agent_version, model, model_version, prompt_version, rule_versions, corpus_version,
source_ids, usage, cost_estimate, event_type, case_id, signal_id, before, after, reason,
prev_hash, hash - plus the app version (D-21). ``hash`` = SHA-256 over the previous hash
and the canonical JSON of the record. :meth:`AuditLog.verify` recomputes the chain and
reports the first break; editing any stored record makes it fail.

Limit (README): the chain makes changes detectable; an owner of the subscription or of
the database file can still delete the whole store.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .models import Clock, SystemClock, iso_utc
from .versions import APP_VERSION

GENESIS_HASH = "0" * 64

SOURCES: tuple[str, ...] = ("upload", "seed", "rule", "agent", "ui", "api", "system")
ACTOR_TYPES: tuple[str, ...] = ("human", "agent", "rule", "system")

# Event types (concept section 8 lists examples; the vocabulary is closed on purpose).
STATUS_CHANGED = "STATUS_CHANGED"
AGENT_RUN = "AGENT_RUN"
TOOL_CALL = "TOOL_CALL"
RETRIEVAL_RUN = "RETRIEVAL_RUN"
CORRELATION_RUN = "CORRELATION_RUN"
DECISION = "DECISION"
FINDING_CREATED = "FINDING_CREATED"
FINDING_UPDATED = "FINDING_UPDATED"
METADATA_EDITED = "METADATA_EDITED"
SIGNAL_ADDED = "SIGNAL_ADDED"
ALERT_REQUESTED = "ALERT_REQUESTED"
ALERT_SENT = "ALERT_SENT"
COST_RECORDED = "COST_RECORDED"
ACCESS_DENIED = "ACCESS_DENIED"
RULE_EVALUATED = "RULE_EVALUATED"
CASE_CREATED = "CASE_CREATED"
TASK_CREATED = "TASK_CREATED"
TASK_CLOSED = "TASK_CLOSED"
URGENCY_RAISED = "URGENCY_RAISED"
URGENCY_LOWERED = "URGENCY_LOWERED"
APPROVAL_VOIDED = "APPROVAL_VOIDED"
SUBMISSION_RECORDED = "SUBMISSION_RECORDED"
RECOMMENDATION_ADDED = "RECOMMENDATION_ADDED"
DEADLINE_UPDATED = "DEADLINE_UPDATED"
REREVIEW_FLAGGED = "REREVIEW_FLAGGED"

EVENT_TYPES: frozenset[str] = frozenset({
    STATUS_CHANGED, AGENT_RUN, TOOL_CALL, RETRIEVAL_RUN, CORRELATION_RUN, DECISION, FINDING_CREATED,
    FINDING_UPDATED, METADATA_EDITED, SIGNAL_ADDED, ALERT_REQUESTED, ALERT_SENT, COST_RECORDED, ACCESS_DENIED,
    RULE_EVALUATED, CASE_CREATED, TASK_CREATED, TASK_CLOSED, URGENCY_RAISED, URGENCY_LOWERED, APPROVAL_VOIDED,
    SUBMISSION_RECORDED, RECOMMENDATION_ADDED, DEADLINE_UPDATED, REREVIEW_FLAGGED,
})

#: Human event types that require a reason (concept 7.2, 8: "Pflicht bei menschlichen
#: Entscheidungen und Metadatenaenderungen").
REASON_REQUIRED: frozenset[str] = frozenset({DECISION, METADATA_EDITED, URGENCY_LOWERED, ACCESS_DENIED, FINDING_UPDATED})

#: Every record carries all of these keys.
RECORD_FIELDS: tuple[str, ...] = (
    "record_id", "seq", "ts_utc", "source", "actor_type", "actor_id", "agent_version", "model", "model_version",
    "prompt_version", "rule_versions", "corpus_version", "source_ids", "usage", "cost_estimate", "event_type",
    "case_id", "signal_id", "before", "after", "reason", "app_version", "prev_hash", "hash",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE,
    ts_utc TEXT NOT NULL,
    event_type TEXT NOT NULL,
    case_id TEXT,
    signal_id TEXT,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS audit_case ON audit(case_id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit records are append-only: UPDATE is not allowed'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit records are append-only: DELETE is not allowed'); END;
"""


class AuditError(ValueError):
    """An audit record is incomplete or inconsistent (the action must not proceed)."""


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def chain_hash(prev_hash: str, record_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(record_without_hash)).encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """Normalise values to their JSON form so the stored record equals the hashed one."""
    return json.loads(canonical_json(value)) if value is not None else None


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    first_break_seq: int | None = None
    reason: str | None = None
    head_hash: str = GENESIS_HASH

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class AuditLog:
    """Audit trail on a SQLite connection shared with :class:`pms.store.Store`."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock | None = None,
                 lock: threading.RLock | None = None) -> None:
        self.conn = conn
        self.clock = clock or SystemClock()
        self.lock = lock or threading.RLock()

    def init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ write
    def append(
        self,
        *,
        event_type: str,
        source: str,
        actor_type: str,
        actor_id: str,
        case_id: str | None = None,
        signal_id: str | None = None,
        before: Any = None,
        after: Any = None,
        reason: str | None = None,
        rule_versions: Iterable[str] | None = None,
        agent_version: str | None = None,
        model: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        corpus_version: str | None = None,
        source_ids: Iterable[str] | None = None,
        usage: dict[str, Any] | None = None,
        cost_estimate: dict[str, Any] | None = None,
        ts: Any = None,
    ) -> dict[str, Any]:
        """Validate and append one record; returns the stored record (with hash)."""
        record: dict[str, Any] = {
            "record_id": uuid.uuid4().hex,
            "seq": None,
            "ts_utc": iso_utc(ts if ts is not None else self.clock.now(), ms=True),
            "source": source,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "agent_version": agent_version,
            "model": model,
            "model_version": model_version,
            "prompt_version": prompt_version,
            "rule_versions": list(rule_versions) if rule_versions is not None else None,
            "corpus_version": corpus_version,
            "source_ids": list(source_ids) if source_ids is not None else None,
            "usage": _jsonable(usage),
            "cost_estimate": _jsonable(cost_estimate),
            "event_type": event_type,
            "case_id": case_id,
            "signal_id": signal_id,
            "before": _jsonable(before),
            "after": _jsonable(after),
            "reason": reason.strip() if isinstance(reason, str) else reason,
            "app_version": APP_VERSION,
            "prev_hash": None,
        }
        self.validate(record)
        with self.lock:
            own_tx = not self.conn.in_transaction
            if own_tx:
                self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
                record["seq"] = (row[0] + 1) if row else 1
                record["prev_hash"] = row[1] if row else GENESIS_HASH
                digest = chain_hash(record["prev_hash"], record)
                self.conn.execute(
                    "INSERT INTO audit (seq, record_id, ts_utc, event_type, case_id, signal_id, actor_type, actor_id,"
                    " record_json, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (record["seq"], record["record_id"], record["ts_utc"], event_type, case_id, signal_id,
                     actor_type, actor_id, canonical_json(record), record["prev_hash"], digest),
                )
                if own_tx:
                    self.conn.execute("COMMIT")
            except BaseException:
                if own_tx:
                    self.conn.execute("ROLLBACK")
                raise
        return {**record, "hash": digest}

    @staticmethod
    def validate(record: dict[str, Any]) -> None:
        """Refuse incomplete records (0 missing fields is a Gate B check, concept 12 T06)."""
        if record["source"] not in SOURCES:
            raise AuditError(f"unknown audit source {record['source']!r}")
        if record["actor_type"] not in ACTOR_TYPES:
            raise AuditError(f"unknown actor type {record['actor_type']!r}")
        if record["event_type"] not in EVENT_TYPES:
            raise AuditError(f"unknown event type {record['event_type']!r}")
        if not record["actor_id"]:
            raise AuditError("actor_id is mandatory")
        if record["actor_type"] == "agent" and record["event_type"] != ACCESS_DENIED:
            # A refused agent attempt is recorded with whatever identity is known.
            for name in ("agent_version", "model", "model_version", "prompt_version"):
                if not record[name]:
                    raise AuditError(f"agent records need {name}")
        if record["actor_type"] == "rule" and not record["rule_versions"]:
            raise AuditError("rule records need rule_versions")
        if record["event_type"] in (RETRIEVAL_RUN, CORRELATION_RUN) and not record["corpus_version"]:
            raise AuditError("retrieval and correlation records need corpus_version")
        if record["actor_type"] == "human" and record["event_type"] in REASON_REQUIRED and not record["reason"]:
            raise AuditError(f"{record['event_type']} by a human needs a reason")

    # ------------------------------------------------------------------ read
    def records(self, *, case_id: str | None = None, event_type: str | None = None,
                after_seq: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT record_json, hash FROM audit WHERE seq > ?"
        params: list[Any] = [after_seq]
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [{**json.loads(r[0]), "hash": r[1]} for r in rows]

    def count(self, *, case_id: str | None = None, event_type: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM audit WHERE 1=1"
        params: list[Any] = []
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type)
        with self.lock:
            return int(self.conn.execute(sql, params).fetchone()[0])

    def head(self) -> str:
        with self.lock:
            row = self.conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS_HASH

    def verify(self) -> VerifyResult:
        """Recompute the whole chain; report the first broken record."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT seq, record_id, ts_utc, event_type, case_id, signal_id, actor_type, actor_id, record_json,"
                " prev_hash, hash FROM audit ORDER BY seq"
            ).fetchall()
        prev = GENESIS_HASH
        for expected_seq, row in enumerate(rows, start=1):
            seq, record_id, ts_utc, event_type, case_id, signal_id, actor_type, actor_id, raw, prev_hash, digest = row

            def broken(reason: str) -> VerifyResult:
                return VerifyResult(False, expected_seq - 1, seq, reason, prev)

            if seq != expected_seq:
                return broken(f"sequence gap: expected {expected_seq}, found {seq}")
            try:
                record = json.loads(raw)
            except ValueError:
                return broken("record is not valid JSON")
            missing = [f for f in RECORD_FIELDS if f != "hash" and f not in record]
            if missing:
                return broken(f"record misses fields {missing}")
            if record.get("seq") != seq:
                return broken("seq column differs from the record")
            if prev_hash != prev or record.get("prev_hash") != prev:
                return broken("prev_hash does not match the previous record")
            columns = {"record_id": record_id, "ts_utc": ts_utc, "event_type": event_type, "case_id": case_id,
                       "signal_id": signal_id, "actor_type": actor_type, "actor_id": actor_id}
            for name, value in columns.items():
                if record.get(name) != value:
                    return broken(f"column {name} differs from the hashed record")
            if chain_hash(prev, record) != digest:
                return broken("hash does not match the record content")
            prev = digest
        return VerifyResult(True, len(rows), None, None, prev)
