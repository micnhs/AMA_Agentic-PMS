"""Case store on Azure Cosmos DB for NoSQL with the semantics of the SQLite store (ADR-004, INFRA_NOTES 5).

Contract (the containers exist already - the app cannot create them; partition key path ``/pk``):

* ``cases``   - one document per case, ``pk`` = case id.
* ``signals`` - one document per signal, ``pk`` = case id.
* ``records`` - every other table of :mod:`pms.store` as documents typed by table name
  (``type`` = ``tasks``, ``alerts``, ``deadlines`` ...; ``pk`` = case id or ``global``), plus the
  analysis-job queue (``analysis_jobs``), counters, meta values, the write lease and the
  published Foundry agent versions.
* ``audit``   - the hash-chained audit trail, written with ``create_item`` only (the app role
  allows create and read, no replace/upsert/delete), ``pk = "audit-chain"`` and ``id`` = ``seq``;
  the unique key ``/seq`` rejects a second record with the same number (HTTP 409) and the writer
  retries with the next number, so web and worker processes extend one linear chain.

:class:`CosmosStore` subclasses :class:`pms.store.Store` and replaces only its persistence
primitives (``_insert``, ``_get``, ``_update``, ``_list``, ``next_id``, meta values and
``transaction``), so every repository method keeps exactly the SQLite semantics (defaults, NOT NULL
columns, ordering, JSON round trip). Column defaults and NOT NULL flags are read from the SQLite
schema itself. Two processes (web and worker) share the store:

* single-document writes use optimistic concurrency (``_etag`` + ``IfNotModified``, retry on 412);
* ID counters are documents incremented with optimistic concurrency;
* ``transaction()`` holds a short write lease (a document in ``records``) so that read-check-write
  sequences of the workflow are serialised across processes like SQLite's ``BEGIN IMMEDIATE``.
  Cosmos DB has no multi-document rollback: an error inside a transaction leaves the writes made
  so far (logged); the audit chain stays valid.

All Azure SDK imports (azure-cosmos, azure-core, azure-identity) happen inside functions.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields
from typing import Any, Callable, Iterable, Iterator

from .. import store as _sqlite_store
from ..audit import (
    GENESIS_HASH, RECORD_FIELDS, AuditError, AuditLog, VerifyResult, canonical_json, chain_hash,
)
from ..interfaces import Job
from ..models import Clock, SystemClock, iso_utc, parse_utc
from ..store import _TABLES, NotFound, Store, _Table
from ..versions import APP_VERSION
from .errors import AzureAdapterError, status_code

log = logging.getLogger("pms.azure.cosmos")

CASES, SIGNALS, RECORDS, AUDIT = "cases", "signals", "records", "audit"
CONTAINERS: tuple[str, ...] = (CASES, SIGNALS, RECORDS, AUDIT)
AUDIT_PK = "audit-chain"
GLOBAL_PK = "global"
META_PK = "meta"
COUNTER_PK = "counters"
LEASE_PK = "leases"
AGENTS_PK = "foundry-agents"
JOB_TYPE = "analysis_jobs"
AGENT_VERSION_TYPE = "foundry_agent_versions"
LEASE_ID = "lease:store"
SYSTEM_PROPERTIES: tuple[str, ...] = ("_rid", "_self", "_etag", "_attachments", "_ts")
MAX_CONFLICT_RETRIES = 25
JOB_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(Job))
#: FAILED alert deliveries are retried after these pauses (seconds, by attempt number).
ALERT_RETRY_BACKOFF_S: tuple[float, ...] = (5.0, 15.0, 45.0, 135.0)

_sleep: Callable[[float], None] = time.sleep


class CosmosStoreError(AzureAdapterError):
    """Cosmos DB answered unexpectedly (missing container, missing permission, endless conflicts)."""


class StoreBusy(CosmosStoreError):
    """The cross-process write lease could not be acquired in time."""


# --------------------------------------------------------------------------- SDK helpers
def _if_not_modified() -> Any:
    from azure.core import MatchConditions  # type: ignore[import-not-found]

    return MatchConditions.IfNotModified


def _backoff(attempt: int) -> None:
    _sleep(min(0.25, 0.005 * (2 ** min(attempt, 8))) * random.uniform(0.5, 1.5))


def _strip(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in SYSTEM_PROPERTIES}


_UNSAFE_ID = re.compile(r"[%/\\?#]")


def safe_id(raw: str) -> str:
    """Cosmos DB ids must not contain '/', '\\', '?' or '#': percent-encode them (injective)."""
    return _UNSAFE_ID.sub(lambda m: f"%{ord(m.group(0)):02X}", str(raw))


def _jsonable(value: Any) -> Any:
    """JSON round trip with the SQLite store's encoder (``default=str``) - same values come back."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- account and containers
class CosmosAccount:
    """Cosmos DB client and the four containers of the contract (created by Bicep, never by the app)."""

    def __init__(self, endpoint: str, database: str, *, credential: Any = None, client: Any = None) -> None:
        if client is None:
            from azure.cosmos import CosmosClient  # type: ignore[import-not-found]

            client = CosmosClient(endpoint, credential=credential)
        self.endpoint = endpoint
        self.database_name = database
        self.client = client
        db = client.get_database_client(database)
        self.containers = {name: db.get_container_client(name) for name in CONTAINERS}
        self._checked = False
        self._lock = threading.Lock()

    def container(self, name: str) -> Any:
        return self.containers[name]

    def check(self) -> None:
        """Fail early with a plain message when a container is missing or not accessible."""
        with self._lock:
            if self._checked:
                return
            for name, container in self.containers.items():
                try:
                    container.read()
                except Exception as exc:
                    code = status_code(exc)
                    if code == 404:
                        raise CosmosStoreError(
                            f"Cosmos DB container '{name}' does not exist in database '{self.database_name}' - "
                            "the app cannot create containers (data-plane RBAC); deploy infra/modules/cosmos.bicep"
                        ) from exc
                    if code == 403:
                        raise CosmosStoreError(
                            f"no data-plane access to the Cosmos DB container '{name}' - check the role assignments "
                            "of the app identity (AZURE_CLIENT_ID) in infra/modules/cosmos.bicep") from exc
                    raise
            self._checked = True

    def describe(self) -> str:
        return f"Azure Cosmos DB {self.endpoint} database {self.database_name}"


_accounts_lock = threading.Lock()
_accounts: dict[tuple[str, str], CosmosAccount] = {}


def account_from_settings(settings: Any) -> CosmosAccount:
    """One shared account object per process (store, job queue and agent registry)."""
    if not settings.cosmos_endpoint:
        raise CosmosStoreError("COSMOS_ENDPOINT is not set")
    key = (settings.cosmos_endpoint, settings.cosmos_database or "pms")
    with _accounts_lock:
        account = _accounts.get(key)
        if account is None:
            from .identity import get_credential

            account = CosmosAccount(key[0], key[1], credential=get_credential(settings.azure_client_id))
            _accounts[key] = account
        return account


def reset_accounts() -> None:
    """Forget cached accounts (tests)."""
    with _accounts_lock:
        _accounts.clear()


# --------------------------------------------------------------------------- schema of the SQLite store
@dataclass(frozen=True)
class _Column:
    name: str
    type: str
    notnull: bool
    has_default: bool
    default: Any


def _literal(text: str) -> Any:
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _schema() -> dict[str, dict[str, _Column]]:
    """Column types, NOT NULL flags and decoded defaults, read from the SQLite schema of :mod:`pms.store`."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_sqlite_store._SCHEMA)
        out: dict[str, dict[str, _Column]] = {}
        for name, table in _TABLES.items():
            cols: dict[str, _Column] = {}
            for _cid, col, ctype, notnull, dflt, _pk in conn.execute(f"PRAGMA table_info({name})"):
                default: Any = None
                if dflt is not None:
                    default = _literal(str(dflt))
                    if col in table.json_cols:
                        default = json.loads(default)
                    elif col in table.bool_cols:
                        default = bool(int(default))
                cols[col] = _Column(col, (ctype or "").upper(), bool(notnull), dflt is not None, default)
            out[name] = cols
        return out
    finally:
        conn.close()


SCHEMA: dict[str, dict[str, _Column]] = _schema()
_INT_RE = re.compile(r"^[+-]?\d+$")


def _affinity(ctype: str, value: Any) -> Any:
    """SQLite type affinity for plain columns (what a value looks like after a SQLite round trip)."""
    if value is None:
        return None
    if ctype == "INTEGER":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and _INT_RE.match(value.strip()):
            return int(value.strip())
        return value
    if ctype == "REAL":
        if isinstance(value, (bool, int)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return value
    if ctype == "TEXT" and isinstance(value, (bool, int, float)):
        return str(int(value) if isinstance(value, bool) else value)
    return value


# --------------------------------------------------------------------------- audit chain
def verify_rows(rows: Iterable[tuple]) -> VerifyResult:
    """Chain check over (seq, record_id, ts_utc, event_type, case_id, signal_id, actor_type, actor_id,
    record_json, prev_hash, hash) rows ordered by seq - the algorithm of :meth:`pms.audit.AuditLog.verify`."""
    prev = GENESIS_HASH
    count = 0
    for expected_seq, row in enumerate(rows, start=1):
        count += 1
        seq, record_id, ts_utc, event_type, case_id, signal_id, actor_type, actor_id, raw, prev_hash, digest = row

        def broken(reason: str) -> VerifyResult:
            return VerifyResult(False, expected_seq - 1, seq, reason, prev)

        if seq != expected_seq:
            return broken(f"sequence gap: expected {expected_seq}, found {seq}")
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            return broken("record is not valid JSON")
        if not isinstance(record, dict):
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
    return VerifyResult(True, count, None, None, prev)


def _row(doc: dict[str, Any]) -> tuple:
    return (doc.get("seq"), doc.get("record_id"), doc.get("ts_utc"), doc.get("event_type"), doc.get("case_id"),
            doc.get("signal_id"), doc.get("actor_type"), doc.get("actor_id"), doc.get("record_json"),
            doc.get("prev_hash"), doc.get("hash"))


class CosmosAuditTrail:
    """Append-only hash chain in the ``audit`` container (``create_item`` only; ``id`` = ``seq``)."""

    def __init__(self, container: Any, *, clock: Clock | None = None, lock: threading.RLock | None = None) -> None:
        self.container = container
        self.clock = clock or SystemClock()
        self.lock = lock or threading.RLock()
        self._head: tuple[int, str] | None = None
        self.conflicts = 0  # 409 answers seen (another process appended first)

    # ------------------------------------------------------------------ write
    def append(self, *, event_type: str, source: str, actor_type: str, actor_id: str, case_id: str | None = None,
               signal_id: str | None = None, before: Any = None, after: Any = None, reason: str | None = None,
               rule_versions: Iterable[str] | None = None, agent_version: str | None = None,
               model: str | None = None, model_version: str | None = None, prompt_version: str | None = None,
               corpus_version: str | None = None, source_ids: Iterable[str] | None = None,
               usage: dict[str, Any] | None = None, cost_estimate: dict[str, Any] | None = None,
               ts: Any = None) -> dict[str, Any]:
        """Validate and append one record (same record layout and checks as :class:`pms.audit.AuditLog`)."""
        def norm(value: Any) -> Any:
            return json.loads(canonical_json(value)) if value is not None else None

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
            "usage": norm(usage),
            "cost_estimate": norm(cost_estimate),
            "event_type": event_type,
            "case_id": case_id,
            "signal_id": signal_id,
            "before": norm(before),
            "after": norm(after),
            "reason": reason.strip() if isinstance(reason, str) else reason,
            "app_version": APP_VERSION,
            "prev_hash": None,
        }
        AuditLog.validate(record)
        with self.lock:
            for attempt in range(MAX_CONFLICT_RETRIES * 2):
                if self._head is None:
                    self._head = self._read_head()
                seq, prev = self._head[0] + 1, self._head[1]
                record["seq"] = seq
                record["prev_hash"] = prev
                digest = chain_hash(prev, record)
                doc = {"id": str(seq), "pk": AUDIT_PK, "type": "audit", "seq": seq,
                       "record_id": record["record_id"], "ts_utc": record["ts_utc"], "event_type": event_type,
                       "case_id": case_id, "signal_id": signal_id, "actor_type": actor_type, "actor_id": actor_id,
                       "record_json": canonical_json(record), "prev_hash": prev, "hash": digest}
                try:
                    self.container.create_item(body=doc)
                except Exception as exc:
                    code = status_code(exc)
                    if code == 409:
                        self.conflicts += 1
                        if self._is_own(seq, record["record_id"]):  # a retried create that had succeeded
                            self._head = (seq, digest)
                            return {**record, "hash": digest}
                        self._head = None  # another process appended: read the head again, take the next number
                        if attempt:
                            _backoff(attempt)
                        continue
                    if code == 403:
                        raise AuditError("the audit container refused the write: the app role 'PMS audit "
                                         "append-only' needs items/create on the container 'audit'") from exc
                    raise
                self._head = (seq, digest)
                return {**record, "hash": digest}
        raise AuditError("could not append the audit record: the sequence number kept conflicting")

    def _is_own(self, seq: int, record_id: str) -> bool:
        try:
            doc = self.container.read_item(item=str(seq), partition_key=AUDIT_PK)
        except Exception as exc:
            if status_code(exc) == 404:
                return False
            raise
        return doc.get("record_id") == record_id

    def _read_head(self) -> tuple[int, str]:
        docs = list(self.container.query_items(
            query='SELECT TOP 1 * FROM c WHERE c["pk"] = @pk ORDER BY c["seq"] DESC',
            parameters=[{"name": "@pk", "value": AUDIT_PK}], partition_key=AUDIT_PK))
        return (int(docs[0]["seq"]), str(docs[0]["hash"])) if docs else (0, GENESIS_HASH)

    # ------------------------------------------------------------------ read
    def _query(self, select: str, *, case_id: str | None, event_type: str | None, after_seq: int = 0,
               order: bool = True, top: int | None = None) -> list[Any]:
        clauses = ['c["pk"] = @pk']
        params: list[dict[str, Any]] = [{"name": "@pk", "value": AUDIT_PK}]
        if after_seq:
            clauses.append('c["seq"] > @after')
            params.append({"name": "@after", "value": int(after_seq)})
        if case_id is not None:
            clauses.append('c["case_id"] = @case')
            params.append({"name": "@case", "value": case_id})
        if event_type is not None:
            clauses.append('c["event_type"] = @event')
            params.append({"name": "@event", "value": event_type})
        prefix = f"SELECT TOP {int(top)} " if top is not None else "SELECT "
        query = f"{prefix}{select} FROM c WHERE {' AND '.join(clauses)}"
        if order:
            query += ' ORDER BY c["seq"] ASC'
        return list(self.container.query_items(query=query, parameters=params, partition_key=AUDIT_PK))

    def records(self, *, case_id: str | None = None, event_type: str | None = None, after_seq: int = 0,
                limit: int | None = None) -> list[dict[str, Any]]:
        top = int(limit) if limit is not None and int(limit) >= 0 else None
        docs = self._query("*", case_id=case_id, event_type=event_type, after_seq=after_seq, top=top)
        return [{**json.loads(d["record_json"]), "hash": d["hash"]} for d in docs]

    def count(self, *, case_id: str | None = None, event_type: str | None = None) -> int:
        values = self._query("VALUE COUNT(1)", case_id=case_id, event_type=event_type, order=False)
        return int(values[0]) if values else 0

    def head(self) -> str:
        return self._read_head()[1]

    def documents(self) -> list[dict[str, Any]]:
        return self._query("*", case_id=None, event_type=None)

    def verify(self) -> VerifyResult:
        """Recompute the whole chain; report the first broken record."""
        return verify_rows(_row(d) for d in self.documents())

    # ------------------------------------------------------------------ tamper test (concept 8, Goal Card check 6)
    def probe_update_rbac(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Safe negative test of the role: replace one record with ITS OWN content and an etag that never matches.

        403 = the role blocks the update. 412 = only the precondition stopped it (the role would allow updates).
        Nothing can change: the body is identical and the precondition fails."""
        try:
            self.container.replace_item(item=doc["id"], body=_strip(doc), etag='"pms-tamper-probe-never-matches"',
                                        match_condition=_if_not_modified())
            outcome, blocked = "NOT BLOCKED: the service accepted the write", False
        except Exception as exc:
            code = status_code(exc)
            blocked = code == 403
            outcome = {403: "blocked: 403 Forbidden by the Cosmos DB role 'PMS audit append-only'",
                       412: "NOT blocked by the role (stopped only by the precondition) - check the role assignment"
                       }.get(code or 0, f"inconclusive: {type(exc).__name__} (HTTP {code})")
        return {"replace": outcome, "blocked": blocked}

    def tamper_test(self, *, probe_rbac: bool = True) -> dict[str, Any]:
        """Edit one record of an in-memory COPY of the chain and show that the chain check finds it."""
        docs = [dict(d) for d in self.documents()]
        if len(docs) < 2:
            return {"detected": False, "note": "too few audit records for the tamper test"}
        seq = len(docs) // 2
        probe = self.probe_update_rbac(docs[seq - 1]) if probe_rbac else {"blocked": None, "replace": "not probed"}
        target = docs[seq - 1]
        record = json.loads(target["record_json"])
        record["reason"] = "edited directly in the database (tamper test)"
        target["record_json"] = canonical_json(record)
        result = verify_rows(_row(d) for d in docs)
        return {"edited_seq": seq, "update_blocked_by_trigger": probe["blocked"], "update_blocked_by_rbac": probe,
                "verify_ok_after_edit": result.ok, "first_break_seq": result.first_break_seq, "reason": result.reason,
                "detected": (not result.ok) and result.first_break_seq == seq,
                "note": "performed on an in-memory copy of the audit documents; the real audit trail is unchanged"}


# --------------------------------------------------------------------------- write lease
class CosmosLease:
    """Cross-process write lease (one document in ``records``): ``transaction()`` of the web and the worker
    process run one after the other, like SQLite's BEGIN IMMEDIATE. A lease of a crashed process expires."""

    def __init__(self, container: Any, *, owner: str, ttl_s: float = 60.0, timeout_s: float = 60.0) -> None:
        self.container = container
        self.owner = owner
        self.ttl_s = ttl_s
        self.timeout_s = timeout_s
        self.waits = 0
        self.takeovers = 0

    def _body(self) -> dict[str, Any]:
        now = time.time()
        return {"id": LEASE_ID, "pk": LEASE_PK, "type": "lease", "owner": self.owner, "expires": now + self.ttl_s,
                "acquired_at": iso_utc(_dt.datetime.now(_dt.timezone.utc), ms=True)}

    def acquire(self) -> str:
        deadline = time.monotonic() + self.timeout_s
        attempt = 0
        while True:
            try:
                return str(self.container.create_item(body=self._body())["_etag"])
            except Exception as exc:
                if status_code(exc) != 409:
                    raise
            try:
                current = self.container.read_item(item=LEASE_ID, partition_key=LEASE_PK)
            except Exception as exc:
                if status_code(exc) == 404:
                    continue  # released in the meantime
                raise
            if float(current.get("expires", 0)) < time.time():
                try:
                    taken = self.container.replace_item(item=LEASE_ID, body=self._body(), etag=current["_etag"],
                                                        match_condition=_if_not_modified())
                    self.takeovers += 1
                    log.warning("took over an expired store lease of %s", current.get("owner"))
                    return str(taken["_etag"])
                except Exception as exc:
                    if status_code(exc) not in (404, 412):
                        raise
                    continue
            if time.monotonic() > deadline:
                raise StoreBusy(f"the Cosmos DB store is locked by {current.get('owner')} for more than "
                                f"{self.timeout_s:.0f} s")
            self.waits += 1
            attempt += 1
            _backoff(attempt)

    def release(self, etag: str) -> None:
        try:
            self.container.delete_item(item=LEASE_ID, partition_key=LEASE_PK, etag=etag,
                                       match_condition=_if_not_modified())
        except Exception as exc:
            if status_code(exc) in (404, 412):
                log.warning("store lease of %s had expired and was taken over before release", self.owner)
                return
            raise


class _ConnectionShim:
    """``store.conn.in_transaction`` for code written against the SQLite store; SQL itself is refused."""

    def __init__(self, store: "CosmosStore") -> None:
        self._store = store

    @property
    def in_transaction(self) -> bool:
        return self._store._depth > 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise CosmosStoreError("SQL is not available on the Cosmos DB store")

    executescript = execute
    backup = execute


# --------------------------------------------------------------------------- the store
class CosmosStore(Store):
    """:class:`pms.store.Store` on Cosmos DB (same repository API and semantics)."""

    def __init__(self, account: CosmosAccount, *, clock: Clock | None = None, lease: bool = True,
                 check: bool = True, owner: str | None = None, lease_ttl_s: float = 60.0,
                 lease_timeout_s: float = 60.0) -> None:
        if check:
            account.check()
        self.account = account
        self.path = f"cosmos://{account.endpoint.rstrip('/')}/{account.database_name}"
        self.clock = clock or SystemClock()
        self.lock = threading.RLock()
        self._depth = 0
        self._tx_writes = 0
        self.conn = _ConnectionShim(self)
        self.owner = owner or f"{os.environ.get('CONTAINER_APP_NAME') or 'pms'}/{os.getpid()}/{uuid.uuid4().hex[:8]}"
        self.audit = CosmosAuditTrail(account.container(AUDIT), clock=self.clock, lock=self.lock)
        self.lease = (CosmosLease(account.container(RECORDS), owner=self.owner, ttl_s=lease_ttl_s,
                                  timeout_s=lease_timeout_s) if lease else None)
        self._pk_cache: dict[tuple[str, str], str] = {}
        self._counters: dict[str, dict[str, Any]] = {}
        self.conflicts = 0  # 412 answers seen (optimistic concurrency)
        self._closed = False
        try:
            self._records.create_item(body={"id": "meta:schema_version", "pk": META_PK, "type": "meta",
                                            "key": "schema_version", "value": _sqlite_store.SCHEMA_VERSION})
        except Exception as exc:
            if status_code(exc) not in (403, 409):  # 403: a read-only identity (e.g. verify-audit as deployer)
                raise

    @classmethod
    def from_settings(cls, settings: Any, *, clock: Clock | None = None, **kwargs: Any) -> "CosmosStore":
        return cls(account_from_settings(settings), clock=clock, **kwargs)

    def describe(self) -> str:
        return self.account.describe()

    # ------------------------------------------------------------------ containers and documents
    @property
    def _records(self) -> Any:
        return self.account.container(RECORDS)

    @staticmethod
    def _container_name(table_name: str) -> str:
        return {"cases": CASES, "signals": SIGNALS}.get(table_name, RECORDS)

    def _container(self, table_name: str) -> Any:
        return self.account.container(self._container_name(table_name))

    @staticmethod
    def _doc_id(table_name: str, rec_id: str) -> str:
        return safe_id(rec_id if table_name in ("cases", "signals") else f"{table_name}:{rec_id}")

    @staticmethod
    def _partition_of(table_name: str, doc: dict[str, Any]) -> str:
        if table_name == "cases":
            return str(doc["case_id"])
        case_id = doc.get("case_id")
        return str(case_id) if case_id else GLOBAL_PK

    def _remember(self, table_name: str, rec_id: Any, pk: str) -> None:
        if len(self._pk_cache) > 50000:
            self._pk_cache.clear()
        self._pk_cache[(table_name, str(rec_id))] = pk

    def _wrote(self) -> None:
        if self._depth:
            self._tx_writes += 1

    def _read_doc(self, table_name: str, rec_id: str) -> dict[str, Any]:
        container = self._container(table_name)
        doc_id = self._doc_id(table_name, rec_id)
        pk = str(rec_id) if table_name == "cases" else self._pk_cache.get((table_name, str(rec_id)))
        if pk is not None:
            try:
                return container.read_item(item=doc_id, partition_key=pk)
            except Exception as exc:
                if status_code(exc) != 404:
                    raise
                self._pk_cache.pop((table_name, str(rec_id)), None)
                if table_name == "cases":
                    raise NotFound(f"{table_name} {rec_id} not found") from exc
        found = list(container.query_items(query='SELECT * FROM c WHERE c["id"] = @id',
                                           parameters=[{"name": "@id", "value": doc_id}],
                                           enable_cross_partition_query=True))
        if not found:
            raise NotFound(f"{table_name} {rec_id} not found")
        self._remember(table_name, rec_id, str(found[0]["pk"]))
        return found[0]

    def _decode_doc(self, table: _Table, doc: dict[str, Any]) -> Any:
        schema = SCHEMA[table.name]
        data: dict[str, Any] = {}
        for col in self._cols(table):
            value = doc.get(col)
            if col in table.bool_cols:
                value = bool(value)
            elif col not in table.json_cols:
                value = _affinity(schema[col].type, value)
            data[col] = value
        return table.record(**data)

    def _encode_value(self, table: _Table, col: str, value: Any) -> Any:
        if col in table.json_cols:
            return None if value is None else _jsonable(value)
        if col in table.bool_cols:
            return bool(value)
        column = SCHEMA[table.name][col]
        if value is None and column.notnull:
            raise ValueError(f"NOT NULL constraint failed: {table.name}.{col}")
        return _affinity(column.type, value)

    # ------------------------------------------------------------------ persistence primitives
    def _insert(self, table_name: str, values: dict[str, Any]) -> Any:
        table = _TABLES[table_name]
        cols = self._cols(table)
        unknown = set(values) - set(cols)
        if unknown:
            raise ValueError(f"unknown columns for {table_name}: {sorted(unknown)}")
        with self.lock:
            if not values.get(table.pk):
                values[table.pk] = self.next_id(table.prefix)
            schema = SCHEMA[table_name]
            doc: dict[str, Any] = {}
            for col in cols:
                if col in values:
                    doc[col] = self._encode_value(table, col, values[col])
                elif schema[col].has_default:
                    doc[col] = copy.deepcopy(schema[col].default)
                elif schema[col].notnull:
                    raise ValueError(f"NOT NULL constraint failed: {table_name}.{col}")
                else:
                    doc[col] = None
            rec_id = str(doc[table.pk])
            pk = self._partition_of(table_name, doc)
            body = {"id": self._doc_id(table_name, rec_id), "pk": pk, "type": table_name, **doc}
            try:
                created = self._container(table_name).create_item(body=body)
            except Exception as exc:
                if status_code(exc) == 409:
                    raise ValueError(f"UNIQUE constraint failed: {table_name}.{table.pk} ({rec_id})") from exc
                raise
            self._wrote()
            self._remember(table_name, rec_id, pk)
        return self._decode_doc(table, created)

    def _get(self, table_name: str, pk: str) -> Any:
        with self.lock:
            doc = self._read_doc(table_name, pk)
        return self._decode_doc(_TABLES[table_name], doc)

    def _update(self, table_name: str, pk: str, values: dict[str, Any], *,
                drop_keys: tuple[str, ...] = ()) -> Any:
        table = _TABLES[table_name]
        cols = self._cols(table)
        unknown = set(values) - set(cols) | ({table.pk} & set(values))
        if unknown:
            raise ValueError(f"cannot update columns {sorted(unknown)} of {table_name}")
        if not values:
            return self._get(table_name, pk)
        encoded = {col: self._encode_value(table, col, value) for col, value in values.items()}
        container = self._container(table_name)
        with self.lock:
            for attempt in range(MAX_CONFLICT_RETRIES):
                doc = self._read_doc(table_name, pk)
                body = {k: v for k, v in _strip(doc).items() if k not in drop_keys}
                body.update(encoded)
                new_pk = self._partition_of(table_name, body)
                try:
                    if new_pk != doc["pk"]:  # case id changed: the document moves to the new partition
                        replaced = container.create_item(body={**body, "pk": new_pk})
                        try:
                            container.delete_item(item=doc["id"], partition_key=doc["pk"])
                        except Exception as exc:
                            if status_code(exc) != 404:
                                raise
                        self._remember(table_name, pk, new_pk)
                    else:
                        replaced = container.replace_item(item=doc["id"], body=body, etag=doc["_etag"],
                                                          match_condition=_if_not_modified())
                except Exception as exc:
                    if status_code(exc) in (404, 409, 412):
                        self.conflicts += 1
                        _backoff(attempt)
                        continue
                    raise
                self._wrote()
                return self._decode_doc(table, replaced)
        raise CosmosStoreError(f"update of {table_name} {pk} kept conflicting with other writers")

    def _query_docs(self, table_name: str, conds: dict[str, Any]) -> list[dict[str, Any]]:
        container = self._container(table_name)
        clauses: list[str] = []
        params: list[dict[str, Any]] = []
        if self._container_name(table_name) == RECORDS:
            clauses.append('c["type"] = @type')
            params.append({"name": "@type", "value": table_name})
        for i, (col, value) in enumerate(conds.items()):
            clauses.append(f'c["{col}"] = @p{i}')
            params.append({"name": f"@p{i}", "value": value})
        query = "SELECT * FROM c" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        partition = conds.get("case_id")
        if isinstance(partition, str) and partition:
            items = container.query_items(query=query, parameters=params, partition_key=partition)
        else:
            items = container.query_items(query=query, parameters=params, enable_cross_partition_query=True)
        docs = list(items)
        pk_col = _TABLES[table_name].pk
        for d in docs:
            if d.get(pk_col) is not None:
                self._remember(table_name, d[pk_col], str(d["pk"]))
        return docs

    def _list(self, table_name: str, where: dict[str, Any] | None = None) -> list[Any]:
        table = _TABLES[table_name]
        cols = self._cols(table)
        conds: dict[str, Any] = {}
        for col, value in (where or {}).items():
            if col not in cols:
                raise ValueError(f"unknown filter column {col} for {table_name}")
            if value is None:
                continue
            conds[col] = self._encode_value(table, col, value)
        with self.lock:
            docs = self._query_docs(table_name, conds)
        rows = [self._decode_doc(table, d) for d in docs]
        order = [c.strip() for c in table.order_by.split(",") if c.strip()] + [table.pk]

        def key(row: Any) -> tuple:
            return tuple((0, 0) if getattr(row, c) is None else (1, getattr(row, c)) for c in order)

        return sorted(rows, key=key)

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def transaction(self) -> Iterator["CosmosStore"]:
        """Re-entrant; the outermost level holds the cross-process write lease. There is no rollback."""
        with self.lock:
            outer = self._depth == 0
            etag = self.lease.acquire() if outer and self.lease is not None else None
            if outer:
                self._tx_writes = 0
            self._depth += 1
            try:
                yield self
            except BaseException:
                if outer and self._tx_writes:
                    log.warning("transaction aborted after %s writes - Cosmos DB has no multi-document rollback; "
                                "the writes stay visible and the audit chain stays valid", self._tx_writes)
                raise
            finally:
                self._depth -= 1
                if outer and etag is not None:
                    self.lease.release(etag)

    def next_id(self, prefix: str) -> str:
        records = self._records
        doc_id = f"counter:{safe_id(prefix)}"
        with self.lock:
            for attempt in range(MAX_CONFLICT_RETRIES):
                current = self._counters.get(prefix)
                if current is None:
                    try:
                        current = records.read_item(item=doc_id, partition_key=COUNTER_PK)
                    except Exception as exc:
                        if status_code(exc) != 404:
                            raise
                        try:
                            created = records.create_item(body={"id": doc_id, "pk": COUNTER_PK, "type": "counter",
                                                                "name": prefix, "value": 1})
                        except Exception as exc2:
                            if status_code(exc2) == 409:
                                continue
                            raise
                        self._counters[prefix] = created
                        return f"{prefix}-{1:05d}"
                body = {**_strip(current), "value": int(current["value"]) + 1}
                try:
                    updated = records.replace_item(item=doc_id, body=body, etag=current["_etag"],
                                                   match_condition=_if_not_modified())
                except Exception as exc:
                    if status_code(exc) in (404, 412):
                        self._counters.pop(prefix, None)
                        self.conflicts += 1
                        if attempt:
                            _backoff(attempt)
                        continue
                    raise
                self._counters[prefix] = updated
                return f"{prefix}-{int(updated['value']):05d}"
        raise CosmosStoreError(f"counter {prefix} kept conflicting with other writers")

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        try:
            doc = self._records.read_item(item=f"meta:{safe_id(key)}", partition_key=META_PK)
        except Exception as exc:
            if status_code(exc) == 404:
                return default
            raise
        return doc.get("value", default)

    def set_meta(self, key: str, value: str) -> None:
        self._records.upsert_item(body={"id": f"meta:{safe_id(key)}", "pk": META_PK, "type": "meta", "key": key,
                                        "value": value})

    def update_alert(self, alert_id: str, **values: Any) -> Any:
        # A changed status ends a delivery claim (see claim_alert).
        return self._update("alerts", alert_id, values, drop_keys=("claim",) if "status" in values else ())

    # ------------------------------------------------------------------ cross-process alert delivery
    def claim_alert(self, alert_id: str, owner: str, lease_s: float = 30.0) -> bool:
        """Claim one delivery attempt of a PENDING or FAILED alert (web sender thread vs. worker retry).

        False when another process holds a live claim, the alert was delivered meanwhile, or a FAILED
        alert is still inside its retry pause (5 s, 15 s, 45 s, 135 s)."""
        records = self._records
        with self.lock:
            for attempt in range(MAX_CONFLICT_RETRIES):
                try:
                    doc = self._read_doc("alerts", alert_id)
                except NotFound:
                    return False
                if doc.get("status") not in ("PENDING", "FAILED"):
                    return False
                now = time.time()
                claim = doc.get("claim") or {}
                if claim.get("owner") not in (None, owner) and float(claim.get("until", 0)) > now:
                    return False
                if doc.get("status") == "FAILED" and doc.get("sent_at"):
                    # sent_at is written with the workflow clock - compare with the same clock
                    attempts = int((doc.get("detail") or {}).get("attempts", 1))
                    pause = ALERT_RETRY_BACKOFF_S[min(max(attempts, 1), len(ALERT_RETRY_BACKOFF_S)) - 1]
                    if parse_utc(doc["sent_at"]).timestamp() + pause > self.clock.now().timestamp():
                        return False
                body = {**_strip(doc), "claim": {"owner": owner, "until": now + lease_s,
                                                 "at": iso_utc(_dt.datetime.now(_dt.timezone.utc), ms=True)}}
                try:
                    records.replace_item(item=doc["id"], body=body, etag=doc["_etag"],
                                         match_condition=_if_not_modified())
                except Exception as exc:
                    if status_code(exc) in (404, 412):
                        self.conflicts += 1
                        continue
                    raise
                return True
        return False

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._closed = True  # the CosmosClient is shared per process (account_from_settings)


# --------------------------------------------------------------------------- analysis-job queue
class CosmosJobQueue:
    """Durable analysis-job queue in ``records`` (``type`` = ``analysis_jobs``, ``pk`` = case id), shared by the
    web and the worker container. Claims use optimistic concurrency, so a job never runs twice."""

    def __init__(self, store: CosmosStore) -> None:
        self.store = store
        self.container = store.account.container(RECORDS)

    @staticmethod
    def _doc_id(job_id: str) -> str:
        return safe_id(f"{JOB_TYPE}:{job_id}")

    @staticmethod
    def _job(doc: dict[str, Any]) -> Job:
        data = {f: doc.get(f) for f in JOB_FIELDS}
        data["progress"] = data.get("progress") or {}
        data["attempts"] = int(data.get("attempts") or 0)
        data["rounds"] = int(data.get("rounds") or 0)
        return Job(**data)

    def _now(self) -> str:
        return iso_utc(self.store.clock.now(), ms=True)

    def _query(self, *, case_id: str | None = None, **conds: Any) -> list[dict[str, Any]]:
        clauses = ['c["type"] = @type']
        params: list[dict[str, Any]] = [{"name": "@type", "value": JOB_TYPE}]
        if case_id is not None:
            conds["case_id"] = case_id
        for i, (col, value) in enumerate(conds.items()):
            clauses.append(f'c["{col}"] = @p{i}')
            params.append({"name": f"@p{i}", "value": value})
        query = "SELECT * FROM c WHERE " + " AND ".join(clauses)
        if case_id is not None:
            items = self.container.query_items(query=query, parameters=params, partition_key=case_id)
        else:
            items = self.container.query_items(query=query, parameters=params, enable_cross_partition_query=True)
        docs = list(items)
        for d in docs:
            self.store._remember(JOB_TYPE, d["job_id"], str(d["pk"]))
        return sorted(docs, key=lambda d: (d.get("enqueued_at") or "", d.get("job_id") or ""))

    def _read(self, job_id: str) -> dict[str, Any]:
        pk = self.store._pk_cache.get((JOB_TYPE, job_id))
        if pk is not None:
            try:
                return self.container.read_item(item=self._doc_id(job_id), partition_key=pk)
            except Exception as exc:
                if status_code(exc) != 404:
                    raise
        found = list(self.container.query_items(query='SELECT * FROM c WHERE c["id"] = @id',
                                                parameters=[{"name": "@id", "value": self._doc_id(job_id)}],
                                                enable_cross_partition_query=True))
        if not found:
            raise KeyError(f"job {job_id} not found")
        self.store._remember(JOB_TYPE, job_id, str(found[0]["pk"]))
        return found[0]

    def _modify(self, job_id: str, change: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any] | None:
        """Read-modify-replace with the document's etag; ``change`` returns None to leave the job as it is."""
        for attempt in range(MAX_CONFLICT_RETRIES):
            doc = self._read(job_id)
            body = change(_strip(doc))
            if body is None:
                return None
            try:
                return self.container.replace_item(item=doc["id"], body=body, etag=doc["_etag"],
                                                   match_condition=_if_not_modified())
            except Exception as exc:
                if status_code(exc) == 412:
                    self.store.conflicts += 1
                    _backoff(attempt)
                    continue
                raise
        raise CosmosStoreError(f"job {job_id} kept conflicting with other writers")

    # ------------------------------------------------------------------ JobQueue protocol
    def enqueue(self, case_id: str, signal_id: str, *, reason: str) -> Job:
        with self.store.transaction():
            open_jobs = [d for d in self._query(case_id=case_id, signal_id=signal_id)
                         if d.get("status") in ("QUEUED", "RUNNING")]
            if open_jobs:
                return self._job(sorted(open_jobs, key=lambda d: d["job_id"])[0])
            job_id = self.store.next_id("JOB")
            body = {"id": self._doc_id(job_id), "pk": case_id, "type": JOB_TYPE, "job_id": job_id,
                    "case_id": case_id, "signal_id": signal_id, "status": "QUEUED", "reason": reason,
                    "enqueued_at": self._now(), "started_at": None, "finished_at": None, "attempts": 0,
                    "error": None, "progress": {}, "rounds": 0, "worker": None}
            created = self.container.create_item(body=body)
            self.store._remember(JOB_TYPE, job_id, case_id)
        return self._job(created)

    def claim_next(self, worker_id: str) -> Job | None:
        for candidate in self._query(status="QUEUED"):
            doc = candidate
            for attempt in range(3):
                body = {**_strip(doc), "status": "RUNNING", "started_at": self._now(),
                        "attempts": int(doc.get("attempts") or 0) + 1, "worker": worker_id}
                try:
                    claimed = self.container.replace_item(item=doc["id"], body=body, etag=doc["_etag"],
                                                          match_condition=_if_not_modified())
                    return self._job(claimed)
                except Exception as exc:
                    if status_code(exc) == 404:
                        break
                    if status_code(exc) != 412:
                        raise
                self.store.conflicts += 1
                doc = self._read(candidate["job_id"])
                if doc.get("status") != "QUEUED":
                    break
        return None

    def update_progress(self, job_id: str, progress: dict[str, Any], *, rounds: int | None = None) -> None:
        payload = _jsonable(progress)

        def change(doc: dict[str, Any]) -> dict[str, Any]:
            doc["progress"] = payload
            if rounds is not None:
                doc["rounds"] = int(rounds)
            return doc

        try:
            self._modify(job_id, change)
        except KeyError:
            pass  # like the SQLite queue: updating an unknown job changes nothing

    def finish(self, job_id: str, status: str, *, error: str | None = None) -> Job:
        if status not in ("DONE", "FAILED", "SKIPPED"):
            raise ValueError(f"invalid final job status {status}")
        finished = self._now()

        def change(doc: dict[str, Any]) -> dict[str, Any]:
            doc.update(status=status, finished_at=finished, error=error)
            return doc

        self._modify(job_id, change)
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        return self._job(self._read(job_id))

    def list(self, *, case_id: str | None = None, status: str | None = None) -> list[Job]:
        conds = {"status": status} if status is not None else {}
        return [self._job(d) for d in self._query(case_id=case_id, **conds)]

    def pending_count(self) -> int:
        values = list(self.container.query_items(
            query='SELECT VALUE COUNT(1) FROM c WHERE c["type"] = @type AND ARRAY_CONTAINS(@open, c["status"])',
            parameters=[{"name": "@type", "value": JOB_TYPE}, {"name": "@open", "value": ["QUEUED", "RUNNING"]}],
            enable_cross_partition_query=True))
        return int(sum(values)) if values else 0

    def recover_stale(self, older_than: _dt.timedelta) -> int:
        """Re-queue RUNNING jobs whose worker vanished (restart without lost work)."""
        limit = self.store.clock.now() - older_than
        recovered = 0
        for doc in self._query(status="RUNNING"):
            if not doc.get("started_at") or parse_utc(doc["started_at"]) > limit:
                continue

            def change(current: dict[str, Any]) -> dict[str, Any] | None:
                if current.get("status") != "RUNNING":
                    return None
                current.update(status="QUEUED",
                               error=f"re-queued after worker interruption (attempt {current.get('attempts')})")
                return current

            if self._modify(doc["job_id"], change) is not None:
                recovered += 1
        return recovered


# --------------------------------------------------------------------------- published Foundry agent versions
class CosmosAgentRegistry:
    """Published Foundry prompt-agent version per agent (``records``, ``type`` = ``foundry_agent_versions``)."""

    def __init__(self, account: CosmosAccount) -> None:
        self.container = account.container(RECORDS)
        self.endpoint = account.endpoint

    @staticmethod
    def _doc_id(foundry_name: str) -> str:
        return safe_id(f"{AGENT_VERSION_TYPE}:{foundry_name}")

    def get(self, foundry_name: str) -> dict[str, Any] | None:
        try:
            doc = self.container.read_item(item=self._doc_id(foundry_name), partition_key=AGENTS_PK)
        except Exception as exc:
            if status_code(exc) == 404:
                return None
            raise
        return {k: v for k, v in _strip(doc).items() if k not in ("id", "pk", "type")}

    def put(self, foundry_name: str, record: dict[str, Any]) -> None:
        previous = self.get(foundry_name)
        history = list((previous or {}).get("history") or [])
        if previous and previous.get("foundry_version"):
            history.append({k: previous.get(k) for k in ("foundry_version", "spec_sha256", "prompt_version",
                                                          "model", "published_at")})
        body = {**_jsonable(record), "id": self._doc_id(foundry_name), "pk": AGENTS_PK, "type": AGENT_VERSION_TYPE,
                "foundry_name": foundry_name, "history": history[-10:]}
        self.container.upsert_item(body=body)

    def describe(self) -> str:
        return f"Cosmos DB records (type {AGENT_VERSION_TYPE})"
