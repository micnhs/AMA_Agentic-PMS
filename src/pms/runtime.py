"""Single factory for persistence, knowledge search, file storage, notifications, jobs and agents.

Application code obtains every service here and depends only on the interfaces of
:mod:`pms.interfaces`.

* ``APP_ENV=local`` (offline): SQLite store and job queue in ``DATA_DIR``, SQLite FTS5 knowledge index,
  local folder file store, outbox notifier, offline agent stand-in. Nothing Azure is imported.
* ``APP_ENV=azure``: Cosmos DB store (containers ``cases``, ``signals``, ``records`` and the create-only
  ``audit`` chain with ``pk = "audit-chain"``) with the job queue in ``records`` - shared by the web and the
  worker container -, Azure AI Search index, Blob Storage (``letters``, ``reports``, ``outbox``), ACS e-mail
  sent by a background thread (never inside an upload) and the Foundry agent adapter whose published
  prompt-agent versions are kept in ``records``. The Azure modules (:mod:`pms.azure`) are imported lazily.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from .agents.base import AgentRuntime
from .config import ConfigError, Settings, load_settings
from .costs import PriceTable
from .interfaces import CaseStore, FileStore, JobQueue, KnowledgeSearch, Notifier, StoredFile
from .knowledge import KnowledgeIndex, ReferenceData
from .models import Clock, iso_utc
from .telemetry import Telemetry

log = logging.getLogger("pms.runtime")

#: Azure worker: the deadline sweep reads every case - once a minute is enough (48-hour window) and keeps
#: the Cosmos DB request units of an idle worker low. Offline the sweep runs in every worker loop.
AZURE_DEADLINE_SWEEP_S = 60.0
#: Azure e-mail: wait at most this long for the final ACS status inside the sender thread.
ACS_SEND_TIMEOUT_S = 15.0


def get_settings(env: Mapping[str, str] | None = None, *, validate: bool = True) -> Settings:
    return load_settings(env, validate=validate)


def get_reference_data(settings: Settings | None = None) -> ReferenceData:
    return ReferenceData.load(settings.synthetic_data_dir if settings is not None else None)


# --------------------------------------------------------------------------- persistence
def _azure(settings: Settings | None) -> bool:
    return settings is not None and settings.is_azure


def uses_cosmos(settings: Settings | None) -> bool:
    """Azure with COSMOS_ENDPOINT: the shared Cosmos DB store. A separate worker without it is refused."""
    if not _azure(settings):
        return False
    assert settings is not None
    if settings.cosmos_endpoint:
        return True
    if settings.worker_mode == "separate":
        raise ConfigError("APP_ENV=azure with WORKER_MODE=separate needs COSMOS_ENDPOINT and COSMOS_DATABASE: the "
                          "web and the worker container must share one store (a SQLite file is per container)")
    return False


def _cosmos_module() -> Any:
    return sys.modules.get("pms.azure.cosmos_store")


def is_cosmos_store(store: Any) -> bool:
    module = _cosmos_module()
    return module is not None and isinstance(store, module.CosmosStore)


def get_store(settings: Settings | None = None, *, path: str | Path | None = None,
              clock: Clock | None = None) -> CaseStore:
    """Case store: Cosmos DB in Azure (shared by web and worker); SQLite in DATA_DIR offline or for an
    explicit ``path`` (``":memory:"`` for tests and the golden run)."""
    if path is None and uses_cosmos(settings):
        from .azure.cosmos_store import CosmosStore

        return CosmosStore.from_settings(settings, clock=clock)
    from .store import Store

    if path is None:
        if settings is None:
            raise ValueError("get_store needs settings or an explicit path")
        path = settings.db_path
    return Store(path, clock=clock)


def get_job_queue(store: CaseStore) -> JobQueue:
    if is_cosmos_store(store):
        from .azure.cosmos_store import CosmosJobQueue

        return CosmosJobQueue(store)  # type: ignore[arg-type]
    from .jobs import SqliteJobQueue
    from .store import Store

    if isinstance(store, Store):
        return SqliteJobQueue(store)
    raise NotImplementedError(f"no job queue for the store type {type(store).__name__}")


def snapshot_store(store: CaseStore) -> CaseStore:
    """Independent in-memory copy of a SQLite store (golden tamper test; never used for real data)."""
    from .store import Store

    if not isinstance(store, Store) or is_cosmos_store(store):
        raise NotImplementedError("snapshots exist for the SQLite store only")
    copy = Store(":memory:", clock=store.clock)
    with store.lock:
        store.conn.backup(copy.conn)
    return copy


def audit_tamper_test(store: CaseStore) -> dict[str, Any]:
    """Negative test of concept 8 on an independent copy: UPDATE is blocked by the trigger; an attacker
    who removes the protection and edits one record is detected by the chain check. Cosmos DB: the copy is
    an in-memory list of the audit documents; a safe probe shows that the role refuses an update."""
    import json
    import sqlite3

    tamper = getattr(store.audit, "tamper_test", None)
    if callable(tamper):
        return tamper()
    copy = snapshot_store(store)
    try:
        count = copy.audit.count()
        if count < 2:
            return {"detected": False, "note": "too few audit records for the tamper test"}
        seq = count // 2
        blocked = False
        try:
            copy.conn.execute("UPDATE audit SET actor_id = 'tampered' WHERE seq = ?", (seq,))
        except sqlite3.DatabaseError:
            blocked = True
        copy.conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        raw = copy.conn.execute("SELECT record_json FROM audit WHERE seq = ?", (seq,)).fetchone()[0]
        record = json.loads(raw)
        record["reason"] = "edited directly in the database (tamper test)"
        copy.conn.execute("UPDATE audit SET record_json = ? WHERE seq = ?",
                          (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False), seq))
        result = copy.audit.verify()
        return {"edited_seq": seq, "update_blocked_by_trigger": blocked, "verify_ok_after_edit": result.ok,
                "first_break_seq": result.first_break_seq, "reason": result.reason,
                "detected": (not result.ok) and result.first_break_seq == seq,
                "note": "performed on an in-memory copy; the real audit trail is unchanged"}
    finally:
        copy.close()


# --------------------------------------------------------------------------- knowledge search
def _uses_ai_search(settings: Settings | None) -> bool:
    return settings is not None and settings.is_azure and bool(settings.azure_search_endpoint
                                                               and settings.azure_search_index)


def get_knowledge_search(ref: ReferenceData, settings: Settings | None = None) -> KnowledgeSearch:
    """Read-only knowledge search: Azure AI Search in Azure; offline the SQLite FTS5 index of the same corpus."""
    if _uses_ai_search(settings):
        from .azure.search import AzureKnowledgeSearch

        assert settings is not None
        return AzureKnowledgeSearch(ref, endpoint=str(settings.azure_search_endpoint),
                                    index_name=str(settings.azure_search_index), client_id=settings.azure_client_id)
    return KnowledgeIndex(ref)


def seed_search_index(settings: Settings, *, ref: ReferenceData | None = None, force: bool = False) -> dict[str, Any]:
    """``python -m pms seed --index-only``: create/fill the Azure AI Search index (idempotent)."""
    if not _uses_ai_search(settings):
        return {"skipped": True, "reason": "offline mode: the SQLite FTS5 index is built in memory at start "
                                           "(Azure AI Search needs APP_ENV=azure, AZURE_SEARCH_ENDPOINT, "
                                           "AZURE_SEARCH_INDEX)"}
    search = get_knowledge_search(ref or get_reference_data(settings), settings)
    try:
        return search.sync(force=force)  # type: ignore[attr-defined]
    finally:
        search.close()


# --------------------------------------------------------------------------- files
class LocalFileStore:
    """Folder-based file store (offline): ``<root>/<area>/<name>``; writes are atomic."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, area: str, name: str) -> Path:
        if not area.replace("-", "").isalnum() or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("invalid file store area or name")
        return self.root / area / name

    def _stored(self, area: str, path: Path) -> StoredFile:
        stat = path.stat()
        return StoredFile(area=area, name=path.name, uri=str(path), size=stat.st_size,
                          modified_utc=iso_utc(_dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc)))

    def put(self, area: str, name: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredFile:
        path = self._path(area, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return self._stored(area, path)

    def get(self, area: str, name: str) -> bytes:
        return self._path(area, name).read_bytes()

    def list(self, area: str) -> list[StoredFile]:
        folder = self.root / area
        if not folder.is_dir():
            return []
        return [self._stored(area, p) for p in sorted(folder.iterdir()) if p.is_file() and not p.name.startswith(".")]

    def describe(self) -> str:
        return f"local folder {self.root}"


class MemoryFileStore:
    """In-memory file store for tests (same interface as :class:`LocalFileStore`)."""

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], tuple[bytes, str]] = {}

    def put(self, area: str, name: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredFile:
        stamp = iso_utc(_dt.datetime.now(_dt.timezone.utc))
        self._files[(area, name)] = (bytes(data), stamp)
        return StoredFile(area=area, name=name, uri=f"memory://{area}/{name}", size=len(data), modified_utc=stamp)

    def get(self, area: str, name: str) -> bytes:
        try:
            return self._files[(area, name)][0]
        except KeyError as exc:
            raise FileNotFoundError(f"{area}/{name}") from exc

    def list(self, area: str) -> list[StoredFile]:
        return [StoredFile(area=a, name=n, uri=f"memory://{a}/{n}", size=len(d), modified_utc=t)
                for (a, n), (d, t) in sorted(self._files.items()) if a == area]

    def describe(self) -> str:
        return "in-memory file store (tests)"


def get_file_store(settings: Settings) -> FileStore:
    """Blob Storage in Azure (STORAGE_ACCOUNT_URL); the local folder DATA_DIR otherwise."""
    if _azure(settings) and settings.storage_account_url:
        from .azure.blob_store import BlobFileStore

        return BlobFileStore(settings.storage_account_url, client_id=settings.azure_client_id)
    return LocalFileStore(settings.data_dir)


# --------------------------------------------------------------------------- notifications
def get_notifier(settings: Settings, files: FileStore | None = None) -> Notifier:
    """Azure: ACS e-mail to the configured demo recipients, sent by a background thread (never inside an
    upload) with an outbox copy in the file store; otherwise (and as fallback) the outbox folder."""
    from .notify import AcsEmailNotifier, BackgroundAlertSender, OutboxNotifier

    if settings.is_azure and settings.acs_endpoint and settings.acs_sender_address and settings.alert_recipients:
        return AcsEmailNotifier(endpoint=settings.acs_endpoint, sender=settings.acs_sender_address,
                                recipients=settings.alert_recipients, client_id=settings.azure_client_id,
                                base_url=settings.public_base_url, timeout_s=ACS_SEND_TIMEOUT_S,
                                background=BackgroundAlertSender(), files=files)
    return OutboxNotifier(files or get_file_store(settings), recipients=settings.alert_recipients,
                          base_url=settings.public_base_url)


def deadline_sweep_interval(settings: Settings | None) -> float:
    return AZURE_DEADLINE_SWEEP_S if _azure(settings) else 0.0


# --------------------------------------------------------------------------- agents, prices, telemetry
def get_agent_runtime(settings: Settings, **kwargs: Any) -> AgentRuntime:
    if settings.agent_backend == "foundry":
        from .agents.foundry import FoundryRuntime

        return FoundryRuntime(settings, **kwargs)  # published versions: get_agent_registry(settings)
    from .agents.offline import OfflineRuntime

    return OfflineRuntime(models_by_role={"analysis": settings.model_deployment_analysis,
                                          "general": settings.model_deployment_general}, **kwargs)


def get_price_table(settings: Settings) -> PriceTable:
    return PriceTable.load(settings.pricing_path)


def get_telemetry(settings: Settings, *, trace_path: str | Path | None = None) -> Telemetry:
    return Telemetry(trace_path=trace_path if trace_path is not None else (
        None if settings.is_azure else settings.trace_path),
        azure_connection_string=settings.applicationinsights_connection_string)


def get_agent_registry(settings: Settings) -> Any:
    """Where the published Foundry prompt-agent versions are kept: Cosmos DB ``records`` in Azure (survives
    restarts, shared by all processes), in memory otherwise."""
    if _azure(settings) and settings.cosmos_endpoint:
        from .azure.cosmos_store import CosmosAgentRegistry, account_from_settings

        return CosmosAgentRegistry(account_from_settings(settings))
    from .agents.foundry import MemoryAgentRegistry

    return MemoryAgentRegistry()


def start_azure_setup(settings: Settings, pipeline: Any) -> threading.Thread | None:
    """Worker start in Azure: bring the search index to the current corpus and publish changed Foundry prompt
    agents (both idempotent) in a background thread - alerts and jobs are served meanwhile. Offline: nothing."""
    if not settings.is_azure:
        return None

    def run() -> None:
        sync = getattr(pipeline.search, "sync", None)
        if callable(sync):
            try:
                log.info("azure setup: knowledge index %s", sync())
            except Exception:  # visible in the log; the first query retries
                log.exception("azure setup: knowledge index synchronisation failed")
        publish = getattr(pipeline.runtime, "publish_all", None)
        if callable(publish):
            try:
                log.info("azure setup: Foundry prompt agents %s",
                         [(p.get("agent"), p.get("action"), p.get("foundry_version")) for p in publish()])
            except Exception:  # agents are published on first use as well
                log.exception("azure setup: publishing the Foundry prompt agents failed")

    thread = threading.Thread(target=run, name="pms-azure-setup", daemon=True)
    thread.start()
    return thread
