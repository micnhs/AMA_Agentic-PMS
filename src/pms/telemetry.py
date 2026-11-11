"""Operational telemetry: structured log records and local agent-run spans (concept 6.6, 6.8; D-33).

Every record goes through the standard :mod:`logging` module with the attribute ``pms_event``
plus ``pms_*`` attributes in ``extra``. In Azure (``APPLICATIONINSIGHTS_CONNECTION_STRING``
set) :func:`configure_azure` enables azure-monitor-opentelemetry lazily, so the attributes land
in ``customDimensions`` for the workbook and the alert rules (infra ``observability.bicep``):

* ``ALERT_SENT`` (``pms_latency_s``, ``pms_case_id``)
* ``AGENT_RUN`` (``pms_status`` ok|failed, ``pms_agent``, ``pms_model``, ``pms_duration_ms``,
  ``pms_tokens_in``, ``pms_tokens_out``, ``pms_cost_usd`` + versions)
* ``WORKER_HEARTBEAT`` (at least every 5 minutes)
* ``AUDIT_VERIFY`` (``pms_status``, ``pms_records``)
* ``CASE_OVERDUE`` (``pms_case_id``)

Offline the records are only logged, and each agent run is also written as a span (JSON line)
to ``DATA_DIR/traces/agent-spans.jsonl`` with the same attributes. Telemetry never carries
letter text.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import iso_utc, utcnow

LOGGER_NAME = "pms.telemetry"
logger = logging.getLogger(LOGGER_NAME)

ALERT_SENT = "ALERT_SENT"
AGENT_RUN = "AGENT_RUN"
WORKER_HEARTBEAT = "WORKER_HEARTBEAT"
AUDIT_VERIFY = "AUDIT_VERIFY"
CASE_OVERDUE = "CASE_OVERDUE"
EVENTS: tuple[str, ...] = (ALERT_SENT, AGENT_RUN, WORKER_HEARTBEAT, AUDIT_VERIFY, CASE_OVERDUE)

HEARTBEAT_INTERVAL_S = 60  # contract: at least every 5 minutes
AUDIT_VERIFY_INTERVAL_S = 3600  # worker: hourly

_azure_lock = threading.Lock()
_azure_state: dict[str, Any] = {"configured": False, "error": None}


def configure_azure(connection_string: str | None) -> str:
    """Enable azure-monitor-opentelemetry once per process (lazy import). Returns a status text."""
    if not connection_string:
        return "local logging only (no Application Insights connection string)"
    with _azure_lock:
        if _azure_state["configured"]:
            return "azure monitor configured"
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor  # type: ignore[import-not-found]
        except ImportError as exc:
            _azure_state["error"] = f"azure-monitor-opentelemetry not installed: {exc}"
            logger.warning("telemetry: %s - logging locally only", _azure_state["error"])
            return _azure_state["error"]
        try:
            try:
                configure_azure_monitor(connection_string=connection_string, logger_name="pms")
            except TypeError:  # older signature without logger_name
                configure_azure_monitor(connection_string=connection_string)
        except Exception as exc:  # never let telemetry stop the app
            _azure_state["error"] = f"azure monitor configuration failed: {type(exc).__name__}: {exc}"
            logger.warning("telemetry: %s", _azure_state["error"])
            return _azure_state["error"]
        _azure_state["configured"] = True
        logging.getLogger("pms").setLevel(logging.INFO)
        return "azure monitor configured"


def azure_status() -> dict[str, Any]:
    return dict(_azure_state)


def _attrs(event: str, values: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {"pms_event": event}
    for key, value in values.items():
        name = key if key.startswith("pms_") else f"pms_{key}"
        if isinstance(value, (list, tuple, dict)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        extra[name] = value
    return extra


class Telemetry:
    """Emits the contract events; optionally writes local agent-run spans (JSON lines)."""

    def __init__(self, *, trace_path: str | Path | None = None, azure_connection_string: str | None = None,
                 service: str = "pms-agent-demo") -> None:
        self.trace_path = Path(trace_path) if trace_path is not None else None
        self.service = service
        self.azure_status = configure_azure(azure_connection_string) if azure_connection_string else (
            "local logging only")
        self._lock = threading.Lock()
        self.emitted: list[dict[str, Any]] = []  # in-process copy of the last events (for views and tests)

    # ------------------------------------------------------------------ generic
    def emit(self, event: str, message: str | None = None, **values: Any) -> dict[str, Any]:
        if event not in EVENTS:
            raise ValueError(f"unknown telemetry event {event!r}")
        extra = _attrs(event, values)
        logger.info(message or event, extra=extra)
        with self._lock:
            self.emitted.append({"ts_utc": iso_utc(utcnow(), ms=True), **extra})
            del self.emitted[:-500]
        return extra

    def events(self, event: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.emitted if event is None or e["pms_event"] == event]

    # ------------------------------------------------------------------ contract events
    def alert_sent(self, *, case_id: str, latency_s: float | None, channel: str, alert_id: str,
                   status: str = "SENT") -> dict[str, Any]:
        return self.emit(ALERT_SENT, f"alert {alert_id} {status} via {channel}", case_id=case_id,
                         latency_s=None if latency_s is None else round(float(latency_s), 3), channel=channel,
                         alert_id=alert_id, status=status)

    def agent_run(self, *, status: str, agent: str, model: str, duration_ms: float, tokens_in: int, tokens_out: int,
                  cost_usd: float, case_id: str, signal_id: str | None, run_id: str | None, backend: str,
                  model_version: str | None, prompt_version: str | None, agent_version: str | None,
                  rule_versions: list[str] | None, corpus_version: str | None, tool_calls: int,
                  started_utc: str, finished_utc: str, error: str | None = None) -> dict[str, Any]:
        attrs = {
            "status": "ok" if status == "DONE" else "failed", "agent": agent, "model": model,
            "duration_ms": round(float(duration_ms), 1), "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
            "cost_usd": round(float(cost_usd), 6), "case_id": case_id, "signal_id": signal_id, "run_id": run_id,
            "backend": backend, "model_version": model_version, "prompt_version": prompt_version,
            "agent_version": agent_version, "rule_versions": rule_versions or [], "corpus_version": corpus_version,
            "tool_calls": tool_calls, "error": error,
        }
        extra = self.emit(AGENT_RUN, f"agent run {agent} {attrs['status']}", **attrs)
        self._write_span({
            "name": f"agent_run {agent}", "trace_id": uuid.uuid4().hex, "span_id": uuid.uuid4().hex[:16],
            "service": self.service, "start_utc": started_utc, "end_utc": finished_utc,
            "attributes": {k: v for k, v in extra.items()},
        })
        return extra

    def worker_heartbeat(self, *, worker_id: str, queue: int, mode: str) -> dict[str, Any]:
        return self.emit(WORKER_HEARTBEAT, f"worker heartbeat {worker_id}", worker_id=worker_id, queue=queue,
                         mode=mode)

    def audit_verify(self, *, ok: bool, records: int, first_break_seq: int | None = None) -> dict[str, Any]:
        return self.emit(AUDIT_VERIFY, f"audit chain {'ok' if ok else 'BROKEN'}", status="ok" if ok else "failed",
                         records=records, first_break_seq=first_break_seq)

    def case_overdue(self, *, case_id: str, due_date: str | None, rule_set: str | None) -> dict[str, Any]:
        return self.emit(CASE_OVERDUE, f"case {case_id} overdue", case_id=case_id, due_date=due_date,
                         rule_set=rule_set)

    # ------------------------------------------------------------------ local spans
    def _write_span(self, span: dict[str, Any]) -> None:
        if self.trace_path is None:
            return
        try:
            with self._lock:
                self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                with self.trace_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(span, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # telemetry must never break the analysis
            logger.warning("telemetry: could not write span: %s", exc)

    def spans(self) -> list[dict[str, Any]]:
        if self.trace_path is None or not self.trace_path.exists():
            return []
        with self._lock:
            lines = self.trace_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def describe(self) -> dict[str, Any]:
        return {
            "logger": LOGGER_NAME,
            "events": list(EVENTS),
            "local_spans": str(self.trace_path) if self.trace_path else None,
            "azure_monitor": self.azure_status,
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
            "audit_verify_interval_s": AUDIT_VERIFY_INTERVAL_S,
        }


def monotonic() -> float:
    return time.perf_counter()
